"""РАЗБОР iCal: приватный адрес календаря → ближайшие события.

**Почему имя `integrations/ical.py`, а не `connectors/calendar.py`.** Каталог назывался
`connectors/` во множественном числе и обещал коннекторы (интеграции), а внутри был ОДИН парсер iCal:
сети он не касается, календарь скачивает вызывающий. Имя `calendar` тоже шире дела — календарей много,
разбирается ровно формат iCal. Здесь — только разбор; доставку делает транспорт, дедуп — таблица
`calendar_events_seen`.

Заменяет собой SaaS-интеграцию (Composio) без потери идеи: событие во внешнем календаре →
уведомление человеку в Telegram.

Реальный Google-календарь отдаёт три формы начала: `20261008T120000Z` (UTC),
`20261008T120000` (+`TZID=` или `X-WR-TIMEZONE`) и `20261008` (весь день). Плюс `RRULE`
(еженедельные встречи) — без разворота правила повторяющаяся встреча «застревает» в прошлом
и в окно уведомлений не попадает, поэтому FREQ=DAILY/WEEKLY/MONTHLY/YEARLY разворачиваются,
а неизвестное правило честно даёт только первое вхождение (пишем в лог).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from realty_agent import vocab

log = logging.getLogger(__name__)

DEFAULT_TZ = ZoneInfo("Europe/Moscow")
MAX_OCCURRENCES = 500
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MONTHS = 12


#: Форма строки таблицы календаря — контракт БАЗЫ, а не парсера (читает её только `db/store.py`),
#: поэтому определение живёт в общем слое. Здесь — РЕЭКСПОРТ: адрес `realty_agent.integrations.ical.CalendarEvent`
#: продолжает работать, источник истины один.
CalendarEvent = vocab.CalendarEvent


@dataclass
class _Recurring:
    uid: str
    summary: str
    location: str
    start: datetime
    all_day: bool
    rule: dict[str, str] = field(default_factory=dict)


async def fetch_ics(url: str, *, timeout_s: float = 20.0) -> str:
    """Скачать ICS. Единственное внешнее обращение коннектора — сам календарь."""
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "bridge/1.0"})
        response.raise_for_status()
        return response.text


def upcoming(
    raw: str,
    *,
    now: datetime | None = None,
    ahead_h: int = 24,
    tz: ZoneInfo = DEFAULT_TZ,
) -> list[CalendarEvent]:
    """События, начинающиеся в окне [now, now+ahead_h], по возрастанию времени."""
    moment = now or datetime.now(UTC)
    horizon = moment + timedelta(hours=ahead_h)
    result: list[CalendarEvent] = []
    for track in _parse(raw, tz=tz):
        for start in _occurrences(track, horizon=horizon):
            # Событие «весь день» началось в полночь: пока его день не кончился, оно ещё актуально.
            ongoing = track.all_day and start < moment < start + timedelta(days=1)
            if moment <= start <= horizon or ongoing:
                result.append(
                    CalendarEvent(
                        uid=track.uid,
                        summary=track.summary,
                        location=track.location,
                        starts_at=start,
                        all_day=track.all_day,
                    )
                )
                break  # ближайшее вхождение достаточно: следующие — в своём окне
    return sorted(result, key=lambda item: item.starts_at)


def format_event(event: CalendarEvent, *, tz: ZoneInfo) -> str:
    """Текст уведомления: название, время (в бизнес-таймзоне), место."""
    local = event.starts_at.astimezone(tz)
    when = "весь день" if event.all_day else local.strftime("%d.%m в %H:%M")
    lines = [f"📅 {when}: {event.summary or '(без названия)'}"]
    if event.location:
        lines.append(f"📍 {event.location}")
    return "\n".join(lines)


# ---------- разбор ---------- #


def _parse(raw: str, *, tz: ZoneInfo) -> list[_Recurring]:
    lines = _unfold(raw)
    calendar_tz = _calendar_tz(lines) or tz
    tracks: list[_Recurring] = []
    props: dict[str, tuple[dict[str, str], str]] = {}
    inside = False
    for line in lines:
        if line == "BEGIN:VEVENT":
            inside, props = True, {}
            continue
        if line == "END:VEVENT":
            inside = False
            track = _track(props, tz=calendar_tz)
            if track:
                tracks.append(track)
            continue
        if not inside:
            continue
        name, params, value = _split_property(line)
        if name:
            props[name] = (params, value)
    return tracks


def _track(props: dict[str, tuple[dict[str, str], str]], *, tz: ZoneInfo) -> _Recurring | None:
    start_prop = props.get("DTSTART")
    if not start_prop:
        return None
    params, value = start_prop
    start, all_day = _parse_dt(value, params, tz)
    if start is None:
        return None
    _, rule_value = props.get("RRULE", ({}, ""))
    return _Recurring(
        uid=props.get("UID", ({}, ""))[1] or f"{start.isoformat()}-{props.get('SUMMARY', ({}, ''))[1]}",
        summary=_unescape(props.get("SUMMARY", ({}, ""))[1]),
        location=_unescape(props.get("LOCATION", ({}, ""))[1]),
        start=start,
        all_day=all_day,
        rule=_parse_rule(rule_value),
    )


def _unfold(raw: str) -> list[str]:
    """RFC 5545: строка продолжается, если начинается с пробела или таба."""
    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return [line.strip() for line in lines if line.strip()]


def _split_property(line: str) -> tuple[str | None, dict[str, str], str]:
    head, _, value = line.partition(":")
    if not head:
        return None, {}, ""
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for chunk in parts[1:]:
        key, _, val = chunk.partition("=")
        params[key.upper()] = val.strip('"')
    return name, params, value


def _calendar_tz(lines: list[str]) -> ZoneInfo | None:
    for line in lines:
        if line.upper().startswith("X-WR-TIMEZONE"):
            name = line.partition(":")[2].strip()
            try:
                return ZoneInfo(name)
            except (ValueError, KeyError):  # ZoneInfoNotFoundError — подкласс KeyError
                log.warning("calendar: неизвестная X-WR-TIMEZONE %r", name)
    return None


def _parse_dt(value: str, params: dict[str, str], tz: ZoneInfo) -> tuple[datetime | None, bool]:
    text = (value or "").strip()
    all_day = params.get("VALUE", "").upper() == "DATE" or (len(text) == 8 and "T" not in text)
    zone = tz
    if params.get("TZID"):
        try:
            zone = ZoneInfo(params["TZID"])
        except (ValueError, KeyError):
            log.warning("calendar: неизвестная TZID %r", params["TZID"])
    try:
        if all_day:
            day = date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
            return datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(UTC), True
        if text.endswith("Z"):
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), False
        local = datetime.strptime(text[:15], "%Y%m%dT%H%M%S").replace(tzinfo=zone)
        return local.astimezone(UTC), False
    except ValueError:
        log.warning("calendar: не разобрал дату %r", text[:20])
        return None, all_day


def _parse_rule(value: str) -> dict[str, str]:
    rule: dict[str, str] = {}
    for chunk in (value or "").split(";"):
        key, _, val = chunk.partition("=")
        if key:
            rule[key.upper()] = val
    return rule


def _unescape(value: str) -> str:
    return (
        (value or "")
        .replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _occurrences(track: _Recurring, *, horizon: datetime) -> list[datetime]:
    """Вхождения события по возрастанию, не позже `horizon` (плюс хотя бы первое)."""
    starts = [track.start]
    freq = track.rule.get("FREQ", "").upper()
    if not freq or track.start > horizon:
        return starts
    interval = max(1, _int(track.rule.get("INTERVAL"), 1))
    until = _until(track.rule.get("UNTIL"), track.start.tzinfo)
    count = _int(track.rule.get("COUNT"), 0)
    produced = 1
    cursor = _fast_forward(track.start, freq, interval, horizon)
    for _ in range(MAX_OCCURRENCES):
        cursor = _step(cursor, freq, interval, track.rule)
        if cursor is None:
            break
        if until and cursor > until:
            break
        if count and produced >= count:
            break
        starts.append(cursor)
        produced += 1
        if cursor > horizon:
            break
    return starts


def _fast_forward(cursor: datetime, freq: str, interval: int, horizon: datetime) -> datetime:
    """Перескочить периоды, заведомо лежащие до окна: правило может идти с 2015 года."""
    if cursor >= horizon:
        return cursor
    if freq == "DAILY":
        return cursor + timedelta(days=max(0, (horizon - cursor).days // interval - 1) * interval)
    if freq == "WEEKLY":
        weeks = max(0, (horizon - cursor).days // (7 * interval) - 1)
        return cursor + timedelta(weeks=weeks * interval)
    if freq in ("MONTHLY", "YEARLY"):
        step = interval if freq == "MONTHLY" else MONTHS * interval
        months = (horizon.year - cursor.year) * MONTHS + horizon.month - cursor.month
        return _add_months(cursor, max(0, months // step - 1) * step)
    return cursor


def _step(cursor: datetime, freq: str, interval: int, rule: dict[str, str]) -> datetime | None:
    if freq == "DAILY":
        return cursor + timedelta(days=interval)
    if freq == "WEEKLY":
        days = [WEEKDAYS[item[-2:]] for item in rule.get("BYDAY", "").split(",") if item[-2:] in WEEKDAYS]
        if not days:
            return cursor + timedelta(weeks=interval)
        for shift in range(1, 8 * interval + 1):
            candidate = cursor + timedelta(days=shift)
            if candidate.weekday() in days:
                return candidate
        return cursor + timedelta(weeks=interval)
    if freq == "MONTHLY":
        return _add_months(cursor, interval)
    if freq == "YEARLY":
        return _add_months(cursor, MONTHS * interval)
    log.warning("calendar: правило %r не поддержано — беру только первое вхождение", freq)
    return None


def _add_months(moment: datetime, months: int) -> datetime:
    month_index = moment.month - 1 + months
    year = moment.year + month_index // MONTHS
    month = month_index % MONTHS + 1
    day = min(moment.day, _days_in_month(year, month))
    return moment.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _until(value: str | None, tz) -> datetime | None:
    if not value:
        return None
    parsed, _ = _parse_dt(value, {}, tz or UTC)
    return parsed


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default
