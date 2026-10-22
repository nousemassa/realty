"""Тесты шагов диалога: разбор выбора объекта/слота, имя, полный проход до BOOKING.

Проверяем главное: шаги ведёт bridge (LLM не влияет), слот принимается только из предложенных,
а телефон в блоке агента может быть маской — реальные цифры подставляет bridge.
"""
from __future__ import annotations

from realty_agent.channels.turn_context import Listing
from realty_agent.dialog import ChatState, DialogService, explicit_object_ref, match_slot, parse_name

LISTINGS = [
    Listing(id="L-1", address="ул. Пушкина, 10", price="8 900 000", rooms="2"),
    Listing(id="L-2", address="пр. Мира, 3", price="6 400 000", rooms="1"),
    Listing(id="L-3", address="ул. Садовая, 27", price="12 500 000", rooms="3"),
]
SLOTS = ["2026-10-01T10:00", "2026-10-01T11:00", "2026-10-02T10:00"]


def test_explicit_object_ref_by_id_number_word_and_street():
    """Выбор объекта — только по ЯВНОЙ ссылке (id, отдельный номер/порядковое, улица)."""
    assert explicit_object_ref("Беру L-1", LISTINGS) == "L-1"
    assert explicit_object_ref("интересует L-3", LISTINGS) == "L-3", "id в нижнем регистре"
    # кириллическая «л-3» — это НЕ id каталога: жадный номерной путь принял бы её за ссылку
    assert explicit_object_ref("интересует л-3", LISTINGS) is None
    assert explicit_object_ref("2", LISTINGS) == "L-2"
    assert explicit_object_ref("второй вариант", LISTINGS) == "L-2"
    assert explicit_object_ref("возьму квартиру на Пушкина", LISTINGS) == "L-1"
    assert explicit_object_ref("Мира, 3", LISTINGS) == "L-2"


def test_explicit_object_ref_never_guesses():
    """Условие/вопрос/дата — это НЕ выбор объекта: «выше второго этажа» больше не выбирает квартиру."""
    for text in ("найди все квартиры выше второго этажа", "выбери все объекты выше второго этажа",
                 "2 комнаты", "не первый этаж", "двушка до 9 млн", "с ремонтом", "а есть скидка?"):
        assert explicit_object_ref(text, LISTINGS) is None, text
    assert explicit_object_ref("свободно 2026-10-01T10:00?", LISTINGS) is None, "дата/слот не номер объекта"
    assert explicit_object_ref("9", LISTINGS) is None, "номера вне списка не принимаем"
    assert explicit_object_ref("Здравствуйте, хочу посмотреть квартиру", LISTINGS) is None, (
        "«посмоТРЕть» не должно читаться как «третий» (грабля подстрочного поиска)"
    )
    assert explicit_object_ref("1 октября 11:00", LISTINGS) is None, "дата/время не номер объекта"


def test_match_slot_forms_and_whitelist():
    assert match_slot("удобно 2026-10-01T11:00", SLOTS) == "2026-10-01T11:00"
    assert match_slot("давайте 01.10 в 11:00", SLOTS) == "2026-10-01T11:00"
    assert match_slot("1 октября 11:00 подойдёт", SLOTS) == "2026-10-01T11:00"
    assert match_slot("11:00", SLOTS) == "2026-10-01T11:00", "единственное такое время — берём"
    assert match_slot("10:00", SLOTS) is None, "такое время есть в двух днях — просим уточнить"
    assert match_slot("давайте 3 октября в 11:00", SLOTS) is None, "слот не из предложенных"
    assert match_slot("послезавтра вечером", SLOTS) is None
    assert match_slot("давайте 3 октября в 11:00", SLOTS) is None, "чужая дата не подменяется временем"


def test_parse_name_rejects_questions_and_phones():
    assert parse_name("Иван Петров") == "Иван Петров"
    assert parse_name("  Анна ") == "Анна"
    assert parse_name("а можно в субботу?") is None
    assert parse_name("+7 999 123-45-67") is None
    assert parse_name("Б" * 41) is None
    assert parse_name("x" * 60) is None


def _collect(bucket):
    async def inner(item):
        bucket.append(item)
    return inner


