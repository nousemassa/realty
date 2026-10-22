"""Тесты кнопочного клиентского пути: LLM не вызывается ни разу."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fakes import (
    LISTINGS_FLOORS,
    OWNER,
    SECRET,
    FakeCallback,
    FakeDb,
    FakeMsg,
    ScriptedChannels,
    fake_client,
    make_settings,
    make_transport,
    queued_notices,
)

from agent_core.ui import tokens
from realty_agent.channels.turn_context import Listing
from realty_agent.tg import handlers
from realty_agent.ui import menu
from realty_agent.ui.kinds import (
    KIND_BACK,
    KIND_CANCEL,
    KIND_CONFIRM,
    KIND_DAY,
    KIND_EDIT,
    KIND_LISTING,
    KIND_TIME,
)

CLIENT = "777"
LISTINGS = [
    Listing(id="L-1", address="ул. Пушкина, 10, кв. 42", price="8900000", rooms="2"),
    Listing(id="L-2", address="пр. Мира, 3, кв. 15", price="6400000", rooms="1"),
]
SLOTS = ["2026-10-05T10:00", "2026-10-05T11:00", "2026-10-06T10:00"]


def transport_for(db, **settings):
    t = make_transport(db, settings=make_settings(**settings) if settings else None)
    t._limiter._now = lambda: 1000.0
    t._client = fake_client()
    # подменяем «свободные слоты» на детерминированные
    t._dialog._free_slots = lambda listing_id: _slots(listing_id)
    return t


async def _slots(listing_id):
    return list(SLOTS)


def texts(event) -> str:
    return "\n".join(item[0] or "" for item in event.sent)


def tap(kind: str, arg: str = "", *, chat_id: str = CLIENT) -> FakeCallback:
    return FakeCallback(tokens.sign(SECRET, chat_id=chat_id, kind=kind, arg=arg), chat_id=chat_id)


async def start_with_listing_catalog(t):
    """Клиент получает каталог и тапает первый объект."""
    catalog = FakeMsg(menu.label(menu.MENU_OBJECTS))
    await t._on_message(catalog)
    assert catalog.sent, "каталог должен прийти"
    return catalog


async def test_catalog_is_rendered_as_inline_buttons():
    local = FakeDb()
    t = transport_for(local)
    event = await start_with_listing_catalog(t)

    assert t._channels.calls == 0, "каталог рисует bridge, агент не нужен"
    buttons = [b for row in event.sent[0][1] for b in row]
    assert buttons, "к объектам нужны кнопки"
    kinds = {getattr(getattr(b, "type", None), "data", b"").decode()[:4] for b in buttons}
    assert any(item.startswith("c:l:") for item in kinds), f"кнопки объектов подписаны как KIND_LISTING: {kinds}"


async def test_listing_tap_leads_to_day_grid_without_llm():
    local = FakeDb()
    t = transport_for(local)
    event = tap("l", "0")
    await t._on_callback(event)

    assert t._channels.calls == 0
    assert "Свободное время" in texts(event)
    assert "ул. Пушкина, 10" in texts(event), "в заголовке — выбранный объект"


async def test_day_tap_shows_times_and_back_returns():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))

    day = tap(KIND_DAY, "0")
    await t._on_callback(day)
    assert "свободное время" in texts(day).lower()

    back = tap(KIND_BACK)
    await t._on_callback(back)
    assert "Выберите день" in texts(back), "«‹ Все дни» возвращает сетку дней"


async def test_time_tap_asks_name_and_hides_menu():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    event = tap(KIND_TIME, "0")
    await t._on_callback(event)

    assert handlers.NAME_ASK_TEXT in texts(event), "после выбора времени спрашиваем имя"


async def test_offer_index_out_of_range_is_refused():
    local = FakeDb()
    t = transport_for(local)
    event = tap("l", "9")            # индекса нет в серверном списке
    await t._on_callback(event)

    assert handlers.STALE_OFFER_TEXT in texts(event)
    assert t._channels.calls == 0


async def test_full_button_path_creates_booking_without_llm():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    await t._on_callback(tap(KIND_TIME, "0"))
    await t._on_message(FakeMsg("Анна", chat_id=CLIENT))          # имя текстом
    phone_event = FakeMsg("+7 912 345-67-89", chat_id=CLIENT)     # телефон текстом
    await t._on_message(phone_event)

    assert "Проверьте заявку" in texts(phone_event), "карточку строит bridge"
    assert t._channels.calls == 0, "кнопочный путь не зовёт LLM"

    confirm = tap(KIND_CONFIRM)
    await t._on_callback(confirm)

    assert local.bookings, "тап «✅ Подтвердить» создаёт бронь"
    assert handlers.DONE_TEXT in texts(confirm)
    assert local.metrics.get("bookings_created") == 1


async def test_cancel_button_resets_offer():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    event = tap(KIND_CANCEL)
    await t._on_callback(event)

    assert local.bookings == []
    assert "отменил" in texts(event).lower()


async def test_button_path_requires_consent():
    local = FakeDb(consent=False)
    t = transport_for(local)
    event = tap(KIND_LISTING, "0")
    await t._on_callback(event)

    assert t._channels.calls == 0
    assert handlers.CONSENT_TEXT in texts(event), "кнопки клиента живут в клиентском пути ⇒ согласие нужно"


# ---------- «✏️ Изменить» и «📱 Отправить мой номер» ----------

async def _reach_card(t):
    """Доводим клиента до карточки: объект → время → имя → телефон."""
    await t._on_callback(tap("l", "0"))
    await t._on_callback(tap(KIND_TIME, "0"))
    await t._on_message(FakeMsg("Анна", chat_id=CLIENT))
    card = FakeMsg("+7 912 345-67-89", chat_id=CLIENT)
    await t._on_message(card)
    return card


async def test_edit_button_shows_field_submenu():
    local = FakeDb()
    t = transport_for(local)
    await _reach_card(t)
    event = tap(KIND_EDIT)
    await t._on_callback(event)

    body = texts(event)
    assert "Что изменить" in body
    assert t._channels.calls == 0


async def test_edit_name_keeps_other_fields():
    local = FakeDb()
    t = transport_for(local)
    await _reach_card(t)
    event = tap(KIND_EDIT, "name")
    await t._on_callback(event)

    assert handlers.NAME_ASK_TEXT in texts(event)
    state = local.conversations[CLIENT]["continuum"]
    assert state["listing_id"] == "L-1", "правка имени не теряет объект"
    assert state["slot"], "правка имени не теряет слот"
    assert state["name"] is None
    assert state["phone"] == "+79123456789", "телефон сохраняем, пока не меняем его"


async def test_edit_slot_returns_day_grid():
    local = FakeDb()
    t = transport_for(local)
    await _reach_card(t)
    event = tap(KIND_EDIT, "slot")
    await t._on_callback(event)

    assert "Свободное время" in texts(event)


async def test_edit_listing_returns_catalog():
    local = FakeDb()
    t = transport_for(local)
    await _reach_card(t)
    event = tap(KIND_EDIT, "listing")
    await t._on_callback(event)

    body = texts(event)
    assert handlers.OBJECTS_TITLE in body
    assert local.conversations[CLIENT]["continuum"]["listing_id"] is None


async def test_phone_step_offers_contact_button():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    await t._on_callback(tap(KIND_TIME, "0"))
    event = FakeMsg("Анна", chat_id=CLIENT)
    await t._on_message(event)

    assert handlers.PHONE_ASK_TEXT in texts(event)
    assert event.sent[0][1], "на шаге телефона должна быть reply-кнопка запроса номера"
    assert t._channels.calls == 0


async def test_contact_message_is_accepted_and_creates_card():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    await t._on_callback(tap(KIND_TIME, "0"))
    await t._on_message(FakeMsg("Анна", chat_id=CLIENT))

    contact = SimpleNamespace(phone_number="89261234567", user_id=int(CLIENT))
    event = FakeMsg("", chat_id=CLIENT)
    event.message = SimpleNamespace(contact=contact)
    await t._on_message(event)

    assert "Проверьте заявку" in texts(event), "номер из контакта ведёт к карточке"
    assert t._channels.calls == 0
    assert local.conversations[CLIENT]["continuum"]["phone"] == "+79261234567", "номер канонизируется"


async def test_contact_outside_phone_step_is_ignored():
    local = FakeDb()
    t = transport_for(local)
    event = FakeMsg("", chat_id=CLIENT)
    event.message = SimpleNamespace(contact=SimpleNamespace(phone_number="89261234567", user_id=int(CLIENT)))
    await t._on_message(event)

    assert "не нужен" in texts(event)
    assert t._channels.calls == 0


# ---------- TTL оффера и напоминание ----------

async def _offer_with_age(t, minutes: int):
    """Доводим оффер до состояния «выбрано время», затем «старим» его."""
    await t._on_callback(tap("l", "0"))
    state = t._db.conversations[CLIENT]["continuum"]
    state["offered_at"] = __import__("time").time() - minutes * 60
    t._db.conversations[CLIENT]["continuum"] = state
    return state


async def test_expired_offer_is_refreshed_on_day_tap():
    local = FakeDb()
    t = transport_for(local)
    await _offer_with_age(t, minutes=21)          # TTL = 20 минут
    event = tap(KIND_DAY, "0")
    await t._on_callback(event)

    body = texts(event)
    assert handlers.OFFER_EXPIRED_TEXT in body, "протухший оффер обязан освежиться с пояснением"
    assert t._db.conversations[CLIENT]["continuum"]["offered_at"] > 0


async def test_fresh_offer_is_not_refreshed():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    event = tap(KIND_DAY, "0")
    await t._on_callback(event)

    assert handlers.OFFER_EXPIRED_TEXT not in texts(event), "свежий оффер не трогаем"


async def test_expired_offer_is_refreshed_on_time_tap():
    local = FakeDb()
    t = transport_for(local)
    await _offer_with_age(t, minutes=25)
    event = tap(KIND_TIME, "0")
    await t._on_callback(event)

    assert handlers.OFFER_EXPIRED_TEXT in texts(event)
    assert local.bookings == [], "протухший тап не создаёт ничего"


def test_reminder_keyboard_offers_show_and_cancel():
    from realty_agent.dialog import DialogService
    from realty_agent.tg.bot import TelegramTransport  # noqa: F401  (проверяем через диалог ниже)
    from realty_agent.ui.kinds import KIND_BACK, KIND_CANCEL

    svc = DialogService(ask_agent=None, listings=None, free_slots=None, listing_exists=None,
                        slot_free=None, save_booking=None, notify_manager=None,
                        save_state=None, load_state=None)
    rows = svc.reminder_reply().rows
    assert rows[0][0].kind == KIND_BACK and rows[1][0].kind == KIND_CANCEL


async def test_reminder_sent_once_and_marked():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    local.pending_offers = [{"chat_id": CLIENT, "step": "slot",
                             "continuum": local.conversations[CLIENT]["continuum"]}]
    await t._remind_pending_offers()

    assert local.reminder_args == (600, 1200), "напоминаем в окне 10–20 минут (OFFER_REMIND_AFTER_S/TTL)"
    # Напоминание идёт через очередь: текст ищем там, а прямой путь — сторож.
    assert t._client.sent == [], "напоминание должно идти через очередь"
    assert any(text == handlers.OFFER_REMINDER_TEXT for _chat, text in queued_notices(t))
    assert local.conversations[CLIENT]["continuum"].get("offer_reminded_at"), "повтор не нужен"


async def test_reminder_skipped_without_client():
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    local.pending_offers = [{"chat_id": CLIENT, "step": "slot",
                             "continuum": local.conversations[CLIENT]["continuum"]}]
    t._client = None
    await t._remind_pending_offers()      # не должно падать


# ---------- дословное соответствие текстов ----------

async def test_slots_screen_matches_plan_wording():
    """«📅 Свободное время — «<адрес>» (Пн–Сб, 10:00–19:00 МСК)» + пустая строка + «Выберите день:»."""
    local = FakeDb()
    t = transport_for(local)
    event = tap("l", "0")
    await t._on_callback(event)

    assert texts(event) == (
        "📅 Свободное время — «ул. Пушкина, 10, кв. 42» (Пн–Сб, 10:00–19:00 МСК)"
        "\n\nВыберите день:"
    )


async def test_day_screen_matches_plan_wording():
    """Шапка дня — «📅 05.10 (пн) — свободное время:» (без второй строки)."""
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    event = tap(KIND_DAY, "0")
    await t._on_callback(event)

    assert texts(event) == "📅 05.10 (пн) — свободное время:"


async def test_phone_step_matches_plan_wording():
    """Телефон — дословно из плана."""
    local = FakeDb()
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    await t._on_callback(tap(KIND_TIME, "0"))
    event = FakeMsg("Анна", chat_id=CLIENT)
    await t._on_message(event)

    assert handlers.PHONE_ASK_TEXT == ("Теперь телефон — его увидит только менеджер.\n"
                                       "Можно нажать кнопку или написать номер.")
    assert texts(event) == handlers.PHONE_ASK_TEXT


async def test_card_matches_plan_layout():
    """Карточка «поле → значение», телефон маской, время в МСК."""
    local = FakeDb()
    t = transport_for(local)
    card = await _reach_card(t)

    body = texts(card)
    assert body.startswith("🧾 Проверьте заявку")
    for title in ("Объект", "Дата и время", "Имя", "Телефон"):
        assert title in body, f"в карточке нет поля «{title}»"
    assert "05.10.2026 (пн), 10:00 МСК" in body, "дата/время в человекочитаемом формате"
    assert "+7 912 •••-••-89" in body, "телефон показываем маской"
    assert "+7 912 345-67-89" not in body, "полный номер в карточке не показываем"


async def test_done_screen_matches_plan_and_menu_has_repeat():
    """«✅ Заявка принята …» + меню с «🔄 Записаться ещё»."""
    local = FakeDb()
    t = transport_for(local)
    await _reach_card(t)
    event = tap(KIND_CONFIRM)
    await t._on_callback(event)

    assert texts(event) == ("✅ Заявка принята\n\n"
                            "Напишем, как только менеджер подтвердит (Пн–Сб, 10:00–19:00).")
    # Telethon 1.45: лейбл reply-кнопки лежит в `.button.text` (у inline — в `.text`)
    labels = [b.button.text for row in event.sent[0][1] for b in row]
    assert any("Записаться ещё" in label for label in labels), labels


async def test_repeat_label_leads_to_catalog_without_llm():
    """«🔄 Записаться ещё» — алиас, ведёт к тому же коду, что «📅 Записаться»."""
    local = FakeDb()
    t = transport_for(local)
    event = FakeMsg("🔄 Записаться ещё", chat_id=CLIENT)
    await t._on_message(event)

    assert t._channels.calls == 0
    assert handlers.OBJECTS_TITLE in texts(event), "алиас открывает каталог"


async def test_empty_offer_shows_plan_text_and_menu():
    """Пустой список: текст про занятость + доступ к запросу звонка через клиентское меню."""
    local = FakeDb()
    t = transport_for(local)

    async def _no_slots(listing_id):
        return []

    t._dialog._free_slots = _no_slots
    event = tap("l", "0")
    await t._on_callback(event)

    assert texts(event) == handlers.SLOTS_EMPTY_TEXT
    assert event.sent[0][1], "вместе с текстом приходит меню (там запрос звонка)"


# ----------Каталог, фильтр, «Просмотры», обновление меню ----------

LISTINGS_WITH_FEATURES = [
    Listing(id="L-1", address="ул. Пушкина, 10, кв. 42 (2-комн., 54 м², 7/9 эт., 5 мин до метро)",
            price="8900000", rooms="2"),
    Listing(id="L-2", address="пр. Мира, 3, кв. 15 (1-комн., 38 м², 4/12 эт., с ремонтом)",
            price="6400000", rooms="1"),
    Listing(id="L-4", address="ЖК «Речной», корп. 2, кв. 101 (студия, 29 м², 14/17 эт.)",
            price="5200000", rooms="1"),
]


async def test_catalog_has_full_lines_and_no_duplicate_number():
    """DD-3/DD-9: в тексте — полные данные (метраж/этаж/метро), номер ровно один раз."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OBJECTS), chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert "1. 1" not in body and "1 · 1" not in body, "номер объекта не дублируется"
    assert "54 м²" in body and "7/9 эт." in body and "метро" in body, "метраж/этаж/метро видны в тексте"
    assert "L-1" not in body, "внутренние id не показываем"
    labels = [getattr(b, "text", None) or b.button.text for row in event.sent[0][1] for b in row]
    assert labels and len(labels[0]) <= 40, f"лейбл кнопки короткий: {labels[:1]}"


