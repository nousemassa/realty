"""Подписи и карточки для клиента/менеджера/овнера (чистые функции — тестируются офлайн).

Формат слотов: строки вида `YYYY-MM-DDTHH:MM` в бизнес-таймзоне (так их собирает
`TelegramTransport._free_slots`) — поэтому конвертация tz здесь не нужна; обе формы («с пробелом» и «с T»)
приводит `canonical`.

Что уже в проде: `manager_card` (карточка заявки менеджеру), `report_text`/`status_text` (отчёт и статус
овнера), `address_for`/`object_lines` (каталог клиента). Ждут своих этапов: подписи дней/времён и сетка
слотов, `client_card` (карточка подтверждения) —().
"""
from __future__ import annotations

import datetime as dt
import re

from realty_agent.channels.turn_context import Listing

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def canonical(slot: str) -> str:
    """Канонизируем слот к форме с `T`: в проекте законны обе («2026-10-02 10:00» и с `T»)."""
    return re.sub(r"\s+", "T", (slot or "").strip())


def _parse(slot: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(canonical(slot))
    except ValueError:
        return None


def slot_dt(slot_iso: str) -> dt.datetime:
    """Строгий разбор (для проверок); подписи ниже терпимы к мусору и не роняют ход."""
    return dt.datetime.fromisoformat(canonical(slot_iso))


def day_label(slot_iso: str) -> str:
    """«02.10 чт» — подпись кнопки дня."""
    date = _parse(slot_iso)
    return f"{date:%d.%m} {WEEKDAYS[date.weekday()]}" if date else (slot_iso or "—")


def time_label(slot_iso: str) -> str:
    """«10:00» — подпись кнопки времени и строка карточки."""
    date = _parse(slot_iso)
    return f"{date:%H:%M}" if date else (slot_iso or "—")


def day_label_pretty(slot_iso: str) -> str:
    """«02.10 (чт)» — шапка выбранного дня (), в отличие от короткой подписи кнопки «02.10 чт»."""
    date = _parse(slot_iso)
    return f"{date:%d.%m} ({WEEKDAYS[date.weekday()]})" if date else (slot_iso or "—")


def pretty_date(slot_iso: str) -> str:
    """«02.10.2026 (чт), 10:00 МСК» — для карточек."""
    date = _parse(slot_iso)
    if not date:
        return slot_iso or "—"
    return f"{date:%d.%m.%Y} ({WEEKDAYS[date.weekday()]}), {date:%H:%M} МСК"


def short_price(price: str | None) -> str:
    """«8900000» → «8,9 млн ₽»; нераспознанное возвращаем как есть."""
    raw = (price or "").replace("\u00a0", " ").replace(" ", "").replace("₽", "")
    if not raw.isdigit():
        return price or ""
    value = int(raw)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".replace(".", ",") + " млн ₽"
    if value >= 1_000:
        return f"{value // 1_000} тыс ₽"
    return f"{value} ₽"


def rooms_label(rooms: str | None) -> str:
    rooms = (rooms or "").strip()
    return f"{rooms}к" if rooms.isdigit() else rooms


def short_address(listing: Listing) -> str:
    """«ул. Пушкина, 10» — улица + дом (номер квартиры в кнопке не нужен)."""
    parts = [part.strip() for part in (listing.address or "").split(",") if part.strip()]
    return ", ".join(parts[:2]) or listing.id


def full_address(listing: Listing | None) -> str:
    """Полный адрес; комнатность добавляем, только если её нет в адресе."""
    if listing is None:
        return "—"
    address = (listing.address or "").strip()
    rooms = rooms_label(listing.rooms)
    parts = [part for part in (listing.address or "").split(",") if part.strip()]
    if rooms and rooms not in address and len(parts) < 3:
        return f"{address} · {rooms}".strip(" ·")
    return address or rooms or listing.id


def address_for(listing_id: str | None, listings: list[Listing]) -> str | None:
    """Короткий адрес объекта по id из состояния (или None, если он вне активного списка)."""
    if not listing_id:
        return None
    return next((short_address(item) for item in listings if item.id == listing_id), None)


BUTTON_LABEL_LIMIT = 40


def features(address: str | None) -> str:
    """Содержимое скобок адреса: «2-комн., 54 м², 7/9 эт., 5 мин до метро».

    Метраж/этаж/метро живут именно здесь (в схеме отдельных колонок нет), поэтому и в тексте
    каталога, и в фильтре по ограничению нужны эти данные.
    """
    found = re.search(r"\(([^)]*)\)", address or "")
    return found.group(1).strip() if found else ""


def short_features(address: str | None) -> str:
    """Короткая выжимка для логов/подписей: «54 м², 7/9 эт.» (если таких данных нет — пусто)."""
    parts = [part.strip() for part in features(address).split(",") if part.strip()]
    keep = [part for part in parts if "м²" in part or "эт" in part]
    return ", ".join(keep[:2])


LISTING_ID_TOKEN_RE = re.compile(r"\b([a-zа-яё]{1,3}-\d{1,4})\b", re.IGNORECASE)


def replace_listing_ids(text: str, listings: list[Listing]) -> str:
    """: «объект L-1» → «объект ул. Пушкина, 10, кв. 42» — страховка на случай утечки id от агента.

    Применяется к тексту, который уходит ЧЕЛОВЕКУ (кнопочный и текстовый пути), уже ПОСЛЕ вырезания
    служебных блоков BOOKING| — поэтому протокол не задевается.
    """
    if not text:
        return text
    ids = {item.id.lower() for item in listings}
    return LISTING_ID_TOKEN_RE.sub(
        lambda match: address_label(match.group(1), listings) if match.group(1).lower() in ids else match.group(0),
        text,
    )


def address_label(listing_id: str | None, listings: list[Listing]) -> str:
    """Адрес объекта ДЛЯ ЧЕЛОВЕКА (): «ул. Пушкина, 10, кв. 42» — без скобок-характеристик.

    Внутренний id в текстах клиенту/менеджеру не показываем; id остаётся только резервом,
    когда объекта уже нет в активном каталоге (иначе показать нечего).
    """
    item = next((x for x in listings if x.id == listing_id), None)
    return address_wo_features(item) if item is not None else (listing_id or "—")


def address_wo_features(listing: Listing) -> str:
    """Адрес без скобок: «ул. Пушкина, 10, кв. 42»."""
    return re.sub(r"\s*\([^)]*\)", "", listing.address or "").strip().rstrip(",") or listing.id


def object_label(index: int, listing: Listing) -> str:
    """Лейбл кнопки: «1 · ул. Пушкина, 10 · 8,9 млн ₽». Метраж/этаж — в тексте, не в кнопке.

    Слишком длинный адрес сокращаем (цену не рвём никогда): Telegram растягивает клавиатуру
    по самому длинному лейблу.
    """
    price = short_price(listing.price)
    address = short_address(listing)
    base = f"{index} · {address}"
    if price and len(base) + len(price) + 3 > BUTTON_LABEL_LIMIT:
        keep = max(6, BUTTON_LABEL_LIMIT - len(str(index)) - len(price) - 6)
        address = address[:keep].rstrip(" ,·") + "…"
        base = f"{index} · {address}"
    return " · ".join(part for part in (base, price) if part)


def object_line(index: int, listing: Listing) -> str:
    """Полная строка каталога: «1 · ул. Пушкина, 10, кв. 42 · 2-комн., 54 м², 7/9 эт. · 8,9 млн ₽»."""
    parts = [str(index), address_wo_features(listing), features(listing.address), short_price(listing.price)]
    return " · ".join(part for part in parts if part)


def object_lines(listings: list[Listing]) -> str:
    """Текст каталога: номер объекта ровно один раз (раньше дублировался) и полные данные."""
    return "\n".join(object_line(i, item) for i, item in enumerate(listings, start=1))


def catalog_note(total: int, shown: int) -> str:
    """Приписка про лимит показа (.2): честное N берётся из count, до обрезки."""
    if total > shown:
        return f"Показаны первые {shown} из {total} — уточните запрос, если нужного нет в списке."
    return ""


def client_card(*, listing: Listing | None, slot_iso: str, name: str, phone_mask: str) -> str:
    """Карточка подтверждения (образец: таблица «поле → значение»). Телефон — маской."""
    rows = [
        ("Объект", full_address(listing)),
        ("Дата и время", pretty_date(slot_iso)),
        ("Имя", name or "—"),
        ("Телефон", phone_mask or "—"),
    ]
    width = max(len(title) for title, _ in rows)
    body = "\n".join(f"{title.ljust(width)}   {value}" for title, value in rows)
    return f"🧾 Проверьте заявку\n\n{body}"


def manager_card(*, booking_id, listing: Listing | None, slot_iso: str, name: str, phone: str) -> str:
    """Карточка брони менеджеру: телефон в открытом виде (менеджер — уполномоченное лицо)."""
    return (
        f"🔔 Новая заявка #{booking_id}\n"
        f"Объект: {full_address(listing)}\n"
        f"Время: {pretty_date(slot_iso)}\n"
        f"Клиент: {name}, {phone}"
    )


def status_text(*, mode: str, pending: int, managers_active: int, managers_ready: int,
                updates_processed: int, last_update_age_s: int | None, schema_ok: bool) -> str:
    """ℹ️ Статус овнера: состояние сервиса без ПД ()."""
    mode_label = {"live": "принимает заявки", "paused": "на паузе", "off": "выключен"}.get(mode, mode)
    age = "нет данных" if last_update_age_s is None else f"{last_update_age_s} с назад"
    return (
        f"ℹ️ Статус\n\n"
        f"Сервис: {mode_label}\n"
        f"Заявок ждёт: {pending}\n"
        f"Менеджеры: {managers_active} активных, {managers_ready} готовы получать\n"
        f"Схема 2b/2c: {'применена' if schema_ok else 'НЕ применена'}\n"
        f"Апдейтов обработано: {updates_processed}, последний: {age}"
    )


def report_text(*, day: str, total: int, confirmed: int, rejected: int, pending: int,
                upcoming: int, dialogues: int, llm_turns: int, cost_usd: float | None) -> str:
    """📊 Отчёт овнера: только числа (без ПД)."""
    money = "нет данных" if cost_usd is None else f"${cost_usd:.2f}"
    return (
        f"📊 Отчёт за {day}\n\n"
        f"Заявки: {total} — подтверждено {confirmed}, отклонено {rejected}, ожидают {pending}\n"
        f"Предстоящие просмотры: {upcoming}\n"
        f"Диалогов: {dialogues} · LLM-ходов: {llm_turns} · расходы: {money}"
    )


def nick_at(name: str | None) -> str:
    """Ник для ПОКАЗА: «purkautuva» / «@purkautuva» / «@@purkautuva» → «@purkautuva» (идемпотентно).

    Два пути записи ника (ручное добавление овнером и самозаявка) исторически давали разный
    формат, а шаблоны дописывают «@» сами ⇒ в списках появлялось «@@nick» (дефект).
    """
    return "@" + str(name or "").lstrip("@").strip()


def nick_bare(name: str | None) -> str:
    """Ник для ХРАНЕНИЯ и для шаблонов, где «@» уже стоит в тексте: без ведущего «@»."""
    return str(name or "").lstrip("@").strip()
