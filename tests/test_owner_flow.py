"""Овнер-флоу: тайм-аут, отмена, «не съедать» обычный текст."""
from __future__ import annotations

from fakes import OWNER, FakeCallback, FakeDb, FakeMsg, fake_client, make_transport

from agent_core.ui import tokens
from realty_agent.tg import handlers
from realty_agent.tg.owner_flows import FLOW_ADD, FLOW_TTL_S
from realty_agent.ui import menu
from realty_agent.ui.kinds import KIND_CANCEL, KIND_OWNER


def transport_for(db):
    t = make_transport(db)
    t._limiter._now = lambda: 1000.0
    t._client = fake_client()
    return t


def texts(event) -> str:
    return "\n".join(item[0] or "" for item in event.sent)


async def tap(transport, kind: str, arg: str = ""):
    event = FakeCallback(tokens.sign("s" * 40, chat_id=OWNER, kind=kind, arg=arg), chat_id=OWNER)
    await transport._on_callback(event)
    return event


async def test_plain_text_without_flow_reaches_agent():
    db = FakeDb()
    t = transport_for(db)
    await t._on_message(FakeMsg("ищу двушку", chat_id=OWNER))

    assert t._channels.calls == 1
    assert db.owner_flow is None


async def test_flow_does_not_eat_plain_text_before_start():
    """«Добавить» нажимают, но ник ещё не прислали: обычные слова идут агенту, а не парсеру."""
    db = FakeDb()
    t = transport_for(db)

    assert db.owner_flow is None
    await t._on_message(FakeMsg("привет", chat_id=OWNER))
    assert t._channels.calls == 1


async def test_add_flow_asks_username_and_creates_nothing_by_itself():
    db = FakeDb()
    t = transport_for(db)
    event = await tap(t, KIND_OWNER, "add")

    assert db.owner_flow == FLOW_ADD
    assert db.managers == {}, "тап «Добавить» сам никого не добавляет"
    assert handlers.MANAGERS_ADD_ASK in texts(event)


async def test_flow_times_out_after_ten_minutes():
    db = FakeDb()
    t = transport_for(db)
    await tap(t, KIND_OWNER, "add")

    db.now += FLOW_TTL_S + 1
    await t._on_message(FakeMsg("привет", chat_id=OWNER))

    assert db.owner_flow is None, "просроченный флоу снимается"
    assert t._channels.calls == 1, "текст уходит агенту, а не в парсер ника"


async def test_cancel_button_clears_flow():
    db = FakeDb()
    t = transport_for(db)
    await tap(t, KIND_OWNER, "add")

    event = await tap(t, KIND_CANCEL, "flow")

    assert db.owner_flow is None
    assert handlers.FLOW_CANCELLED in texts(event)


async def test_cancel_word_clears_flow():
    db = FakeDb()
    t = transport_for(db)
    await tap(t, KIND_OWNER, "add")

    event = FakeMsg("отмена", chat_id=OWNER)
    await t._on_message(event)

    assert db.owner_flow is None
    assert handlers.FLOW_CANCELLED in texts(event)
    assert db.managers == {}


async def test_menu_tap_clears_flow():
    """Владелец выбрал другой раздел — флоу снимаем, иначе текст съедал бы парсер ника."""
    db = FakeDb(pending=[])
    t = transport_for(db)
    await tap(t, KIND_OWNER, "add")

    event = FakeMsg(menu.label(menu.MENU_OBJECTS), chat_id=OWNER)
    await t._on_message(event)

    assert db.owner_flow is None
    assert t._channels.calls == 0
    assert handlers.CLIENT_PATH_NOTICE in texts(event)


async def test_command_clears_flow():
    db = FakeDb(consent=True)
    t = transport_for(db)
    await tap(t, KIND_OWNER, "add")

    await t._on_message(FakeMsg("/start", chat_id=OWNER))

    assert db.owner_flow is None


async def test_flow_does_not_leak_between_chats():
    """Флоу чата владельца не влияет на клиентский чат (иначе клиент попал бы в парсер ника)."""
    db = FakeDb()
    t = transport_for(db)
    await tap(t, KIND_OWNER, "add")

    await t._on_message(FakeMsg("ищу квартиру", chat_id="777"))

    assert t._channels.calls == 1
    assert db.managers == {}