def _service():
    saved: dict = {"bookings": [], "notified": [], "state": None, "last_turn": ""}

    async def ask_agent(user: str, text: str):
        """Подставной агент: ведёт себя по контракту (шаг из данных хода, BOOKING в конце)."""
        saved["last_turn"] = text

        def field(name: str) -> str:
            for line in text.splitlines():
                if line.startswith(name):
                    return line.split(":", 1)[1].strip()
            return ""

        step = field("step")
        lexeme = field("challenge-лексема хода")
        listing_id = field("выбранный объект")
        slot = field("выбранный слот")
        name = field("имя клиента")
        phone_mask = field("телефон (маска, цифры подставит система)")
        if step == "listing":
            body = "Актуальные объекты:\n1) ул. Пушкина, 10\n2) пр. Мира, 3\n3) ул. Садовая, 27"
        elif step == "slot":
            body = "Свободное время (МСК): 1 октября 10:00, 11:00"
        elif step == "name":
            body = "Как к вам обращаться?"
        elif step == "phone":
            body = "И телефон — его увидит только менеджер."
        else:
            body = (
                f"Сводка: объект {listing_id}, {slot}, {name}, {phone_mask}. Подтвердите, пожалуйста.\n"
                f"BOOKING|{listing_id}|{slot}|{name}|{phone_mask}@ch{lexeme}"
            )
        return type("Reply", (), {"text": body, "done": True})()

    async def _list():
        return LISTINGS

    async def _slots(listing_id):
        return SLOTS

    async def _exists(listing_id):
        return listing_id in {listing.id for listing in LISTINGS}

    async def _slot_free(listing_id, slot):
        return slot.replace(" ", "T") in SLOTS

    async def _save_state(state):
        saved["state"] = state

    async def _load_state(*args, **kwargs):
        return saved["state"]

    svc = DialogService(
        ask_agent=ask_agent,
        listings=_list,
        free_slots=_slots,
        listing_exists=_exists,
        slot_free=_slot_free,
        save_booking=_collect(saved["bookings"]),
        notify_manager=_collect(saved["notified"]),
        save_state=_save_state,
        load_state=_load_state,
        new_id=lambda: "BK-1",
    )
    return svc, saved


async def test_full_dialog_reaches_pending_booking_with_masked_phone():
    svc, saved = _service()

    first = await svc.handle_message(platform="tg", chat_id="42", text="Здравствуйте, хочу посмотреть квартиру")
    assert "ул. Пушкина" in first.text
    assert "step: listing" in saved["last_turn"]
    assert saved["state"].step == "listing", "без выбора объекта шаг не двигается"

    await svc.handle_message(platform="tg", chat_id="42", text="Первый вариант")
    assert "step: slot" in saved["last_turn"], "после выбора объекта агент получает шаг slot"
    assert saved["state"].listing_id == "L-1"

    await svc.handle_message(platform="tg", chat_id="42", text="1 октября 11:00")
    assert "step: name" in saved["last_turn"]
    assert saved["state"].slot == "2026-10-01T11:00"
    assert "выбранный слот: 2026-10-01T11:00" in saved["last_turn"], "данные выбора поданы агенту"

    await svc.handle_message(platform="tg", chat_id="42", text="Иван Петров")
    assert "step: phone" in saved["last_turn"]
    assert saved["state"].name == "Иван Петров"

    final = await svc.handle_message(platform="tg", chat_id="42", text="+7 999 123-45-67")
    assert "step: confirm" in saved["last_turn"]
    assert saved["state"].step == "done", "после заявки диалог завершён"
    assert len(saved["bookings"]) == 1, "бронь создана bridge-ом"
    booking = saved["bookings"][0]
    assert booking.client_phone == "+79991234567", "реальные цифры подставлены bridge, не агентом"
    assert booking.listing_id == "L-1" and booking.slot == "2026-10-01T11:00"
    assert saved["notified"] == saved["bookings"], "менеджер уведомлён"
    assert "•••-••-67" in final.text, "в ответе клиенту телефон маскирован"
    assert "BOOKING|" not in final.text, "служебная строка вырезана"
    # Объект показываем адресом, внутренний id клиенту не уходит
    assert "ул. Пушкина, 10" in final.text and "L-1" not in final.text, final.text


async def test_agent_cannot_swap_listing_or_slot():
    svc, saved = _service()
    saved["state"] = ChatState(
        platform="tg", chat_id="9", step="confirm", listing_id="L-1", slot="2026-10-01T11:00",
        name="Анна", phone="+79991234567",
    )

    async def liar(user: str, text: str):
        lexeme = text.split("challenge-лексема хода: ")[1].split("\n")[0].strip()
        return type("Reply", (), {
            "text": f"BOOKING|L-3|2026-10-02T10:00|Анна|+7 999 123-45-67@ch{lexeme}", "done": True,
        })()

    svc._ask = liar  # type: ignore[attr-defined]
    await svc.handle_message(platform="tg", chat_id="9", text="подтверждаю")
    assert saved["bookings"] == [], "подмена объекта/слота агентом не приводит к брони"