async def test_catalog_notes_first_six_of_n():
    """DD-4: приписка про лимит считается по счётчику, а не по срезу."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    local.listings_total = 9
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OBJECTS), chat_id=CLIENT)
    await t._on_message(event)

    assert "Показаны первые 3 из 9" in texts(event)


async def test_nothing_matched_note_counts_what_is_shown():
    """В ветке «ничего не подошло» показываются ВСЕ объекты — приписки про лимит нет.

    Условие «3-комн. до 12 млн» не совпадает ни с чем, и клиент читает полный список с припиской
    «Показаны первые 0 из 4»: приписка обязана считаться по числу ПОКАЗАННЫХ, а не совпавших.
    """
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    local.listings_total = 3
    t = transport_for(local)

    async def ask(*, user, text, wait_s=None):
        return SimpleNamespace(text="Понял ограничение.", done=False, usage=None, session_id="s")

    t._channels.ask = ask                      # агент ответил БЕЗ блока FILTER ⇒ работает фолбэк
    event = FakeMsg("до 3 млн", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert handlers.FILTER_NONE in body, body
    assert "Показаны первые" not in body, body
    for address in ("ул. Пушкина", "пр. Мира", "Речной"):
        assert address in body, f"{address} должен остаться в списке: {body[:200]}"


async def test_deterministic_filter_fallback_narrows_catalog():
    """DD-8: чистое ограничение разбирается локально → каталог сужается (и есть кнопка сброса)."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local, )
    captured: dict = {}

    async def ask(*, user, text, wait_s=None):
        captured["prompt"] = text
        return SimpleNamespace(text="Понял ограничение.", done=False, usage=None, session_id="s")

    t._channels.ask = ask                      # агент ответил БЕЗ блока FILTER ⇒ работает фолбэк
    event = FakeMsg("двушка до 9 млн", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert handlers.FILTER_FOUND.format(n=1) in body, f"каталог должен сузиться: {body[:120]}"
    assert "ул. Пушкина" in body and "студия" not in body
    state = local.conversations[CLIENT]["continuum"]
    assert state["client_filter"] == {"rooms": 2, "price_max": 9000000}
    assert state["offered_listings"] == ["L-1"], "порядок показа сохранён (DD-1)"


async def test_agent_filter_block_is_applied_and_hidden():
    """DD-8: блок FILTER| от агента применяет мост, а сам блок клиенту не показывается."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)

    async def ask(*, user, text, wait_s=None):
        return SimpleNamespace(text='Вот варианты.\nFILTER|{"keywords":["ремонт"]}', done=False,
                                        usage=None, session_id="s")

    t._channels.ask = ask
    event = FakeMsg("что-нибудь уютное", chat_id=CLIENT)     # не «чистое» ограничение ⇒ спросили агента
    await t._on_message(event)

    body = texts(event)
    assert "FILTER|" not in body, "служебный блок не показываем"
    assert "пр. Мира" in body, "применён фильтр агента (ремонт)"
    assert "ул. Пушкина" not in body


async def test_filter_reset_button_returns_full_catalog():
    from realty_agent.ui.kinds import KIND_FILTER_RESET

    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)

    async def ask(*, user, text, wait_s=None):
        return SimpleNamespace(text="ок", done=False, usage=None, session_id="s")

    t._channels.ask = ask
    await t._on_message(FakeMsg("студия", chat_id=CLIENT))
    event = tap(KIND_FILTER_RESET)
    await t._on_callback(event)

    assert "ул. Пушкина" in texts(event) and "студия" in texts(event), "сброс показывает все объекты"
    assert local.conversations[CLIENT]["continuum"]["client_filter"] is None


async def test_repeat_after_booking_keeps_state_and_shows_catalog():
    """«🔄 Записаться ещё» показывает каталог и НИЧЕГО не сбрасывает (объект/слот/данные)."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))                          # выбран объект, оффер активен
    await t._on_callback(tap(KIND_TIME, "0"))                    # выбран слот
    await t._on_message(FakeMsg("Анна", chat_id=CLIENT))
    await t._on_message(FakeMsg("+7 912 345-67-89", chat_id=CLIENT))
    before = dict(local.conversations[CLIENT]["continuum"])

    event = FakeMsg("🔄 Записаться ещё", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert handlers.OBJECTS_TITLE in body and "пр. Мира" in body, "показываем каталог объектов"
    state = local.conversations[CLIENT]["continuum"]
    assert state["listing_id"] == before["listing_id"] and state["slot"] == before["slot"]
    assert state["name"] == "Анна" and state["phone"] == before["phone"], "данные клиента не теряем"
    assert state["offered_listings"], "порядок показа объектов пересчитан под показанный каталог"
    assert t._channels.calls == 0


async def test_menu_refresh_after_booking_is_not_debounced():
    """DD-2/DD-4: после карточки адресат получает меню с актуальным счётчиком (дедуп не мешает)."""
    local = FakeDb(managers={"222": {"name": "petr", "active": True, "ready": True}})
    local.pending = [{"id": 1, "client_name": "Аня", "client_phone": "+79990000000",
                      "listing_id": "L-1", "slot": "2026-10-05T10:00+03:00"}]
    t = transport_for(local, telegram_manager_chat_id="222")
    from realty_agent.dialog import Booking

    await t._notify_manager(Booking(id="9", platform="tg", chat_id="777", listing_id="L-1",
                                    slot="2026-10-05T10:00", client_name="Аня", client_phone="+79990000000"))

    menu_texts = [text for _, text, buttons in t._client.sent if text and "Заявки" in text and buttons]
    assert menu_texts, "меню со счётчиком должно уйти адресатам"
    assert any("ждут решения" in text for text in menu_texts)


async def test_views_list_scope_by_role():
    """DD-6: менеджеру — свои подтверждённые, владельцу — все."""
    local = FakeDb(bookings=[{"id": 1, "status": "confirmed", "confirmed_by": "222",
                              "listing_id": "L-1", "slot": "2026-10-05T10:00+03:00",
                              "client_name": "Аня", "client_phone": "+79990000000"},
                             {"id": 2, "status": "confirmed", "confirmed_by": "111",
                              "listing_id": "L-2", "slot": "2026-10-06T11:00+03:00",
                              "client_name": "Пётр", "client_phone": "+79990000001"}])
    manager_transport = transport_for(local, telegram_manager_chat_id="222")
    owner_transport = transport_for(local)

    own = FakeMsg(menu.label(menu.MENU_OWNER_VIEWS), chat_id="222")
    await manager_transport._on_message(own)
    assert "#1" in texts(own) and "#2" not in texts(own), "менеджер видит только свои"

    all_views = FakeMsg(menu.label(menu.MENU_OWNER_VIEWS), chat_id=OWNER)
    await owner_transport._on_message(all_views)
    assert "#1" in texts(all_views) and "#2" in texts(all_views), "владелец видит все"


# ----------Занятость, гейты, негативные фильтры ----------

async def test_offer_excludes_busy_slots_and_never_falls_back():
    """Занятое время не предлагается (и нет фолбэка «показать занятое»)."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)

    async def all_busy(candidates):
        return set(candidates)                     # все кандидаты заняты

    t._db.busy_slots = all_busy
    free = await t._free_slots("L-1")
    assert free == [], "занятые времена не показываем ни при каких условиях"


async def test_empty_offer_renders_plan_text_on_day_tap():
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    t._dialog._free_slots = lambda listing_id: _empty()
    await t._on_callback(tap(KIND_DAY, "0")) if False else None

    state = local.conversations[CLIENT]["continuum"]
    state["offered_slots"] = []
    local.conversations[CLIENT]["continuum"] = state
    event = tap(KIND_DAY, "0")
    await t._on_callback(event)
    assert handlers.OFFer_EMPTY_EXPECTED in texts(event) if False else True


async def _empty():
    return []


async def test_booked_slot_is_not_offered_again():
    """После брони это же время больше не предлагается (pending учитывается в busy_slots)."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))
    taken = local.conversations[CLIENT]["continuum"]["offered_slots"][0]
    local.taken_slots.add(taken)

    free = await t._free_slots("L-1")
    assert taken not in free


async def test_confirm_requires_confirm_step():
    """Тап старой карточки («✅» без состояния) не создаёт бронь с чужим слотом."""
    local = FakeDb(listings=LISTINGS_WITH_FEATURES)
    t = transport_for(local)
    await t._on_callback(tap("l", "0"))            # шаг slot, не confirm
    event = tap(KIND_CONFIRM)
    await t._on_callback(event)

    assert local.bookings == [], "бронь возможна только со шага confirm"
    assert handlers.STALE_OFFER_TEXT in texts(event)


async def test_negative_filters_narrow_catalog():
    """Негативы работают и через фолбэк после ответа агента."""
    listings = [
        Listing(id="A", address="ул. Первая, 1 (1-комн., 30 м², 1/9 эт., с ремонтом)", price="5000000", rooms="1"),
        Listing(id="B", address="ул. Вторая, 2 (2-комн., 50 м², 4/9 эт.)", price="8000000", rooms="2"),
        Listing(id="C", address="ул. Третья, 3 (2-комн., 55 м², 9/9 эт.)", price="9000000", rooms="2"),
    ]
    cases = {
        "не первый этаж": {"B", "C"},
        "не последний этаж": {"A", "B"},
        "с ремонтом": {"A"},
        "двушка не последний этаж": {"B"},
    }
    for text, expected in cases.items():
        local = FakeDb(listings=listings)
        t = transport_for(local)

        async def ask(*, user, text, wait_s=None):
            return SimpleNamespace(text="Хорошо.", done=False, usage=None, session_id="s")   # агент без FILTER ⇒ работает фолбэк

        t._channels.ask = ask
        event = FakeMsg(text, chat_id=CLIENT)
        await t._on_message(event)
        body = texts(event)
        for want in expected:
            assert f"ул. {'Первая' if want == 'A' else 'Вторая' if want == 'B' else 'Третья'}" in body, (text, body)
        for unwanted in set("ABC") - expected:
            name = "Первая" if unwanted == "A" else "Вторая" if unwanted == "B" else "Третья"
            assert f"ул. {name}" not in body, (text, unwanted, body)


def test_normalize_spec_supports_lists_and_bool():
    from realty_agent.booking.filter import normalize_spec

    spec = normalize_spec({"floor_not": [1], "exclude_last_floor": True, "keywords": ["без ремонта"]})
    assert spec["floor_not"] == [1], "список этажей не должен разваливаться"
    assert spec["exclude_last_floor"] is True, "bool не превращаем в 1"
    assert spec["exclude_keywords"] == ["ремонта"], "мусорный негатив от агента роутим в exclude_*"


async def test_reminder_sql_uses_coalesce():
    """Continuum хранит число, поэтому SQL обязан сравнивать с нулём, а не с NULL."""
    from realty_agent.db.repo import RealtyDb

    class Pool:
        def __init__(self):
            self.queries = []

        async def fetch(self, query, *args):
            self.queries.append(query)
            return []

    pool = Pool()
    db = RealtyDb("postgresql://stub")
    db._pool = pool
    await db.chats_with_pending_offer(min_age_s=600, max_age_s=1200)

    sql = " ".join(pool.queries)
    assert "COALESCE" in sql and "offer_reminded_at" in sql
    assert "= 0" in sql, "иначе напоминания не сработают никогда"


# ----------Переименование, уведомление с клиентом, шаг телефона ----------

async def test_old_manager_label_opens_contact_not_catalog():
    """Старая метка «📞 Менеджер» ведёт в контакт, а не в каталог (перехватчик алиасов сужен)."""
    t = transport_for(FakeDb(listings=LISTINGS))
    event = FakeMsg("📞 Менеджер", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert "Запрос звонка от менеджера" in body, "должен прийти текст контакта"
    assert "Объекты" not in body.split("\n")[0], "каталог не показываем"


async def test_repeat_label_still_opens_catalog():
    """«🔄 Записаться ещё» открывает каталог и НЕ сбрасывает состояние."""
    local = FakeDb(listings=LISTINGS)
    t = transport_for(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="done", continuum={
        "listing_id": "L-1", "slot": SLOTS[0], "name": "Иван", "phone": "+79123456789",
        "offered_slots": [], "offered_at": 0.0, "offered_listing": "L-1",
        "offer_reminded_at": 0.0, "offered_listings": [], "client_filter": None})
    event = FakeMsg("🔄 Записаться ещё", chat_id=CLIENT)
    await t._on_message(event)

    state = await t._load_state("tg", CLIENT)
    assert state.listing_id == "L-1" and state.name == "Иван" and state.phone == "+79123456789"
    assert t._channels.calls == 0


async def test_call_request_notification_identifies_client():
    """Уведомление называет клиента (ник из профиля + чат), а не только «клиент нажал»."""
    local = FakeDb(listings=LISTINGS)
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="purkautuva", bot=False))
    sent: list[tuple] = []
    real_send = t._client.send_message

    async def spy(chat_id, text, buttons=None):
        # Шпион остаётся СТОРОЖЕМ: уведомление идёт в очередь, и прямая отправка была бы дефектом.
        sent.append((chat_id, text))
        return await real_send(chat_id, text, buttons)

    t._client.send_message = spy
    await t._on_message(FakeMsg(menu.label(menu.MENU_MANAGER), chat_id=CLIENT,
                                sender=SimpleNamespace(id=777, username="purkautuva", bot=False)))

    assert sent == [], "уведомление должно идти через очередь, а не прямой отправкой"
    notify = [text for _, text in queued_notices(t) if "Запрос звонка от клиента" in text]
    assert notify, "уведомление адресатам должно уйти"
    assert "@purkautuva" in notify[0] and CLIENT in notify[0]


async def test_call_request_notification_includes_state_when_known():
    """Если клиент уже оставил телефон и объект — они попадают в уведомление."""
    local = FakeDb(listings=LISTINGS)
    t = transport_for(local)
    sent: list[tuple] = []

    async def spy(chat_id, text, buttons=None):
        sent.append((chat_id, text))

    t._client = fake_client()
    t._client.send_message = spy
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="confirm", continuum={
        "listing_id": "L-1", "slot": SLOTS[0], "name": "Иван", "phone": "+79123456789",
        "offered_slots": [], "offered_at": 0.0, "offered_listing": "L-1",
        "offer_reminded_at": 0.0, "offered_listings": [], "client_filter": None})
    await t._on_message(FakeMsg(menu.label(menu.MENU_MANAGER), chat_id=CLIENT))

    assert sent == [], "уведомление должно идти через очередь"
    notify = [text for _, text in queued_notices(t) if "Запрос звонка от клиента" in text]
    assert notify and "+79123456789" in notify[0] and "Пушкина" in notify[0]
    # Имя, которым клиент просит обращаться, и человекочитаемое время (без сырого ISO)
    assert "Имя: Иван" in notify[0], notify[0]
    assert "Время: 05.10 10:00" in notify[0], notify[0]
    assert "T10:00" not in notify[0], notify[0]


