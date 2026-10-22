"""Парсер BOOKING-блока с анти-эхо (план v11, решения 6 и промпты).

Правила приёмки блока (все обязательны):
  1) блок — ПОСЛЕДНЯЯ непустая строка ответа (после rstrip и снятия ```-фенса);
  2) формат строго: BOOKING|listing_id|slot(ISO c 'T')|name|phone@ch<лексема>
  3) лексема — challenge текущего хода (клиент её не знает);
  4) приём валиден только в состоянии await_confirm (проверяет вызывающая сторона);
  5) поле — валидируются: listing_id (active FK), slot (расписание/горизонт), phone (canonical >=10 цифр), name (<=40, без control-символов).
Любое несовпадение → невалид (никогда не «угадываем»).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from realty_agent.phones import MASKED_LIKE_RE, canonical_phone

BLOCK_RE = re.compile(
    r"^BOOKING\|(?P<listing_id>[A-Za-z0-9_-]{1,20})\|"
    r"(?P<slot>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\|"
    r"(?P<name>[^|\n]{1,40})\|"
    r"(?P<phone>[+\d][\d\s()\-*•]{6,22})@ch(?P<lex>[A-Za-z0-9]{4,12})$"
)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# BOOKING-подобное может быть встроено в ЛЮБОЕ место сообщения клиента (эхо-атака) —
# поэтому вырезаем не только строки, начинающиеся с BOOKING.
CLIENT_BLOCK_LIKE_RE = re.compile(r"BOOKING\s*\|[^\n]*", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedBooking:
    listing_id: str
    slot: str          # "YYYY-MM-DDTHH:MM"
    name: str
    phone: str         # canonical +7XXXXXXXXXX либо маска (тогда цифры подставит bridge)
    raw: str


def _last_nonempty_line(text: str) -> str | None:
    """Последняя непустая строка; снимаем хвостовой ```-фенс, отдельно стоящие ``` и пробелы."""
    if not text:
        return None
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    # убрать пустые хвосты и закрывающий фенс
    while lines and (not lines[-1] or lines[-1].startswith("```")):
        lines.pop()
    return lines[-1] if lines else None


def parse_filter_block(text: str) -> dict | None:
    """Вытащить блок `FILTER|{...}` из ответа агента (DD-8).

    Блок может стоять где угодно (но НЕ последней строкой — последняя остаётся за BOOKING-контрактом);
    мусор, не-json и чужие ключи молча игнорируются: фильтр — подсказка, а не команда.
    """
    from realty_agent.booking.filter import normalize_spec

    for line in (text or "").splitlines():
        stripped = line.strip().strip("`").strip()
        if not stripped.upper().startswith("FILTER|"):
            continue
        raw = stripped.split("|", 1)[1].strip()
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return normalize_spec(parsed)
    return None


def strip_filter_block(text: str) -> str:
    """Убрать строки FILTER| из ответа — клиент не должен видеть служебный формат."""
    lines = [line for line in (text or "").splitlines() if not line.strip().upper().startswith("FILTER|")]
    return "\n".join(lines).strip()


COVERAGE_PREFIX = "COVERAGE|"


def parse_coverage_block(text: str) -> bool | None:
    """Выбор агента «ответил или уточнил»: `COVERAGE|{"answered": true|false}`.

    **Зачем это разбирать.** Владелец поставил вопрос, на который иначе не ответить: «где мера
    между необходимостью уточняющего вопроса и прямым ответом». Дешёвой меры нет — лексическое
    совпадение ошибается в обе стороны (таблица в `realty_agent/retrieval/coverage.py`), поэтому
    решает модель, но её выбор обязан быть **наблюдаемым**. С этим маркером «мера» становится
    числом: доля ходов, где бот ответил, против ходов, где он спросил.

    `None` — маркера нет или он нечитаемый. Это не ошибка: маркер наблюдательный, а не
    команда, и его отсутствие ничего не ломает.
    """
    for line in (text or "").splitlines():
        stripped = line.strip().strip("`").strip()
        if not stripped.upper().startswith(COVERAGE_PREFIX):
            continue
        try:
            parsed = json.loads(stripped.split("|", 1)[1].strip())
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        answered = parsed.get("answered")
        return answered if isinstance(answered, bool) else None
    return None


def strip_coverage_block(text: str) -> str:
    """Убрать строки `COVERAGE|` из ответа — клиент не должен видеть служебный формат."""
    lines = [
        line for line in (text or "").splitlines()
        if not line.strip().upper().startswith(COVERAGE_PREFIX)
    ]
    return "\n".join(lines).strip()


# Обещания, за которыми НЕТ механизма. Живой случай: агент написал «Полный перечень под
# конкретный объект уточню у менеджера», а менеджеру не ушло ничего — ни уведомления, ни
# записи в `manager_actions`. Клиент ждал человека, который ничего не получил.
# **Ложное обещание хуже отказа:** отказ клиент видит, а обещание — нет.
# Список явный (канон проекта: «перечислять, а не мерить»), сравнение по нормализованному
# тексту — регистр, `ё`, лишние пробелы и знаки.
UNFULFILLED_PROMISE_PHRASES = (
    "уточню у менеджера",
    "уточню у специалиста",
    "уточним у менеджера",
    "уточню и напишу",
    "менеджер пришлет",
    "менеджер уточнит",
    "менеджер подскажет",
    "менеджер свяжется",
    "передам менеджеру",
    "передам ваш вопрос",
    "свяжусь с менеджером",
)

# Как в боте подписана НАСТОЯЩАЯ кнопка вызова человека (`realty_agent/ui/menu.py:30`). По ней
# менеджеры получают уведомление (`bot.py:534-536`), поэтому бот обязан называть её так же —
# иначе клиент не найдёт, куда нажать, а разница в формулировке выглядит как разные действия.
ESCALATION_BUTTON_LABEL = "Запрос звонка от менеджера"


def _normalize_phrase(text: str) -> str:
    """Нормализация для сравнения фраз: регистр, `ё`, пробелы, краевые знаки."""
    return " ".join((text or "").lower().replace("ё", "е").split()).strip(" .,:;!?—–-")


def find_unfulfilled_promise(text: str) -> str | None:
    """Найти фразу-обещание, за которой нет действия. `None` — обещаний нет.

    Возвращает найденную фразу: в журнале нужно видеть, что именно пообещал бот, а не только
    факт «обещал». Наблюдение, а не запрет: запрет живёт в контракте хода, здесь — счётчик.
    """
    normalized = _normalize_phrase(text)
    if not normalized:
        return None
    for phrase in UNFULFILLED_PROMISE_PHRASES:
        if phrase in normalized:
            return phrase
    return None


def offers_escalation(text: str) -> bool:
    """Предложил ли бот НАСТОЯЩЕЕ действие вместо обещания — кнопку запроса звонка."""
    return ESCALATION_BUTTON_LABEL.lower() in _normalize_phrase(text)


def neutralize_client_blocks(user_text: str) -> str:
    """Защита от эхо: любое BOOKING-подобное во вводе КЛИЕНТА (в т.ч. в середине строки) → плейсхолдер."""
    if not user_text:
        return user_text
    return CLIENT_BLOCK_LIKE_RE.sub("[BOOKING-подобная строка удалена из ввода клиента]", user_text)


def parse_booking_block(
    agent_reply: str,
    *,
    lexeme: str,
    listing_id_valid: bool = True,
    slot_valid: bool | None = None,
) -> tuple[ParsedBooking | None, str]:
    """Возвращает (booking | None, причина отбоя). Невалид никогда не «додумывается»."""
    line = _last_nonempty_line(agent_reply)
    if not line:
        return None, "empty_reply"
    match = BLOCK_RE.match(line)
    if not match:
        return None, "no_block_in_last_line"
    if match.group("lex") != lexeme:
        return None, "bad_challenge_lexeme"

    name = match.group("name").strip()
    if not name or CONTROL_CHARS_RE.search(name):
        return None, "bad_name"

    phone = canonical_phone(match.group("phone"))
    if not phone and not MASKED_LIKE_RE.search(match.group("phone")):
        return None, "bad_phone"

    if not listing_id_valid:
        return None, "unknown_listing"
    if slot_valid is False:
        return None, "slot_unavailable"

    return (
        ParsedBooking(
            listing_id=match.group("listing_id"),
            slot=match.group("slot"),
            name=name,
            phone=phone,
            raw=line,
        ),
        "ok",
    )
