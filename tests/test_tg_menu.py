"""Маршрутизация reply-меток и роль-гейт.

Обещания плана, которые здесь проверяются: тап по меню не доходит до LLM; метка или текст без
согласия не уводит хода; лимит списывается ровно один раз; овнер/менеджер получают очередь заявок,
клиент — нет; просмотр очереди пишется в аудит.
"""
from __future__ import annotations

import pytest
from fakes import (
    MANAGER,
    OWNER,
    FakeDb,
    FakeMsg,
    make_settings,
)

from realty_agent.tg import handlers
from realty_agent.ui import menu

CLIENT = "777"

PENDING_ROW = {
    "id": 1,
    "client_name": "Аня",
    "client_phone": "+79990000000",
    "listing_id": "L-1",
    "slot": "2026-10-05T10:00+03:00",
}


def label(code: str, count: int | None = None) -> str:
    """Метка reply-меню — то, что реально приходит от Telegram (кнопка = обычный текст)."""
    return menu.label(code, count)


def body_text(event: FakeMsg) -> str:
    return "\n".join(item[0] or "" for item in event.sent)


@pytest.mark.parametrize("code", [menu.MENU_OBJECTS, menu.MENU_BOOK, menu.MENU_POLICY, menu.MENU_MANAGER])
async def test_client_label_does_not_reach_agent(transport, db, code):
    transport._db = db
    event = FakeMsg(label(code))
    await transport._on_message(event)

    assert transport._channels.calls == 0, "тап по меню не должен стоить LLM-хода"
    assert event.sent, "клиент должен получить ответ"


async def test_objects_label_lists_catalog(transport, db):
    transport._db = db
    event = FakeMsg(label(menu.MENU_OBJECTS))
    await transport._on_message(event)

    assert transport._channels.calls == 0
    text = body_text(event)
    assert "ул. Пушкина, 10" in text
    assert "L-1" not in text, "внутренние идентификаторы клиенту не показываем"


async def test_book_label_without_listing_shows_catalog(transport, db):
    """Каталог идёт с шапкой и подсказкой («напишите номер»), без требования номера у пустого списка."""
    transport._db = db
    event = FakeMsg(label(menu.MENU_BOOK))
    await transport._on_message(event)

    assert transport._channels.calls == 0
    text = body_text(event)
    assert handlers.OBJECTS_TITLE in text
    assert handlers.OBJECTS_HINT in text


async def test_book_label_with_listing_asks_day(transport, db):
    local = FakeDb()
    local.conversations = {"777": {"step": "listing", "continuum": {"listing_id": "L-2"}}}
    transport._db = local
    event = FakeMsg(label(menu.MENU_BOOK))
    await transport._on_message(event)

    assert transport._channels.calls == 0
    text = body_text(event)
    assert "пр. Мира, 3" in text
    assert "день и время" in text


async def test_policy_label_answers_with_privacy_text(transport, db):
    transport._db = db
    event = FakeMsg(label(menu.MENU_POLICY))
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert handlers.PRIVACY_TEXT in body_text(event)


async def test_manager_label_without_contact_gives_honest_fallback(transport, db):
    transport._db = db
    event = FakeMsg(label(menu.MENU_MANAGER))
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert "Контакт пока не указан" in body_text(event)


async def test_manager_label_uses_configured_contact(db):
    from fakes import make_transport

    local = make_transport(db, settings=make_settings(manager_contact="+7 900 000-00-00"))
    local._limiter._now = lambda: 1000.0
    event = FakeMsg(label(menu.MENU_MANAGER))
    await local._on_message(event)

    assert "+7 900 000-00-00" in body_text(event)


async def test_label_without_consent_asks_consent(transport):
    transport._db = FakeDb(consent=False)
    event = FakeMsg(label(menu.MENU_OBJECTS))
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert handlers.CONSENT_TEXT in body_text(event)


async def test_paused_bot_refuses_reserved_label(transport):
    transport._db = FakeDb(bot_mode="paused")
    event = FakeMsg(label(menu.MENU_BOOK))
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert handlers.PAUSED_TEXT in body_text(event)


async def test_plain_text_still_reaches_agent(transport, db):
    transport._db = db
    event = FakeMsg("ищу двушку до 7 млн")
    await transport._on_message(event)

    assert transport._channels.calls == 1, "кнопки не отменяют текстовый путь"