def test_match_slot_accepts_space_form_slots():
    """Слоты из БД/фикстур приходят и через пробел — разбор не должен падать (критика A)."""
    spaced = ["2026-10-01 10:00", "2026-10-01 11:00"]
    assert match_slot("11:00", spaced) == "2026-10-01 11:00"
    assert match_slot("01.10 в 11:00", spaced) == "2026-10-01 11:00"
    assert match_slot("10:00", spaced) == "2026-10-01 10:00"


def test_match_slot_ignores_garbage_slots():
    """Мусорный слот не роняет ход и не подменяет выбор."""
    assert match_slot("11:00", ["без времени", "2026-10-01T11:00"]) == "2026-10-01T11:00"
    assert match_slot("11:00", ["без времени"]) is None
    assert match_slot("11:00", []) is None


# --- шаг слота: время мимо списка отвечаем сами -------------------------------------
#
# На «в 10:30» (слоты идут по часам) агент отвечает общим «уточните, что именно вас интересует».
# Ответ известен заранее — назвать, что такого времени нет, и показать сетку, — поэтому он
# детерминированный: агент не зовётся, деньги не тратятся, история и трасса пишутся.


async def test_unavailable_time_is_answered_without_the_agent():
    import time as _time

    from test_dialog import make_service

    from realty_agent.dialog import ChatState as _ChatState

    called: list[str] = []

    async def ask_agent(user: str, text: str):
        called.append(text)
        return type("Reply", (), {"text": "АГЕНТ ОТВЕТИЛ", "done": True})()

    state = _ChatState(platform="tg", chat_id="5", step="slot", listing_id="L-1",
                       offered_slots=["2026-10-01T10:00", "2026-10-01T11:00"], offered_at=_time.time())
    svc, _saved = make_service(agent_text="АГЕНТ ОТВЕТИЛ", state=state)
    svc._ask = ask_agent

    reply = await svc.handle_message(platform="tg", chat_id="5", text="в 10:30")

    assert not called, "агент не должен вызываться: ответ известен заранее"
    assert "10:30" in reply.text, "клиент обязан узнать про СВОЁ время, а не общее «уточните»"
    assert "просмотров нет" in reply.text
    assert "АГЕНТ ОТВЕТИЛ" not in reply.text
    assert reply.rows, "альтернативы показываются сеткой, а не текстом «свободны: …»"


async def test_time_outside_the_offered_list_keeps_the_step_and_shows_the_grid():
    import time as _time

    from test_dialog import make_service

    from realty_agent.dialog import ChatState as _ChatState

    state = _ChatState(platform="tg", chat_id="6", step="slot", listing_id="L-1",
                       offered_slots=["2026-10-01T10:00"], offered_at=_time.time())
    svc, saved = make_service(agent_text="АГЕНТ", state=state)

    await svc.handle_message(platform="tg", chat_id="6", text="а можно 25 октября в 10:30?")

    #: Шаг НЕ двигается: слота нет, выбирать нечего. И объект не теряется.
    assert saved["state"].step == "slot"
    assert saved["state"].slot is None
    assert saved["state"].listing_id == "L-1"


async def test_generic_question_at_slot_step_still_goes_to_the_agent():
    """Без явного времени/даты это ВОПРОС, а не выбор — решает агент, как и раньше."""
    import time as _time

    from test_dialog import make_service

    from realty_agent.dialog import ChatState as _ChatState

    called: list[str] = []

    async def ask_agent(user: str, text: str):
        called.append(text)
        return type("Reply", (), {"text": "АГЕНТ ОТВЕТИЛ", "done": True})()

    state = _ChatState(platform="tg", chat_id="7", step="slot", listing_id="L-1",
                       offered_slots=["2026-10-01T10:00"], offered_at=_time.time())
    svc, _saved = make_service(agent_text="АГЕНТ ОТВЕТИЛ", state=state)
    svc._ask = ask_agent

    reply = await svc.handle_message(platform="tg", chat_id="7", text="а что ещё есть?")

    assert called, "вопрос без времени обязан идти агенту"
    assert "АГЕНТ ОТВЕТИЛ" in reply.text


async def test_unavailable_slot_reply_joins_the_history():
    """У этой ветки те же побочные записи, что у любого хода: иначе ответ выпадал бы из истории."""
    import time as _time

    from test_dialog import make_service

    from realty_agent.dialog import ChatState as _ChatState

    recorded: list[dict] = []

    async def recorder(**kwargs):
        recorded.append(kwargs)

    state = _ChatState(platform="tg", chat_id="8", step="slot", listing_id="L-1",
                       offered_slots=["2026-10-01T10:00"], offered_at=_time.time())
    svc, _saved = make_service(agent_text="АГЕНТ", state=state, record_message=recorder)

    await svc.handle_message(platform="tg", chat_id="8", text="в 23:45")

    assert [item["role"] for item in recorded] == ["user", "assistant"]
    assert "23:45" in recorded[1]["text"]