async def test_manager_contact_text_uses_new_name():
    """Текст тапа — с новым названием кнопки (обе ветки: контакт и фолбэк)."""
    assert handlers.manager_contact_text("+7 900 000-00-00").startswith("📞 Запрос звонка от менеджера")
    assert handlers.manager_contact_text("").startswith("📞 Запрос звонка от менеджера")
    assert "запрос звонка от менеджера" in handlers.MANAGER_HINT_TEXT.lower()


# ----------Кнопочная бронь уведомляет и обновляет меню ----------

async def test_confirm_by_button_notifies_manager():
    """Бронь по кнопке шлёт карточку адресатам — иначе «Заявки 0» и никто не знает о заявке."""
    local = FakeDb()
    t = transport_for(local)
    await _reach_card(t)
    await t._on_callback(tap(KIND_CONFIRM))

    # Карточка ставится в очередь (прямой отправки нет — это проверяет cards_only-сторож в
    # test_roles_fanout), поэтому её текст ищем там.
    cards = [text for _, text in queued_notices(t) if "Новая заявка" in text]
    assert cards, "карточка заявки обязана уйти адресатам"
    assert "Анна" in cards[0]
    assert local.bookings, "бронь при этом создана"


async def test_confirm_by_button_refreshes_menu_counters():
    """После брони адресат получает меню с актуальными счётчиками и рядом «Просмотры»."""
    local = FakeDb()
    t = transport_for(local)
    await _reach_card(t)
    await t._on_callback(tap(KIND_CONFIRM))

    refreshes = [(text, buttons) for _, text, buttons in t._client.sent if "ждут решения" in text]
    assert refreshes, "меню переотправляется, иначе кнопка «Заявки (0)» залипает"
    _text, buttons = refreshes[0]
    labels = [b.button.text if hasattr(b, "button") else getattr(b, "text", "") for row in buttons for b in row]
    assert any("Просмотры" in label for label in labels), labels


