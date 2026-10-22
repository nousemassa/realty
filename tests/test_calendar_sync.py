"""Проход по календарю в транспорте: дедуп, выключенный режим, окно.

Внешний HTTP подменяем: коннектор и дедуп проверяются вместе, без сети.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakes

from realty_agent.integrations import ical as calendar_connector


def _ics(uid: str, hours_ahead: int) -> str:
    stamp = (datetime.now(UTC) + timedelta(hours=hours_ahead)).strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        f"UID:{uid}\nSUMMARY:Встреча\nDTSTART:{stamp}\nLOCATION:Офис\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )


def _patch_fetch(monkeypatch, raw: str, calls: list[str]) -> None:
    async def fake_fetch(url: str, *, timeout_s: float = 20.0) -> str:
        calls.append(url)
        return raw

    monkeypatch.setattr(calendar_connector, "fetch_ics", fake_fetch)


async def test_calendar_disabled_without_url(monkeypatch):
    transport = fakes.make_transport(fakes.FakeDb(), settings=fakes.make_settings())
    calls: list[str] = []
    _patch_fetch(monkeypatch, _ics("u1", 2), calls)
    result = await transport.sync_calendar()
    assert result == {"enabled": False, "events": 0, "notified": 0, "delivered": 0}
    assert calls == []


async def test_calendar_notifies_once_and_skips_repeat(monkeypatch):
    db = fakes.FakeDb(managers={"100000002": {"active": True, "ready": True}})
    settings = fakes.make_settings(calendar_ics_url="https://calendar.invalid/private/basic.ics")
    transport = fakes.make_transport(db, settings=settings)
    calls: list[str] = []
    _patch_fetch(monkeypatch, _ics("dedup-1", 3), calls)

    first = await transport.sync_calendar()
    assert first["enabled"] is True and first["events"] == 1 and first["notified"] == 1

    second = await transport.sync_calendar()
    assert second["events"] == 1 and second["notified"] == 0, "повтор того же вхождения не отправляется"
    assert len(calls) == 2, "календарь опрашивается каждый проход"


async def test_calendar_ignores_events_outside_window(monkeypatch):
    db = fakes.FakeDb()
    settings = fakes.make_settings(calendar_ics_url="https://calendar.invalid/basic.ics", calendar_notify_ahead_h=2)
    transport = fakes.make_transport(db, settings=settings)
    _patch_fetch(monkeypatch, _ics("far-1", 30), [])
    result = await transport.sync_calendar()
    assert result["events"] == 0 and result["notified"] == 0


def test_ics_url_is_masked_in_logs():
    """Приватный токен iCal не должен попадать в лог: маскируем и в сообщении, и в аргументах."""
    import logging

    from agent_core import logging_redact

    url = "https://calendar.google.com/calendar/ical/user%40mail/private-SECRETTOKEN42/basic.ics"
    filt = logging_redact.RedactPrivateUrls()
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg="HTTP Request: %s %s", args=("GET", url), exc_info=None,
    )
    assert filt.filter(record) is True
    assert "SECRETTOKEN42" not in record.getMessage()
    assert "/private-***" in record.getMessage()


def test_redaction_keeps_mapping_arguments_shape():
    """Аргумент-СЛОВАРЬ остаётся словарём: иначе запись пропадала бы из журнала.

    logging разворачивает единственный mapping-аргумент (`log.warning("…: %r", payload)` с dict)
    в `record.args = payload`. Фильтр, который перебирал аргументы как кортеж, превращал его в
    кортеж КЛЮЧЕЙ, и форматирование падало с «not all arguments converted»: строка заменялась
    стеком ошибки логирования.
    """
    import logging

    from agent_core import logging_redact

    filt = logging_redact.RedactPrivateUrls()
    record = logging.LogRecord(
        name="realty_agent.reminders.pipeline", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="чужое продолжение: %r", args={"kind": "client", "url": "/private-SECRET/in.ics"},
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert isinstance(record.args, dict), "форма аргумента обязана сохраниться"
    assert "SECRET" not in record.getMessage()


class _ScriptedChannels:
    """Канал, отвечающий заранее заданным текстом (или падающий) — для шаблонного пути."""

    def __init__(self, *, text: str = "", error: Exception | None = None, timeout: bool = False) -> None:
        self.text = text
        self.error = error
        self.timeout = timeout
        self.asked: list[tuple[str, str]] = []

    async def ask(self, *, user: str, text: str, wait_s: float = 60.0, poll_s: float = 3.0, **kwargs):
        from agent_core.vendors.channels.port import Reply

        self.asked.append((user, text))
        if self.error is not None:
            raise self.error
        return Reply(session_id="s1", text="" if self.timeout else self.text, done=not self.timeout, records=1)


def _event(*, summary: str = "Созвон с клиентом", location: str = "Zoom"):
    # 10:00 UTC = 13:00 МСК — время в шаблоне «08.10 в 13:00»
    return calendar_connector.CalendarEvent(
        uid="evt-1", summary=summary, location=location,
        starts_at=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
    )


async def test_agent_text_used_when_facts_kept():
    channels = _ScriptedChannels(text="⏰ Напоминаю: созвон с клиентом в 13:00")
    transport = fakes.make_transport(
        fakes.FakeDb(), settings=fakes.make_settings(bridge_tz="Europe/Moscow"), channels=channels
    )
    message = await transport._event_message(_event())
    assert message == "⏰ Напоминаю: созвон с клиентом в 13:00"
    assert channels.asked and channels.asked[0][0] == "calendar-notify-evt-1", "сессия на событие, изолированная"
    assert "13:00" in channels.asked[0][1], "агенту переданы факты из шаблона"


async def test_fallback_when_agent_drops_the_time():
    """Главный риск: агент переписал факты. Терять время нельзя — уходим на шаблон."""
    channels = _ScriptedChannels(text="Скоро встреча, не забудь!")
    transport = fakes.make_transport(fakes.FakeDb(), settings=fakes.make_settings(), channels=channels)
    message = await transport._event_message(_event())
    assert message == "📅 08.10 в 13:00: Созвон с клиентом\n📍 Zoom"


async def test_fallback_on_agent_failure():
    channels = _ScriptedChannels(error=RuntimeError("канал недоступен"))
    transport = fakes.make_transport(fakes.FakeDb(), settings=fakes.make_settings(), channels=channels)
    assert await transport._event_message(_event()) == "📅 08.10 в 13:00: Созвон с клиентом\n📍 Zoom"


async def test_flag_off_keeps_template_without_llm_call():
    channels = _ScriptedChannels(text="что угодно")
    transport = fakes.make_transport(
        fakes.FakeDb(), settings=fakes.make_settings(calendar_agent_text=False), channels=channels
    )
    assert (await transport._event_message(_event())).startswith("📅 08.10")
    assert channels.asked == [], "при CALENDAR_AGENT_TEXT=0 агент не вызывается вовсе"


async def test_too_long_answer_rejected():
    channels = _ScriptedChannels(text="в 13:00 " + "очень длинное напоминание " * 20)
    transport = fakes.make_transport(fakes.FakeDb(), settings=fakes.make_settings(), channels=channels)
    assert (await transport._event_message(_event())).startswith("📅 08.10")


async def test_no_foreign_session_is_ever_read(monkeypatch):
    """Чужие сессии не читаются НИ В КАКОМ режиме.

    Фолбэк «свежие 20» даже в НЕстрогом режиме даёт два следствия: у чата, чья сессия создана под
    другой ревизией, своя сессия не находится — мост ждёт ответ в чужих и отдаёт клиенту запасной
    текст, хотя агент отвечал; а при совпадении по времени чужой ответ уходит другому клиенту (первый
    выросший `done` становится ответом текущего хода).

    Своей сессии ещё нет — возвращаем пусто. Это не поломка: цикл опроса перечитывает
    список каждый тик, а первое сообщение создаёт сессию, и следующий тик её находит.
    """
    from agent_core.vendors.channels.agenta import AgentaChannels as ChannelsClient

    client = ChannelsClient(base_url="http://agenta.invalid", api_key="k" * 32, project_id="p", bot="test-bot")
    seen: list[str | None] = []

    async def fake_session_ids(*, revision_id=None, user=None, limit=20):
        """Как в жизни: сессии ЭТОГО пользователя ещё нет, а без фильтра платформа отдаёт свежие 20."""
        seen.append(user)
        return [] if user else ["foreign", "foreign2"]

    monkeypatch.setattr(client, "session_ids", fake_session_ids)
    assert await client._poll_sessions(user="calendar-notify-u1")() == []
    assert await client._poll_sessions(user="calendar-notify-u1", strict=True)() == []
    assert all(user for user in seen), f"запрос без пользователя — это чтение чужих сессий: {seen}"
    await client.aclose()


async def test_fallback_when_agent_timed_out():
    """Ход не успел за бюджет — уведомление всё равно уходит (шаблоном)."""
    channels = _ScriptedChannels(timeout=True)
    transport = fakes.make_transport(fakes.FakeDb(), settings=fakes.make_settings(), channels=channels)
    assert (await transport._event_message(_event())).startswith("📅 08.10")
