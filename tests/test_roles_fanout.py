"""Роли, права и фан-аут карточек."""
from __future__ import annotations

import pytest
from fakes import (
    MANAGER,
    OWNER,
    SECRET,
    FakeDb,
    FakeMsg,
    fake_client,
    make_settings,
    make_transport,
)

from realty_agent.dialog import Booking
from realty_agent.tg import handlers
from realty_agent.tg.callbacks import verify_callback
from realty_agent.tg.owner_flows import manager_state, normalize_username
from realty_agent.tg.roles import may, resolve_role

OTHER = "333"
# Роли — строки ролей, чаты — id из фейков (OWNER/MANAGER — это chat_id "111"/"222").
OWNER_ROLE, MANAGER_ROLE, CLIENT_ROLE = "owner", "manager", "client"


def transport_for(db, settings=None):
    t = make_transport(db, settings=settings)
    t._limiter._now = lambda: 1000.0
    t._client = fake_client()
    return t


def booking() -> Booking:
    return Booking(id="9", platform="tg", chat_id="777", listing_id="L-1",
                   slot="2026-10-05T10:00", client_name="Аня", client_phone="+79990000000")


def _data(button) -> str:
    """Telethon 1.45: данные inline-кнопки — в `.type.data` (у reply-кнопок их нет вовсе)."""
    payload = getattr(button, "data", None) or getattr(button.type, "data", None)
    return payload.decode() if payload else ""


def _has_inline_buttons(buttons) -> bool:
    """Inline-кнопки (карточка ✅/❌) отличаем от reply-клавиатуры (меню): у inline есть `.type.data`."""
    if not buttons:
        return False
    flat = buttons if not isinstance(buttons[0], (list, tuple)) else [b for row in buttons for b in row]
    return any(getattr(getattr(button, "type", None), "data", None) for button in flat)


def queued_cards(transport) -> list[tuple]:
    """Только КАРТОЧКИ из очереди: (chat_id, text, buttons-дескрипторы).

    Карточка не уходит прямым вызовом, а ставится в очередь с кнопками-ДЕСКРИПТОРАМИ (готовые объекты
    Telethon в очередь не положить). Признак карточки — наличие `buttons` в payload: у меню и служебных
    уведомлений их нет. Сама карточка проходит те же проверки (дедуп, подпись под каждого, пропуск
    неготовых).
    """
    return [(row["payload"]["chat_id"], row["payload"]["text"], row["payload"].get("buttons") or [])
            for row in transport._db.outbox_rows if row["payload"].get("buttons")]


def cards_only(client) -> list[tuple]:
    """Карточки, ушедшие ПРЯМЫМ вызовом: теперь их быть не должно — это сторож, а не источник."""
    return [item for item in client.sent if _has_inline_buttons(item[2])]


def button_data(buttons) -> list[str]:
    """Плоский список (карточка заявки) или ряды (reply/inline-раскладки)."""
    if buttons and not isinstance(buttons[0], (list, tuple)):
        return [_data(button) for button in buttons]
    return [_data(button) for row in buttons for button in row]


# ---------- нормализация ника ----------

@pytest.mark.parametrize("raw,expected", [
    ("ivanov", "@ivanov"),
    ("Ivanov", "@ivanov"),
    ("@Ivanov", "@ivanov"),
    ("t.me/ivanov", "@ivanov"),
    ("https://t.me/ivanov", "@ivanov"),
    ("https://t.me/@Ivanov", "@ivanov"),
    ("  @ivanov  ", "@ivanov"),
])
def test_normalize_username_forms(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ["ab", "nick!", "@", "@iv an", "+7 900 000-00-00", "его зовут Иван", ""])
def test_normalize_username_rejects_garbage(raw):
    assert normalize_username(raw) is None


def test_manager_state_reflects_ready():
    assert manager_state({"active": True, "ready": True}) == "готов"
    assert manager_state({"active": True, "ready": False}) == "ждёт /start"
    assert manager_state({"active": False, "ready": True}) == "отключён"


# ---------- роли и права ----------

async def test_resolve_role_prefers_owner_then_legacy_then_db():
    settings = make_settings()
    db = FakeDb(managers={OTHER: {"active": True, "ready": True}})

    assert await resolve_role(OWNER, settings=settings, db=db) == OWNER_ROLE
    assert await resolve_role(MANAGER, settings=settings, db=db) == MANAGER_ROLE, "legacy-чат = менеджер"
    assert await resolve_role(OTHER, settings=settings, db=db) == MANAGER_ROLE
    assert await resolve_role("777", settings=settings, db=db) == CLIENT_ROLE


async def test_resolve_role_is_fail_closed_without_owner():
    settings = make_settings(telegram_owner_chat_id="")
    db = FakeDb(managers={OTHER: {"active": True, "ready": True}})

    assert await resolve_role(MANAGER, settings=settings, db=db) == MANAGER_ROLE
    assert await resolve_role("777", settings=settings, db=db) == CLIENT_ROLE