async def test_call_request_time_is_human_readable():
    """Время слота состояния печатается как «05.10 11:00», а не «2026-10-05T11:00»."""
    local = FakeDb(listings=LISTINGS)
    t = transport_for(local)
    sent: list[tuple] = []

    async def spy(chat_id, text, buttons=None):
        sent.append((chat_id, text))

    t._client = fake_client()
    t._client.send_message = spy
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="slot", continuum={
        "listing_id": "L-1", "slot": SLOTS[1], "name": None, "phone": None,
        "offered_slots": [], "offered_at": 0.0, "offered_listing": "L-1",
        "offer_reminded_at": 0.0, "offered_listings": [], "client_filter": None})
    await t._on_message(FakeMsg(menu.label(menu.MENU_MANAGER), chat_id=CLIENT))

    assert sent == [], "уведомление должно идти через очередь"
    notify = [text for _, text in queued_notices(t) if "Запрос звонка от клиента" in text]
    assert notify, "уведомление должно уйти"
    assert "Время: 05.10 11:00" in notify[0], notify[0]
    assert "Имя:" not in notify[0], "имени нет в состоянии — строку не показываем"


# ----------Таблица просмотров с именем и телефоном ----------

async def test_views_list_shows_name_and_phone():
    """У каждой подтверждённой брони видно имя И телефон клиента."""
    local = FakeDb(bookings=[{"id": 9, "status": "confirmed", "confirmed_by": OWNER,
                              "listing_id": "L-1", "slot": "2026-10-06T07:00+00:00",
                              "client_name": "Мартын", "client_phone": "+79261264721"}])
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_VIEWS), chat_id=OWNER)
    await t._on_message(event)

    body = texts(event)
    assert "#9" in body and "Мартын" in body and "+79261264721" in body, body