async def test_agent_failure_answers_with_failover(transport, db):
    transport._db = db
    event = FakeMsg("ищу двушку")
    await transport._on_message(event)

    assert handlers.FAILOVER_TEXT in body_text(event)


async def test_limit_spent_exactly_once_per_message(transport, db):
    transport._db = db
    for _ in range(10):
        await transport._on_message(FakeMsg("привет"))
    assert transport._channels.calls == 10, "меню не должно съедать лимит дважды"

    event = FakeMsg("привет")
    await transport._on_message(event)
    assert transport._channels.calls == 10
    assert handlers.TOO_OFTEN_TEXT in body_text(event)


async def test_owner_gets_pending_counter_from_label(transport):
    local = FakeDb(pending=[dict(PENDING_ROW, id=index) for index in (1, 2, 3)])
    transport._db = local
    event = FakeMsg(label(menu.MENU_OWNER_BOOKINGS, count=3), chat_id=OWNER)
    await transport._on_message(event)

    assert transport._channels.calls == 0
    text = body_text(event)
    assert "Заявка #1" in text and "Заявка #3" in text


async def test_manager_gets_pending_from_label(transport):
    local = FakeDb(managers={MANAGER: {"active": True, "ready": True}}, pending=[PENDING_ROW])
    transport._db = local
    event = FakeMsg(label(menu.MENU_OWNER_BOOKINGS, count=1), chat_id=MANAGER)
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert "Аня" in body_text(event)


async def test_client_has_no_pending_access(transport, db):
    transport._db = db
    event = FakeMsg("/pending", chat_id=CLIENT)
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert handlers.VIEW_ONLY in body_text(event)


async def test_group_chat_is_not_owner(transport):
    transport._db = FakeDb()
    event = FakeMsg("/pending", chat_id=OWNER, private=False)
    await transport._on_message(event)

    assert handlers.VIEW_ONLY in body_text(event), "в групповом чате прав владельца нет"


async def test_pending_view_is_audited(transport):
    local = FakeDb(pending=[PENDING_ROW])
    transport._db = local
    await transport._on_message(FakeMsg("/pending", chat_id=OWNER))

    assert (OWNER, "view_pending", "owner") in local.actions, local.actions


async def test_booking_counter_is_bumped(transport, db):
    """Счётчик заявок — точка создания брони, а не транспорт (метрики для будущего отчёта)."""
    from realty_agent.dialog import Booking

    transport._db = db
    await transport._save_booking(
        Booking(id="", platform="tg", chat_id=CLIENT, listing_id="L-1", slot="2026-10-05T10:00",
                client_name="Аня", client_phone="+79990000000")
    )
    assert db.metrics.get("bookings_created") == 1


async def test_manager_start_shows_instruction_not_consent(transport):
    local = FakeDb(managers={MANAGER: {"active": True, "ready": False}})
    transport._db = local
    event = FakeMsg("/start", chat_id=MANAGER)
    await transport._on_message(event)

    assert local.managers[MANAGER]["ready"] is True, "после /start менеджер готов получать карточки"
    assert transport._channels.calls == 0
    text = body_text(event)
    assert handlers.MANAGER_INSTRUCTION in text
    assert handlers.CONSENT_TEXT not in text


async def test_owner_start_shows_owner_menu(transport, db):
    transport._db = db
    event = FakeMsg("/start", chat_id=OWNER)
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert handlers.OWNER_START_NOTE in body_text(event)


async def test_plain_client_start_asks_consent(transport):
    transport._db = FakeDb(consent=False)
    event = FakeMsg("/start", chat_id=CLIENT)
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert handlers.CONSENT_TEXT in body_text(event)


async def test_consent_word_mid_dialog_reaches_agent(transport):
    """«согласен» после согласия — обычный текст: ни глотать, ни дублировать согласие."""
    transport._db = FakeDb(consent=True)
    event = FakeMsg("согласен", chat_id=CLIENT)
    await transport._on_message(event)

    assert transport._db.consent_saves == 0
    assert transport._channels.calls == 1


