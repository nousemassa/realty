"""Управление менеджерами через меню владельца."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fakes import (
    MANAGER,
    OWNER,
    SECRET,
    FakeCallback,
    FakeDb,
    FakeMsg,
    fake_client,
    make_settings,
    make_transport,
)

from agent_core.ui import tokens
from realty_agent.tg import handlers
from realty_agent.ui import menu
from realty_agent.ui.kinds import KIND_OWNER, KIND_OWNER_OFF, KIND_OWNER_PICK


def signed(kind: str, arg: str = "", chat_id: str = OWNER) -> str:
    return tokens.sign(SECRET, chat_id=chat_id, kind=kind, arg=arg)


def texts(event) -> str:
    return "\n".join(item[0] or "" for item in event.sent)


def transport_for(db, **settings_kwargs):
    """Транспорт на КОНКРЕТНОЙ БД: `OwnerFlows` держит ссылку на db с момента создания."""
    t = make_transport(db, settings=make_settings(**settings_kwargs) if settings_kwargs else None)
    t._limiter._now = lambda: 1000.0
    return t


async def tap(transport, kind: str, arg: str = "", *, chat_id: str = OWNER):
    event = FakeCallback(signed(kind, arg, chat_id=chat_id), chat_id=chat_id)
    await transport._on_callback(event)
    return event


async def test_managers_menu_opens_submenu():
    owner_transport = transport_for(FakeDb())
    event = FakeMsg(menu.label(menu.MENU_OWNER_MANAGERS), chat_id=OWNER)
    await owner_transport._on_message(event)

    assert owner_transport._channels.calls == 0
    assert handlers.MANAGERS_TITLE in texts(event)
    labels = [button.text for row in event.sent[0][1] for button in row]
    assert "➕ Добавить" in labels and "🗑 Удалить" in labels and "❌ Отмена" in labels


async def test_add_manager_by_username():
    local = FakeDb()
    owner_transport = transport_for(local)
    owner_transport._client = fake_client(user=SimpleNamespace(id=777, username="lenavik", bot=False))

    await tap(owner_transport, KIND_OWNER, "add")
    event = FakeMsg("@lenavik", chat_id=OWNER)
    await owner_transport._on_message(event)

    assert local.managers["777"]["name"] == "lenavik"
    assert local.managers["777"]["ready"] is False, "готовность появится только после /start"
    assert handlers.MANAGERS_ADDED.format(nick="lenavik") in texts(event)
    assert owner_transport._client.sent[0][0] == 777, "инструкция уходит менеджеру сразу"
    assert local.owner_flow is None, "успех снимает флоу"


async def test_add_manager_without_at_and_mixed_case():
    local = FakeDb()
    owner_transport = transport_for(local)
    owner_transport._client = fake_client(user=SimpleNamespace(id=777, username="Nick", bot=False))

    await tap(owner_transport, KIND_OWNER, "add")
    await owner_transport._on_message(FakeMsg("телеграм ник: Nick", chat_id=OWNER))

    assert local.managers == {}, "строка с лишним текстом — не ник"


async def test_add_manager_bad_format_keeps_flow():
    local = FakeDb()
    owner_transport = transport_for(local)
    owner_transport._client = fake_client()

    await tap(owner_transport, KIND_OWNER, "add")
    event = FakeMsg("+7 900 000-00-00", chat_id=OWNER)
    await owner_transport._on_message(event)

    assert local.managers == {}
    assert handlers.MANAGERS_FORMAT_ERR in texts(event)
    assert local.owner_flow is not None, "ошибка формата не сбрасывает флоу — можно повторить"


async def test_add_manager_username_not_found():
    local = FakeDb()
    owner_transport = transport_for(local)
    owner_transport._client = fake_client(raises=ValueError('No user has "nobody" as username'))

    await tap(owner_transport, KIND_OWNER, "add")
    event = FakeMsg("@nobody", chat_id=OWNER)
    await owner_transport._on_message(event)

    assert local.managers == {}
    assert handlers.MANAGERS_NO_NICK in texts(event)


async def test_add_manager_rejects_bot():
    local = FakeDb()
    owner_transport = transport_for(local)
    owner_transport._client = fake_client(user=SimpleNamespace(id=555, username="helper_bot", bot=True))

    await tap(owner_transport, KIND_OWNER, "add")
    event = FakeMsg("@helper_bot", chat_id=OWNER)
    await owner_transport._on_message(event)

    assert local.managers == {}, "бот не жмёт ✅/❌ и не получает прав"
    assert handlers.MANAGERS_NOT_USER in texts(event)


async def test_add_manager_falls_back_when_bot_cannot_write_first():
    local = FakeDb()
    owner_transport = transport_for(local)
    client = fake_client(user=SimpleNamespace(id=777, username="lenavik", bot=False))
    client.send_message = _failing_send
    owner_transport._client = client

    await tap(owner_transport, KIND_OWNER, "add")
    event = FakeMsg("@lenavik", chat_id=OWNER)
    await owner_transport._on_message(event)

    assert local.managers["777"]["name"] == "lenavik"
    assert "/start" in texts(event), "владелец должен узнать, что менеджеру нужно нажать /start"


async def _failing_send(*args, **kwargs):
    raise RuntimeError("bot cannot initiate conversation")


async def test_add_manager_rejects_existing():
    local = FakeDb(managers={"777": {"name": "lenavik", "active": True, "ready": True}})
    owner_transport = transport_for(local)
    owner_transport._client = fake_client(user=SimpleNamespace(id=777, username="lenavik", bot=False))

    await tap(owner_transport, KIND_OWNER, "add")
    event = FakeMsg("@lenavik", chat_id=OWNER)
    await owner_transport._on_message(event)

    assert handlers.MANAGERS_EXISTS.format(nick="lenavik") in texts(event)


async def test_readd_reactivates_without_duplicate():
    local = FakeDb(managers={"777": {"name": "lenavik", "active": False, "ready": True}})
    owner_transport = transport_for(local)
    owner_transport._client = fake_client(user=SimpleNamespace(id=777, username="lenavik", bot=False))

    await tap(owner_transport, KIND_OWNER, "add")
    await owner_transport._on_message(FakeMsg("@lenavik", chat_id=OWNER))

    assert len(local.managers) == 1, "повторное добавление — реактивация, не вторая строка"
    assert local.managers["777"]["active"] is True
    assert local.managers["777"]["ready"] is False, "пусть заново нажмёт /start"


async def test_list_shows_states():
    local = FakeDb(managers={
        "1": {"name": "ivan", "active": True, "ready": True},
        "2": {"name": "petr", "active": True, "ready": False},
        "3": {"name": "olga", "active": False, "ready": True},
    })
    owner_transport = transport_for(local)
    event = await tap(owner_transport, KIND_OWNER, "list")

    body = texts(event)
    assert "@ivan — готов" in body
    assert "@petr — ждёт /start" in body
    assert "@olga — отключён" in body


async def test_list_empty_is_honest():
    owner_transport = transport_for(FakeDb())
    event = await tap(owner_transport, KIND_OWNER, "list")

    assert handlers.MANAGERS_EMPTY in texts(event)


async def test_remove_needs_confirmation_then_deactivates():
    local = FakeDb(managers={
        "777": {"name": "lenavik", "active": True, "ready": True},
        "888": {"name": "petr", "active": True, "ready": True},
    })
    owner_transport = transport_for(local)

    pick = await tap(owner_transport, KIND_OWNER, "del")
    assert handlers.MANAGERS_REMOVE_ASK.format(nick="lenavik") not in texts(pick), "сначала только выбор"

    ask = await tap(owner_transport, KIND_OWNER_PICK, "777")
    assert handlers.MANAGERS_REMOVE_ASK.format(nick="lenavik") in texts(ask)
    assert local.managers["777"]["active"] is True, "без подтверждения права не снимаются"

    done = await tap(owner_transport, KIND_OWNER_OFF, "777")
    assert local.managers["777"]["active"] is False
    assert local.managers["777"]["ready"] is True, "строка остаётся: аудит и обратимость"
    assert handlers.MANAGERS_REMOVED.format(nick="lenavik") in texts(done)
    assert (("777"), "manager_remove", "manager") in local.actions


async def test_remove_last_manager_warns():
    local = FakeDb(managers={"777": {"name": "lenavik", "active": True, "ready": True}})
    owner_transport = transport_for(local)
    ask = await tap(owner_transport, KIND_OWNER_PICK, "777")

    assert handlers.MANAGERS_REMOVE_LAST in texts(ask)


async def test_remove_without_active_managers():
    owner_transport = transport_for(FakeDb(managers={"777": {"name": "lenavik", "active": False, "ready": True}}))
    event = await tap(owner_transport, KIND_OWNER, "del")

    assert handlers.MANAGERS_NOTHING_TO_REMOVE in texts(event)


async def test_manager_cannot_manage_managers():
    local = FakeDb(managers={MANAGER: {"name": "petr", "active": True, "ready": True}})
    owner_transport = transport_for(local)
    owner_transport._client = fake_client(user=SimpleNamespace(id=777, username="lenavik", bot=False))

    event = await tap(owner_transport, KIND_OWNER, "add", chat_id=MANAGER)

    assert local.managers.get("777") is None
    assert local.owner_flow is None
    assert handlers.MANAGERS_ONLY_OWNER in [item for item in event.answers if item]


async def test_client_cannot_manage_managers():
    local = FakeDb()
    owner_transport = transport_for(local)
    event = await tap(owner_transport, KIND_OWNER, "add", chat_id="777")

    assert local.owner_flow is None
    assert handlers.MANAGERS_ONLY_OWNER in [item for item in event.answers if item]


async def test_group_chat_owner_has_no_manager_rights():
    local = FakeDb()
    owner_transport = transport_for(local)
    event = FakeCallback(signed(KIND_OWNER, "add"), chat_id=OWNER)
    event.is_private = False

    await owner_transport._on_callback(event)

    assert local.owner_flow is None
    assert handlers.MANAGERS_ONLY_OWNER in [item for item in event.answers if item]


async def test_unsigned_owner_tap_is_ignored():
    local = FakeDb()
    owner_transport = transport_for(local)
    event = FakeCallback("c:own:add:deadbeefdeadbeef", chat_id=OWNER)
    await owner_transport._on_callback(event)

    assert local.owner_flow is None
    assert event.sent == []


async def test_manager_start_marks_ready():
    local = FakeDb(managers={MANAGER: {"name": "petr", "active": True, "ready": False}})
    owner_transport = transport_for(local)
    event = FakeMsg("/start", chat_id=MANAGER)
    await owner_transport._on_message(event)

    assert local.managers[MANAGER]["ready"] is True


# ---------- ник никогда не показывается как «@@nick» ----------

@pytest.mark.parametrize("stored", ["purkautuva", "@purkautuva", "@@purkautuva"])
async def test_manager_list_never_doubles_at(stored):
    """Три формата хранения (легаси + канон) → в списке ровно один «@»."""
    owner_transport = transport_for(FakeDb(managers={"1": {"name": stored, "active": True, "ready": True}}))

    body = texts(await tap(owner_transport, KIND_OWNER, "list"))

    assert "@purkautuva — готов" in body
    assert "@@" not in body


@pytest.mark.parametrize("stored", ["purkautuva", "@purkautuva", "@@purkautuva"])
async def test_manager_pick_and_confirm_use_single_at(stored):
    """Кнопка удаления и текст подтверждения — с одним «@» при любом формате в БД."""
    local = FakeDb(managers={
        "777": {"name": stored, "active": True, "ready": True},
        "888": {"name": "petr", "active": True, "ready": True},   # второй активный ⇒ путь «Убрать @nick?»
    })
    owner_transport = transport_for(local)

    picker = await tap(owner_transport, KIND_OWNER, "del")
    labels = [button.text for row in picker.sent[0][1] for button in row]
    assert "🗑 @purkautuva" in labels
    assert all("@@" not in item for item in labels)

    ask = await tap(owner_transport, KIND_OWNER_PICK, "777")
    body = texts(ask)
    assert handlers.MANAGERS_REMOVE_ASK.format(nick="purkautuva") in body
    assert "@@" not in body

    done = await tap(owner_transport, KIND_OWNER_OFF, "777")
    removed = texts(done)
    assert handlers.MANAGERS_REMOVED.format(nick="purkautuva") in removed
    assert "@@" not in removed