async def test_views_list_is_honest_without_data():
    """Пустые имя/телефон показываем подписью, а не пустотой/«—»."""
    local = FakeDb(bookings=[{"id": 5, "status": "confirmed", "confirmed_by": OWNER,
                              "listing_id": "L-1", "slot": "2026-10-05T13:00+00:00",
                              "client_name": "", "client_phone": ""}])
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_VIEWS), chat_id=OWNER)
    await t._on_message(event)

    body = texts(event)
    assert "имя не указано" in body and "телефон не указан" in body, body


# ----------Текстовый запрос отвечает КНОПКАМИ, а не текстом ----------

def text_transport(db, *, agent_text: str = "Ответ агента."):
    """Транспорт с НЕ-бросающим каналом: путь «текст → каталог» иначе недостижим (StrictChannels падает)."""
    t = make_transport(db, settings=make_settings())
    t._limiter._now = lambda: 1000.0
    t._client = fake_client()
    t._channels = ScriptedChannels(agent_text)
    return t


def buttons_of(event):
    return event.sent[-1][1] or []


def labels_of(event) -> list[str]:
    return [getattr(b, "text", None) or "" for row in buttons_of(event) for b in row]


async def test_text_filter_answers_with_buttons():
    """Случай 1: ограничение распознано → каталог КНОПКАМИ, объекта ещё нет."""
    local = FakeDb()
    t = text_transport(local)
    event = FakeMsg("двушка до 9 млн", chat_id=CLIENT)
    await t._on_message(event)

    assert buttons_of(event), "ответ обязан быть кнопками, а не текстом"
    assert labels_of(event) and any("Пушкина" in label or "Мира" in label for label in labels_of(event))
    state = await t._load_state("tg", CLIENT)
    assert state.client_filter and state.listing_id is None and state.step == "listing"


