"""Тесты самозаявки «👤 Я менеджер»: профиль, заявка, решение владельца, края."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fakes import (
    MANAGER,
    OWNER,
    SECRET,
    FakeCallback,
    FakeDb,
    FakeMsg,
    FakeSender,
    fake_client,
    make_settings,
    make_transport,
    queued_notices,
)

from agent_core.ui import tokens
from realty_agent.tg import handlers
from realty_agent.ui import menu
from realty_agent.ui.kinds import KIND_APPLY, KIND_APPLY_NO, KIND_APPLY_OK, KIND_APPLY_PICK, KIND_OWNER

CLIENT = "777"


def transport_for(db, **settings):
    t = make_transport(db, settings=make_settings(**settings) if settings else None)
    t._limiter._now = lambda: 1000.0
    t._client = fake_client()
    return t


def texts(event) -> str:
    return "\n".join(item[0] or "" for item in event.sent)


def signed(kind: str, arg: str = "", *, chat_id: str = OWNER) -> str:
    return tokens.sign(SECRET, chat_id=chat_id, kind=kind, arg=arg)


async def apply_from(transport, *, chat_id: str = CLIENT, sender="default", private: bool = True,
                     sender_raises=None) -> FakeMsg:
    event = FakeMsg(menu.label(menu.MENU_APPLY), chat_id=chat_id, private=private,
                    sender=sender, sender_raises=sender_raises)
    await transport._on_message(event)
    return event


# ---------- подача заявки ----------

async def test_apply_creates_request_with_profile_username():
    local = FakeDb()
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="applicant", bot=False))
    event = await apply_from(t, sender=FakeSender(user_id=777, username="applicant"))

    assert len(local.requests) == 1
    assert local.requests[0]["username"] == "@applicant"
    assert local.requests[0]["chat_id"] == "777"
    assert handlers.APPLY_OK.format(nick="applicant") in texts(event)
    assert t._channels.calls == 0, "самозаявка не должна стоить LLM-хода"


async def test_apply_without_username_offers_to_create():
    local = FakeDb()
    t = transport_for(local)
    event = await apply_from(t, sender=FakeSender(username=None))

    assert local.requests == [], "без ника заявка не создаётся"
    assert handlers.APPLY_NO_USERNAME in texts(event)
    assert event.sent[0][1], "нужна кнопка «🔄 Проверить снова»"


async def test_apply_profile_none_falls_back_to_get_entity():
    local = FakeDb()
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="applicant", bot=False))
    await apply_from(t, sender=None)          # get_sender вернул None ⇒ читаем профиль через get_entity

    assert len(local.requests) == 1, "фолбэк обязан создать заявку"
    assert t._client.resolved, "профиль читался через get_entity"


async def test_apply_profile_failure_is_soft():
    local = FakeDb()
    t = transport_for(local)
    t._client = fake_client(raises=ValueError("Request was unsuccessful 3 time(s)"))
    event = await apply_from(t, sender=None)

    assert local.requests == [], "сбой чтения профиля = fail-closed"
    assert handlers.APPLY_PROFILE_FAIL in texts(event)


async def test_apply_refused_for_owner_and_manager():
    local = FakeDb(managers={MANAGER: {"name": "petr", "active": True, "ready": True}})
    t = transport_for(local)
    owner_event = await apply_from(t, chat_id=OWNER)
    manager_event = await apply_from(t, chat_id=MANAGER)

    assert local.requests == []
    assert handlers.APPLY_OWNER in texts(owner_event)
    assert handlers.APPLY_ALREADY_MANAGER in texts(manager_event)


async def test_apply_twice_does_not_duplicate():
    local = FakeDb()
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="applicant", bot=False))
    await apply_from(t, sender=FakeSender(user_id=777, username="applicant"))
    second = await apply_from(t, sender=FakeSender(user_id=777, username="applicant"))

    assert len(local.requests) == 1, "одна ожидающая заявка на чат"
    assert handlers.APPLY_PENDING in texts(second)


async def test_apply_from_group_is_refused():
    local = FakeDb()
    t = transport_for(local)
    await apply_from(t, private=False)

    assert local.requests == [], "заявка только из личного чата"


async def test_apply_refused_for_channel_sender():
    local = FakeDb()
    t = transport_for(local)
    event = await apply_from(t, sender=FakeSender(username="some_channel", broadcast=True))

    assert local.requests == []
    assert handlers.APPLY_NOT_HUMAN in texts(event)


async def test_apply_requires_consent():
    local = FakeDb(consent=False)
    t = transport_for(local)
    event = await apply_from(t)

    assert local.requests == [], "клиентский путь ⇒ сначала согласие"
    assert handlers.CONSENT_TEXT in texts(event)


# ---------- перепроверка профиля ----------

async def test_check_again_creates_request_when_username_appeared():
    local = FakeDb()
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="applicant", bot=False))
    event = FakeCallback(signed(KIND_APPLY, "check", chat_id=CLIENT), chat_id=CLIENT,
                         sender=FakeSender(user_id=777, username="applicant"))
    await t._on_callback(event)

    assert len(local.requests) == 1
    assert handlers.APPLY_OK.format(nick="applicant") in texts(event)


async def test_check_again_without_username_repeats_instruction():
    local = FakeDb()
    t = transport_for(local)
    event = FakeCallback(signed(KIND_APPLY, "check", chat_id=CLIENT), chat_id=CLIENT,
                         sender=FakeSender(username=None))
    await t._on_callback(event)

    assert local.requests == []
    assert handlers.APPLY_NO_USERNAME in texts(event)


# ---------- владелец: счётчик, список, решение ----------

async def test_owner_submenu_shows_requests_counter():
    local = FakeDb(requests=[{"id": 1, "chat_id": "777", "username": "@applicant",
                              "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_MANAGERS), chat_id=OWNER)
    await t._on_message(event)

    labels = [getattr(button, "text", button) for row in event.sent[0][1] for button in row]
    assert any("Заявки в менеджеры (1)" in str(label) for label in labels)


async def test_owner_sees_requests_list():
    local = FakeDb(requests=[{"id": 5, "chat_id": "777", "username": "@applicant", "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_REQUESTS), chat_id=OWNER)
    await t._on_message(event)

    assert "@applicant" in texts(event)
    args = [button.type.data.decode() for row in event.sent[0][1] for button in row]
    assert any(":apk:5:" in item for item in args), "кнопка заявки подписана и знает id"


async def test_owner_approves_and_applicant_becomes_ready_manager():
    local = FakeDb(requests=[{"id": 7, "chat_id": "777", "username": "@applicant", "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    sent: list[tuple] = []

    class Client:
        def is_connected(self) -> bool:
            return True

        async def send_message(self, chat_id, text, buttons=None):
            sent.append((chat_id, text))

        async def get_entity(self, target):
            return SimpleNamespace(id=int(target), username="applicant", bot=False)

    t._client = Client()
    event = FakeCallback(signed(KIND_APPLY_OK, "7"), chat_id=OWNER)
    await t._on_callback(event)

    assert local.managers["777"]["active"] is True
    assert local.managers["777"]["ready"] is True, "заявитель уже в боте ⇒ готов сразу"
    assert any(action == "manager_approve" for _, action, _ in local.actions)
    assert any(chat == 777 for chat, _ in sent), "заявителю уходит подтверждение"
    assert any(handlers.APPLY_APPROVED in text for _, text in sent)


async def test_owner_reject_and_reapply():
    local = FakeDb(requests=[{"id": 8, "chat_id": "777", "username": "@applicant", "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="applicant", bot=False))
    await t._on_callback(FakeCallback(signed(KIND_APPLY_NO, "8"), chat_id=OWNER))

    assert local.managers == {}, "отказ не создаёт менеджера"
    assert any(action == "manager_reject" for _, action, _ in local.actions)

    await apply_from(t, sender=FakeSender(user_id=777, username="applicant"))
    assert any(row["status"] == "pending" for row in local.requests), "повторная подача разрешена"


async def test_double_tap_on_decided_request_is_answered():
    local = FakeDb(requests=[{"id": 9, "chat_id": "777", "username": "@applicant", "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    first = FakeCallback(signed(KIND_APPLY_OK, "9"), chat_id=OWNER)
    await t._on_callback(first)
    second = FakeCallback(signed(KIND_APPLY_OK, "9"), chat_id=OWNER)
    await t._on_callback(second)

    assert handlers.APPLY_NOT_FOUND in [item for item in second.answers if item]


async def test_manager_cannot_decide_requests():
    local = FakeDb(managers={MANAGER: {"name": "petr", "active": True, "ready": True}},
                   requests=[{"id": 11, "chat_id": "777", "username": "@applicant", "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    event = FakeCallback(signed(KIND_APPLY_OK, "11", chat_id=MANAGER), chat_id=MANAGER)
    await t._on_callback(event)

    assert local.requests[0]["status"] == "pending", "менеджер не решает заявки (H4)"
    assert handlers.MANAGERS_ONLY_OWNER in [item for item in event.answers if item]


async def test_owner_card_has_no_pii():
    local = FakeDb()
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="applicant", bot=False))
    await apply_from(t, sender=FakeSender(user_id=777, username="applicant"))

    # Карточка заявки идёт через очередь: проверяем её содержимое там, а прямую отправку — как
    # отсутствие (иначе «нет ПДн» проверялось бы по пустому списку и всегда проходило).
    assert t._client.sent == [], "карточка заявки должна идти через очередь"
    queued = [text for _, text in queued_notices(t)]
    assert any("@applicant" in text for text in queued), "владельцу уходит карточка заявки"
    assert all("+7" not in text for text in queued), "в уведомлении нет телефонов"


async def test_requests_row_in_submenu_opens_list():
    """Строка подменю — inline-действие: reply-метка (`m`) здесь молча гасится."""
    local = FakeDb(requests=[{"id": 3, "chat_id": "777", "username": "@applicant", "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    event = FakeCallback(signed(KIND_OWNER, "requests"), chat_id=OWNER)
    await t._on_callback(event)

    assert "@applicant" in texts(event), "тап обязан открыть список заявок"


@pytest.mark.parametrize("kind,arg", [
    (KIND_OWNER, "requests"), (KIND_OWNER, "add"), (KIND_OWNER, "list"), (KIND_OWNER, "del"),
    (KIND_APPLY_PICK, "3"), (KIND_APPLY_OK, "3"), (KIND_APPLY_NO, "3"),
])
async def test_every_inline_button_is_dispatched(kind, arg):
    """Гард класса ошибок: каждое inline-действие подменю/списка/карточки даёт реакцию, а не тишину."""
    local = FakeDb(requests=[{"id": 3, "chat_id": "777", "username": "@applicant", "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    event = FakeCallback(signed(kind, arg), chat_id=OWNER)
    await t._on_callback(event)

    assert event.sent or event.answers, f"тап {kind}:{arg} не дал ни ответа, ни тоста (молчаливый no-op)"


# ---------- ник заявителя тоже с одним «@» ----------

@pytest.mark.parametrize("stored", ["applicant", "@applicant", "@@applicant"])
async def test_requests_list_shows_single_at(stored):
    """Список заявок (текст и кнопка) показывает ровно один «@» при любом формате хранения."""
    local = FakeDb(requests=[{"id": 5, "chat_id": "777", "username": stored, "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_REQUESTS), chat_id=OWNER)
    await t._on_message(event)

    body = texts(event)
    assert "@applicant" in body and "@@" not in body
    labels = [button.text for row in event.sent[0][1] for button in row]
    assert any(item.startswith("@applicant · ") for item in labels)
    assert all("@@" not in item for item in labels)


@pytest.mark.parametrize("stored", ["applicant", "@applicant", "@@applicant"])
async def test_approved_manager_is_stored_canonically(stored):
    """Канон записи: в `managers.name` лежит ник БЕЗ ведущего «@» (оба пути дают один формат)."""
    local = FakeDb(requests=[{"id": 7, "chat_id": "777", "username": stored, "status": "pending",
                              "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}])
    t = transport_for(local)
    await t._on_callback(FakeCallback(signed(KIND_APPLY_OK, "7"), chat_id=OWNER))

    assert local.managers["777"]["name"] == "applicant"