async def test_owner_client_path_is_marked(transport):
    transport._db = FakeDb()
    event = FakeMsg(label(menu.MENU_OBJECTS), chat_id=OWNER)
    await transport._on_message(event)

    assert handlers.CLIENT_PATH_NOTICE in body_text(event)


async def test_unknown_command_is_silent(transport, db):
    transport._db = db
    event = FakeMsg("/unknown")
    await transport._on_message(event)

    assert transport._channels.calls == 0
    assert event.sent == []


# ---------- ряд «👁 Просмотры (N)» и честные счётчики ----------

def reply_labels(event: FakeMsg) -> list[str]:
    """Лейблы reply-кнопок последнего сообщения (Telethon 1.45: текст — в `.button.text`)."""
    return [b.button.text if hasattr(b, "button") else b.text
            for row in event.sent[-1][1] for b in row]


async def test_owner_start_menu_has_views_row(transport, db):
    """У администратора кнопка «Просмотры» есть уже в меню /start."""
    transport._db = db
    event = FakeMsg("/start", chat_id=OWNER)
    await transport._on_message(event)

    assert any("Просмотры" in label for label in reply_labels(event)), reply_labels(event)


async def test_manager_start_menu_has_views_row(transport, db):
    """У менеджера — тоже (с его собственным счётом)."""
    db.managers = {MANAGER: {"name": "petr", "active": True, "ready": False}}
    transport._db = db
    event = FakeMsg("/start", chat_id=MANAGER)
    await transport._on_message(event)

    assert any("Просмотры" in label for label in reply_labels(event)), reply_labels(event)


async def test_views_counter_is_not_capped(transport, db):
    """Счётчик считается запросом, а не длиной среза: срез «залипает» на 0/1."""
    db.bookings = [{"id": i, "status": "confirmed", "confirmed_by": OWNER,
                    "listing_id": "L-1", "slot": "2026-10-05T10:00+03:00"} for i in range(1, 4)]
    transport._db = db
    event = FakeMsg("/start", chat_id=OWNER)
    await transport._on_message(event)

    assert "👁 Просмотры (3)" in reply_labels(event), reply_labels(event)


async def test_views_counter_is_scoped_for_manager(transport, db):
    """Менеджер видит счёт только своих подтверждённых (confirmed_by)."""
    db.managers = {MANAGER: {"name": "petr", "active": True, "ready": True}}
    db.bookings = [{"id": 1, "status": "confirmed", "confirmed_by": MANAGER,
                    "listing_id": "L-1", "slot": "2026-10-05T10:00+03:00"},
                   {"id": 2, "status": "confirmed", "confirmed_by": OWNER,
                    "listing_id": "L-2", "slot": "2026-10-06T10:00+03:00"}]
    transport._db = db
    event = FakeMsg("/start", chat_id=MANAGER)
    await transport._on_message(event)

    assert "👁 Просмотры (1)" in reply_labels(event), reply_labels(event)


async def test_queue_counter_is_not_capped_by_list_limit(transport, db):
    """«📋 Заявки (N)» — истинная очередь, а не длина выборки с лимитом."""
    db.pending = [dict(PENDING_ROW, id=i) for i in range(1, 12)]
    transport._db = db
    event = FakeMsg("/start", chat_id=OWNER)
    await transport._on_message(event)

    assert any("Заявки (11)" in label for label in reply_labels(event)), reply_labels(event)


async def test_pause_toggle_menu_keeps_views_row(transport, db):
    """После ⏸/▶️ меню тоже пересобирается с рядом «Просмотры»."""
    transport._db = db
    event = FakeMsg(menu.label(menu.MENU_OWNER_PAUSE), chat_id=OWNER)
    await transport._on_message(event)

    assert any("Просмотры" in label for label in reply_labels(event)), reply_labels(event)


async def test_pending_queue_shows_address_not_id(transport):
    """В очереди «📋 Заявки» объект показывается адресом, а не внутренним id."""
    local = FakeDb(pending=[dict(PENDING_ROW, id=7, listing_id="L-1")])
    transport._db = local
    event = FakeMsg(label(menu.MENU_OWNER_BOOKINGS), chat_id=OWNER)
    await transport._on_message(event)

    body = body_text(event)
    assert "Заявка #7" in body, body
    assert "L-1" not in body, "внутренний id объекта в очереди не показываем"
    assert "ул. Пушкина" in body, body