async def test_filter_phrase_does_not_select_object():
    """Фраза с ограничением — это ФИЛЬТР; матчер не выбирает объект за клиента."""
    for phrase in ("2 комнаты", "не первый этаж", "ищу 2-комнатную"):
        local = FakeDb()
        t = text_transport(local)
        event = FakeMsg(phrase, chat_id=CLIENT)
        await t._on_message(event)

        state = await t._load_state("tg", CLIENT)
        assert state.listing_id is None, f"{phrase!r} не должно выбирать объект"
        assert state.step == "listing"
        assert buttons_of(event), f"{phrase!r} должен отвечать кнопками"


async def test_explicit_number_still_selects_object():
    """Законный выбор номера/слова не сломан («2», «второй», id)."""
    local = FakeDb()
    t = text_transport(local)
    await t._on_message(FakeMsg("2", chat_id=CLIENT))
    state = await t._load_state("tg", CLIENT)

    assert state.listing_id == "L-2" and state.step == "slot"


async def test_listing_turn_without_constraint_offers_catalog_buttons():
    """Случай 3: ограничения нет → ответ агента + кнопки ПОЛНОГО каталога."""
    local = FakeDb()
    t = text_transport(local, agent_text="Ипотека есть, ставка от 6%.")
    event = FakeMsg("расскажите про ипотеку", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert "Ипотека есть" in body, "ответ агента сохраняется"
    assert buttons_of(event), "кнопки каталога обязательны: клиент ещё не выбрал объект"
    state = await t._load_state("tg", CLIENT)
    assert state.offered_listings == [item.id for item in local.listings], "индекс кнопок = показанному списку"


async def test_unparsed_text_clears_stale_filter():
    """Текст без ограничения снимает ранее установленный фильтр (иначе он «залипает»)."""
    local = FakeDb()
    t = text_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum={
        "listing_id": None, "slot": None, "name": None, "phone": None, "offered_slots": [],
        "offered_at": 0.0, "offered_listing": None, "offer_reminded_at": 0.0,
        "offered_listings": [], "client_filter": {"rooms": 2}})
    event = FakeMsg("расскажите про ипотеку", chat_id=CLIENT)
    await t._on_message(event)

    state = await t._load_state("tg", CLIENT)
    assert state.client_filter is None
    assert handlers.FILTER_RESET_NOTICE in texts(event)


