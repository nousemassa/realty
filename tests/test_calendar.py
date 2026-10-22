"""Разбор ICS: три формы начала, «весь день», повторы, окно уведомления, формат текста."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from realty_agent.integrations import ical

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)  # 09:00 МСК, четверг


def _ics(*events: str) -> str:
    return "BEGIN:VCALENDAR\n" + "\n".join(events) + "\nEND:VCALENDAR\n"


UTC_EVENT = """BEGIN:VEVENT
UID:u1
SUMMARY:Созвон с клиентом
DTSTART:20261008T100000Z
LOCATION:Zoom
END:VEVENT"""

LOCAL_EVENT = """BEGIN:VEVENT
UID:u2
SUMMARY:Показ квартиры
DTSTART;TZID=Europe/Moscow:20261008T150000
END:VEVENT"""

ALLDAY_EVENT = """BEGIN:VEVENT
UID:u3
SUMMARY:Отпуск
DTSTART;VALUE=DATE:20261008
END:VEVENT"""

WEEKLY_EVENT = """BEGIN:VEVENT
UID:u4
SUMMARY:Планёрка отдела
DTSTART;TZID=Europe/Moscow:20260101T100000
RRULE:FREQ=WEEKLY;BYDAY=TH
END:VEVENT"""

FOLDED_EVENT = """BEGIN:VEVENT
UID:u5
SUMMARY:Длинное назва
 ние с переносом
DTSTART:20261008T110000Z
END:VEVENT"""

FAR_EVENT = """BEGIN:VEVENT
UID:u6
SUMMARY:Через три дня
DTSTART:20261011T100000Z
END:VEVENT"""


def test_utc_and_local_forms_land_in_window():
    events = ical.upcoming(_ics(UTC_EVENT, LOCAL_EVENT), now=NOW, ahead_h=24, tz=MSK)
    assert [(e.uid, e.starts_at) for e in events] == [
        ("u1", datetime(2026, 10, 8, 10, 0, tzinfo=UTC)),
        ("u2", datetime(2026, 10, 8, 12, 0, tzinfo=UTC)),  # 15:00 МСК
    ]


def test_all_day_event_flagged():
    events = ical.upcoming(_ics(ALLDAY_EVENT), now=NOW, ahead_h=24, tz=MSK)
    assert events[0].all_day is True
    assert events[0].starts_at == datetime(2026, 10, 7, 21, 0, tzinfo=UTC)  # полночь МСК


def test_weekly_rule_reaches_today_from_old_start():
    """Правило идёт с 1 января: без разворота RRULE встреча «застряла» бы в прошлом."""
    events = ical.upcoming(_ics(WEEKLY_EVENT), now=NOW, ahead_h=24, tz=MSK)
    assert [e.uid for e in events] == ["u4"]
    assert events[0].starts_at == datetime(2026, 10, 8, 7, 0, tzinfo=UTC)  # 10:00 МСК в четверг


def test_event_outside_window_is_skipped():
    assert ical.upcoming(_ics(FAR_EVENT), now=NOW, ahead_h=24, tz=MSK) == []
    assert [e.uid for e in ical.upcoming(_ics(FAR_EVENT), now=NOW, ahead_h=24 * 4, tz=MSK)] == ["u6"]


def test_folded_lines_and_escaping():
    event = ical.upcoming(_ics(FOLDED_EVENT), now=NOW, ahead_h=24, tz=MSK)[0]
    assert event.summary == "Длинное название с переносом"


def test_unsupported_rule_gives_first_occurrence_only():
    """FREQ=SECONDLY не поддержан — честно берём только первое вхождение (в прошлом ⇒ не в окне)."""
    raw = _ics(
        "BEGIN:VEVENT\nUID:u7\nSUMMARY:Странное\nDTSTART:20260101T100000Z\nRRULE:FREQ=SECONDLY\nEND:VEVENT"
    )
    assert ical.upcoming(raw, now=NOW, ahead_h=24, tz=MSK) == []


def test_format_event_uses_business_timezone_and_place():
    event = ical.upcoming(_ics(UTC_EVENT), now=NOW, ahead_h=24, tz=MSK)[0]
    assert ical.format_event(event, tz=MSK) == "📅 08.10 в 13:00: Созвон с клиентом\n📍 Zoom"
    all_day = ical.upcoming(_ics(ALLDAY_EVENT), now=NOW, ahead_h=24, tz=MSK)[0]
    assert ical.format_event(all_day, tz=MSK) == "📅 весь день: Отпуск"