async def test_resolve_role_survives_db_outage():
    class BrokenDb(FakeDb):
        async def is_active_manager(self, chat_id: str) -> bool:
            raise RuntimeError("db down")

    settings = make_settings()
    assert await resolve_role(OWNER, settings=settings, db=BrokenDb()) == OWNER_ROLE
    assert await resolve_role("777", settings=settings, db=BrokenDb()) == CLIENT_ROLE, "сбой БД ≠ права овнера"


async def test_inactive_manager_loses_rights():
    settings = make_settings()
    db = FakeDb(managers={OTHER: {"active": False, "ready": True}})
    assert await resolve_role(OTHER, settings=settings, db=db) == CLIENT_ROLE


@pytest.mark.parametrize("capability,owner_ok,manager_ok", [
    ("view_pending", True, True),
    ("confirm", True, True),
    ("pause", True, False),
    ("resume", True, False),
    ("report", True, False),
    ("status", True, False),
    ("manage_managers", True, False),
    ("export", True, False),
])
def test_may_gates(capability, owner_ok, manager_ok):
    assert may(OWNER_ROLE, capability) is owner_ok
    assert may(MANAGER_ROLE, capability) is manager_ok
    assert may(CLIENT_ROLE, capability) is False


# ---------- согласие и лимит: по входу, а не по роли ----------

async def test_client_gate_requires_consent_for_owner():
    t = transport_for(FakeDb(consent=False))
    event = FakeMsg("хочу посмотреть квартиру", chat_id=OWNER)
    await t._on_message(event)

    assert t._channels.calls == 0, "овнер в клиентском пути тоже проходит согласие"
    assert handlers.CONSENT_TEXT in "\n".join(item[0] or "" for item in event.sent)


async def test_owner_menu_tap_does_not_need_consent():
    t = transport_for(FakeDb(consent=False, pending=[]))
    event = FakeMsg("📋 Заявки", chat_id=OWNER)
    await t._on_message(event)

    assert handlers.CONSENT_TEXT not in "\n".join(item[0] or "" for item in event.sent)


# ---------- фан-аут карточек ----------

async def test_fanout_dedup_when_owner_equals_manager():
    """Живой `.env`: TELEGRAM_MANAGER_CHAT_ID == TELEGRAM_OWNER_CHAT_ID — одна карточка, не три."""
    db = FakeDb(managers={OWNER: {"active": True, "ready": True}})
    t = transport_for(db, settings=make_settings(telegram_manager_chat_id=OWNER))

    await t._notify_manager(booking())

    assert cards_only(t._client) == [], "карточка должна идти через очередь, а не прямой отправкой"
    cards = queued_cards(t)
    assert len(cards) == 1, "ровно одна карточка (дедуп адресатов)"
    assert cards[0][0] == OWNER


async def test_fanout_signs_per_recipient():
    db = FakeDb(managers={OTHER: {"active": True, "ready": True}})
    t = transport_for(db)

    await t._notify_manager(booking())

    assert {chat for chat, _, _ in queued_cards(t)} == {OWNER, MANAGER, OTHER}
    for chat, _text, buttons in queued_cards(t):
        # Кнопки из очереди собираются тем же построителем, что и прямой путь: подпись обязана
        # проходить настоящую проверку для СВОЕГО чата и отвергаться в чужом.
        data = [item["action"] for item in buttons]
        assert data, "у каждой копии свои подписанные кнопки"
        signed = transport_for(t._db)._callback_buttons(
            booking_id=9, chat_id=chat, actions=[(item["action"], item["label"]) for item in buttons])
        assert all(verify_callback(SECRET, button.type.data.decode(), chat) is not None
                   for button in signed), "подпись из очереди обязана проходить проверку"
        other = MANAGER if chat != MANAGER else OWNER
        assert verify_callback(SECRET, signed[0].type.data.decode(), other) is None, \
            "подпись работает только в своём чате"


async def test_fanout_skips_managers_without_start():
    db = FakeDb(managers={OTHER: {"active": True, "ready": False}})
    t = transport_for(db)

    await t._notify_manager(booking())

    chats = {chat for chat, _, _ in queued_cards(t)}
    assert OTHER not in chats, "менеджер без /start карточку не получает"
    assert OWNER in chats


async def test_fanout_keeps_legacy_manager():
    db = FakeDb()
    t = transport_for(db, settings=make_settings(telegram_manager_chat_id=OTHER))

    await t._notify_manager(booking())

    assert OTHER in {chat for chat, _, _ in queued_cards(t)}


async def test_fanout_without_client_logs_and_continues():
    t = transport_for(FakeDb())
    t._client = None

    await t._notify_manager(booking())     # не должно падать: карточка — не путь клиента


async def test_fanout_marks_ready_manager_after_start():
    db = FakeDb(managers={OTHER: {"name": "petr", "active": True, "ready": False}})
    t = transport_for(db)
    await t._on_message(FakeMsg("/start", chat_id=OTHER))

    await t._notify_manager(booking())

    assert OTHER in {chat for chat, _, _ in queued_cards(t)}