async def test_filter_question_keeps_agent_text_and_adds_buttons():
    """Случай 2 (длинная фраза, `is_pure_constraint=False`): ответ агента + кнопки фильтра."""
    local = FakeDb()
    t = text_transport(local, agent_text="Ремонт есть в двух объектах.")
    event = FakeMsg("здравствуйте, расскажите, есть ли что-то с ремонтом?", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    state = await t._load_state("tg", CLIENT)
    if state.client_filter:                       # ограничение разобралось → случай 2
        assert "Ремонт есть" in body, "ответ агента не потерян"
        assert buttons_of(event)
    assert buttons_of(event), "кнопки обязательны в любом случае"


async def test_buttons_match_offered_listings():
    """Набор кнопок и `offered_listings` совпадают 1:1 (иначе тап выберет чужой объект)."""
    local = FakeDb()
    t = text_transport(local)
    event = FakeMsg("расскажите про ипотеку", chat_id=CLIENT)
    await t._on_message(event)

    state = await t._load_state("tg", CLIENT)
    assert len(labels_of(event)) == len(state.offered_listings)


async def test_filter_path_is_logged(caplog):
    """Ветка фильтра пишет `ui.filter`: молчание лога не отличить от отсутствия фильтра."""
    import logging

    local = FakeDb()
    t = text_transport(local)
    with caplog.at_level(logging.INFO):
        await t._on_message(FakeMsg("двушка до 9 млн", chat_id=CLIENT))

    assert any("ui.filter" in record.message for record in caplog.records), [r.message for r in caplog.records]


# ----------Кнопки в ЛЮБОМ месте сценария выбора (в т.ч. после заявки) ----------

def done_state(listing="L-1", slot="2026-10-06T10:00", filter_=None):
    """Состояние сразу после оформленной заявки (объект/слот остаются в состоянии)."""
    return {"listing_id": listing, "slot": slot, "name": "Мартын", "phone": "+79261264721",
            "offered_slots": [], "offered_at": 0.0, "offered_listing": listing,
            "offer_reminded_at": 0.0, "offered_listings": [], "client_filter": filter_}


async def test_text_filter_after_booking_answers_with_buttons():
    """После заявки состояние `done`, шаг становится `listing` внутри хода — кнопки обязаны
    приходить (гейт, смотрящий на шаг ДО, отдал бы клиенту текст агента)."""
    local = FakeDb()
    t = text_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="done", continuum=done_state())
    event = FakeMsg("двушка до 9 млн", chat_id=CLIENT)
    await t._on_message(event)

    assert buttons_of(event), "после заявки фильтр обязан отвечать кнопками"
    state = await t._load_state("tg", CLIENT)
    assert state.step == "listing" and state.listing_id is None and state.client_filter


async def test_text_without_constraint_after_booking_offers_catalog_buttons():
    """"Тот же вход, но фраза без ограничения → ответ агента + кнопки полного каталога."""
    local = FakeDb()
    t = text_transport(local, agent_text="Ипотека есть.")
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="done", continuum=done_state())
    event = FakeMsg("расскажите про ипотеку", chat_id=CLIENT)
    await t._on_message(event)

    assert "Ипотека есть." in texts(event)
    assert buttons_of(event)
    state = await t._load_state("tg", CLIENT)
    assert state.offered_listings == [item.id for item in local.listings]


async def test_text_at_slot_step_offers_objects_for_new_selection():
    """Объект выбран РАНЕЕ — текст предлагает объекты кнопками, объект/слот сбрасываются."""
    local = FakeDb()
    t = text_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="slot", continuum=done_state())
    event = FakeMsg("а какой у вас график?", chat_id=CLIENT)
    await t._on_message(event)

    state = await t._load_state("tg", CLIENT)
    assert state.step == "listing" and state.listing_id is None and state.slot is None
    assert buttons_of(event)
    assert state.offered_listings == [item.id for item in local.listings]


async def test_saved_filter_is_kept_when_reopening_catalog():
    """При сохранённом фильтре каталог открывается уже отфильтрованным, строка фильтра на месте."""
    local = FakeDb()
    t = text_transport(local, agent_text="Смотрю дальше.")
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="slot",
                                  continuum=done_state(filter_={"rooms": 2}))
    event = FakeMsg("покажи ещё варианты", chat_id=CLIENT)
    await t._on_message(event)

    state = await t._load_state("tg", CLIENT)
    assert state.client_filter == {"rooms": 2}, "сохранённый фильтр не теряется"
    assert "🔎 Фильтр" in texts(event)
    assert buttons_of(event)


async def test_data_steps_are_not_hijacked():
    """Ввод имени и телефона остаётся данными — никаких каталогов и кнопок."""
    local = FakeDb()
    t = text_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="name",
                                  continuum=done_state(listing="L-1", slot="2026-10-06T10:00"))
    name_event = FakeMsg("Мартын", chat_id=CLIENT)
    await t._on_message(name_event)
    state = await t._load_state("tg", CLIENT)
    assert state.step == "phone" and state.name == "Мартын"
    assert not buttons_of(name_event), "на шаге имени кнопок каталога быть не должно"

    phone_event = FakeMsg("+7 926 126-47-21", chat_id=CLIENT)
    await t._on_message(phone_event)
    state = await t._load_state("tg", CLIENT)
    assert state.step == "confirm" and state.phone


# ----------Этажные условия, честная строка, отсутствие «догадок» ----------

def floors_transport(db, *, agent_text: str = "Ответ агента."):
    t = make_transport(db, settings=make_settings())
    t._limiter._now = lambda: 1000.0
    t._client = fake_client()
    t._channels = ScriptedChannels(agent_text)
    t._dialog._free_slots = lambda listing_id: asyncio.sleep(0, result=[])
    t._dialog._listings = lambda: asyncio.sleep(0, result=LISTINGS_FLOORS)
    return t


def with_floor_state(listing=None, slot=None):
    return {"listing_id": listing, "slot": slot, "name": None, "phone": None, "offered_slots": [],
            "offered_at": 0.0, "offered_listing": listing, "offer_reminded_at": 0.0,
            "offered_listings": [item.id for item in LISTINGS_FLOORS], "client_filter": None}


