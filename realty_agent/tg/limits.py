"""Чистая логика Telegram-транспорта: лимитер частоты и тексты статусов (без PTB, тестируемо офлайн)."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


class RateLimiter:
    """Не больше `per_minute` сообщений на чат (антифлуд из плана: 10 msg/мин)."""

    def __init__(self, per_minute: int = 10) -> None:
        self._limit = per_minute
        self._history: dict[str, deque[float]] = defaultdict(deque)
        self._now = time.time  # инъекция часов для тестов

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = self._now() if now is None else now
        bucket = self._history[key]
        while bucket and current - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return False
        bucket.append(current)
        return True


STATUS_BY_ACTION = {"c": "confirmed", "r": "rejected"}


# ---: напоминание о подтверждённом визите ---
# Коды кнопок для подписанных callback-ов (`tg/callbacks.py:sign_callback`, префикс `bk:`).
# Коды короткие: `callback_data` ограничен 64 байтами, а подпись занимает 16 hex.
# Три роли действий названы раздельно, потому что права у них разные: менеджер одобряет
# текст и отмечает явку, клиент отвечает «приду»/«перенести».
VISIT_MANAGER_ACTIONS = {"va": "approve", "vr": "revise"}
VISIT_CLIENT_ACTIONS = {"vy": "yes", "vm": "move"}
VISIT_MARK_ACTIONS = {"wa": "attended", "wn": "no_show"}
# Один набор всех кодов визита: по нему обработчик кнопок отличает их от решений по броне.
VISIT_ACTIONS = {**VISIT_MANAGER_ACTIONS, **VISIT_CLIENT_ACTIONS, **VISIT_MARK_ACTIONS}

VISIT_DRAFT_TITLE = "Напоминание клиенту о визите — проверьте текст:"
VISIT_SENT_TO_CLIENT = "Отправлено клиенту. Отвечайте — напоминание продолжит работу."
VISIT_REWRITE_HINT = "Пришлите текст одним сообщением — он уйдёт клиенту вместо черновика."
VISIT_MARK_ONLY = "Отметить явку можно только по подтверждённой заявке"
VISIT_MARKED = {"attended": "Отмечено: клиент пришёл", "no_show": "Отмечено: клиент не пришёл"}
VISIT_ANSWER_ACK = {"yes": "Спасибо, ждём вас", "move": "Понял, предложим другое время"}
VISIT_NO_FLOW = "По этой заявке напоминание не ведётся"
#: Поток прямо сейчас исполняет планировщик. Отдельный текст, потому что это НЕ «потока нет»:
#: ход идёт, и через минуту ответ можно повторить (ADR 0036).
VISIT_BUSY = "Эту заявку сейчас обрабатывает напоминание — повторите через минуту"
#: Исходы продолжения потока. Три, а не два: «занято» — не «потока нет» (ADR `0036`).
VISIT_RESUMED = "resumed"
VISIT_ABSENT = "absent"
VISIT_BUSY_STATUS = "busy"
VISIT_MARK_PROMPT = "Отметьте, кто дошёл до показа (только прошедшие визиты):"


def status_for_action(action: str) -> str | None:
    return STATUS_BY_ACTION.get(action)


def can_confirm(*, manager_chat_id: str, chat_id: str) -> bool:
    """Подтверждать заявку может только настроенный чат менеджера."""
    return bool(manager_chat_id) and manager_chat_id == chat_id


def client_notification_text(*, status: str, slot: str, address: str) -> str:
    """Текст клиенту после решения менеджера. `address` — адрес объекта, НЕ внутренний id ()."""
    if status == "confirmed":
        return f"Запись подтверждена: {slot}, объект {address}. С собой — паспорт; если планы изменятся, напишите."
    if status == "rejected":
        return "К сожалению, это время не подтвердили. Напишите другой удобный слот — предложим варианты."
    return "Статус заявки обновлён."


def format_slot(value, tz: ZoneInfo) -> str:
    """Слот из БД (aware datetime в UTC) → «01.10 10:00» в бизнес-таймзоне.

    Слоты хранятся как «настенное» время МСК (см. `db.pg.parse_slot`), поэтому при показе
    обязательно переводим в бизнес-таймзону: иначе менеджер увидит время на 3 часа раньше.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return f"{moment.astimezone(tz):%d.%m %H:%M}"
    return str(value)


def format_slot_iso(value: str, tz: ZoneInfo) -> str:
    """ISO-слот из состояния bridge («2026-10-01T10:00», время бизнес-таймзоны) → «01.10 10:00»."""
    try:
        moment = datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    except ValueError:
        return str(value)
    return format_slot(moment if moment.tzinfo else moment.replace(tzinfo=tz), tz)