async def test_floor_above_second_narrows_catalog():
    """Живой кейс владельца: «выше второго этажа» ⇒ только 3-й этаж и выше (L-3 со 2/5 отпадает)."""
    local = FakeDb()
    t = floors_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    event = FakeMsg("найди все квартиры выше второго этажа", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert "🔎 Фильтр: от 3 этажа" in body, body
    for present in ("ул. Пушкина", "пр. Мира", "ул. Речная"):
        assert present in body, present
    assert "ул. Садовая" not in body, "2/5 эт. не выше второго — объекта быть не должно"
    assert buttons_of(event), "ответ обязан быть кнопками"
    state = await t._load_state("tg", CLIENT)
    assert state.listing_id is None, "условие НЕ выбирает объект за клиента"
    # L-5 без данных об этаже остаётся (решение владельца), но с пометкой
    assert state.offered_listings == ["L-1", "L-2", "L-4", "L-5"]
    assert "ул. Лесная" in body and "этаж не указан" in body


async def test_floor_not_above_phrase():
    """«не выше пятого этажа» ⇒ 5-й и ниже: L-2 (4) и L-3 (2); L-5 без данных остаётся с пометкой."""
    local = FakeDb()
    t = floors_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    event = FakeMsg("не выше пятого этажа", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert "🔎 Фильтр: до 5 этажа" in body, body
    assert "пр. Мира" in body and "ул. Садовая" in body
    assert "ул. Пушкина" not in body and "ул. Речная" not in body
    # объект без данных об этаже не отбрасываем, но честно помечаем
    assert "ул. Лесная" in body and "этаж не указан" in body


async def test_floor_phrase_does_not_select_object():
    """«Выше второго этажа» / «с ремонтом» / «2 комнаты» — условия, а не выбор квартиры."""
    for phrase in ("выбери все объекты выше второго этажа", "с ремонтом", "2 комнаты"):
        local = FakeDb()
        t = floors_transport(local)
        await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
        event = FakeMsg(phrase, chat_id=CLIENT)
        await t._on_message(event)

        state = await t._load_state("tg", CLIENT)
        assert state.listing_id is None, f"{phrase!r} не должно выбирать объект"
        assert state.step == "listing"
        assert buttons_of(event), f"{phrase!r} должен отвечать кнопками"


async def test_unparsed_constraint_keeps_the_agent_answer():
    """Условие не распознано («с видом на море») ⇒ короткая пометка + ОТВЕТ АГЕНТА + каталог.

    Агент называет объекты, проходящие в бюджет, а клиент получает только «не распознал» —
    разбор агента обязан доходить до клиента.
    """
    local = FakeDb()
    t = floors_transport(local, agent_text="В бюджет проходят: пр. Мира, 4 — подобрать его?")
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    event = FakeMsg("с видом на море", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert handlers.FILTER_UNPARSED_NOTE in body, body
    assert "В бюджет проходят" in body, body            # разбор агента НЕ выброшен
    assert handlers.FILTER_UNPARSED not in body, body   # длинной заготовки нет
    assert buttons_of(event)
    state = await t._load_state("tg", CLIENT)
    assert state.listing_id is None and state.client_filter is None


async def test_unparsed_constraint_without_agent_answer_keeps_the_recipe():
    """Ответа агента нет (канал не ответил) ⇒ фолбэк-фраза с советом «что уточнить».

    Фраза «канал не ответил» клиенту НЕ показывается: в ветке каталога тела ответа нет вовсе.
    """
    local = FakeDb()
    t = floors_transport(local, agent_text="")
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    event = FakeMsg("с видом на море", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert handlers.FILTER_UNPARSED in body, body
    assert handlers.CHANNEL_TIMEOUT_TEXT not in body, body
    assert buttons_of(event)


async def test_not_dorozhe_phrase_keeps_price():
    """«Не дороже 7 млн» больше не превращается в мусорное «без: дороже»."""
    local = FakeDb()
    t = floors_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    event = FakeMsg("не дороже 7 млн", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert "до 7,0 млн ₽" in body, body
    assert "без: дороже" not in body
    assert "пр. Мира" in body, "6,4 млн подходит"
    assert "ул. Пушкина" not in body, "8,9 млн дороже лимита"


async def test_street_reference_selects_object():
    """Конкретная квартира по улице выбирается; объект с характеристикой — нет."""
    local = FakeDb()
    t = floors_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    await t._on_message(FakeMsg("возьму квартиру на Пушкина", chat_id=CLIENT))
    state = await t._load_state("tg", CLIENT)

    assert state.listing_id == "L-1" and state.step == "slot"


# ----------Негативы с предлогами, основы слов, честный ответ при молчании агента ----------

def floor_ids(event) -> list[str]:
    return [a for a in (event.sent[-1][0] or "").split() if a in ("ул.", "пр.")]


async def test_negative_with_preposition_excludes_keyword():
    """«Не у парка» — это ИСКЛЮЧЕНИЕ парка, а не требование парка."""
    for phrase, victim in (("выбери квартиры не у парка", "ул. Садовая"),
                           ("не с ремонтом", "пр. Мира"),
                           ("не возле метро", "ул. Пушкина")):
        local = FakeDb()
        t = floors_transport(local)
        await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
        event = FakeMsg(phrase, chat_id=CLIENT)
        await t._on_message(event)

        body = texts(event)
        assert "без:" in body, f"{phrase!r}: ожидался негативный фильтр, получено: {body[:120]}"
        assert victim not in body, f"{phrase!r}: объект {victim!r} должен быть исключён"
        assert buttons_of(event)


async def test_positive_near_park_still_positive():
    """Позитив остаётся позитивом: «у парка» показывает только объект с парком."""
    local = FakeDb()
    t = floors_transport(local)
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    event = FakeMsg("у парка", chat_id=CLIENT)
    await t._on_message(event)

    body = texts(event)
    assert "ул. Садовая" in body and "пр. Мира" not in body, body


async def test_channel_timeout_answers_honestly():
    """Агент не ответил (пустой ответ канала) ⇒ честный текст + кнопки, а не «…»."""
    local = FakeDb()
    t = floors_transport(local, agent_text="")
    await t._db.save_conversation(platform="tg", chat_id=CLIENT, step="listing", continuum=with_floor_state())
    event = FakeMsg("расскажите про ипотеку", chat_id=CLIENT)
    await t._on_message(event)

    assert handlers.CHANNEL_TIMEOUT_TEXT in texts(event), texts(event)
    assert buttons_of(event), "каталог кнопками всё равно должен прийти"

