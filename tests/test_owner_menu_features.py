"""Пункты меню: пауза/запуск, отчёт и статус, уведомления; тексты клиента и разбор фильтра.

Проверяется то, что видит человек: пункт меню отвечает делом, а не заглушкой; счётчики и отчёт не
текут ПДн; уведомление не возвращается актору; подписанные кнопки не ломаются без секрета; негатив
с предлогом в тексте клиента — исключение, а не требование.
"""
from __future__ import annotations

from fakes import (
    MANAGER,
    OWNER,
    FakeCallback,
    FakeDb,
    FakeMsg,
    fake_client,
    make_settings,
    make_transport,
    queued_notices,
)

from realty_agent.tg import handlers
from realty_agent.ui import menu


def transport_for(db, **settings):
    t = make_transport(db, settings=make_settings(**settings) if settings else None)
    t._limiter._now = lambda: 1000.0
    t._client = fake_client()
    return t


def texts(event) -> str:
    return "\n".join(item[0] or "" for item in event.sent)


# ---------- пауза/запуск ----------

async def test_pause_from_menu_switches_mode_and_flips_button():
    local = FakeDb()
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_PAUSE), chat_id=OWNER)
    await t._on_message(event)

    assert ("bot_mode", "paused") in local.settings_writes, "пауза должна писаться в настройки"
    assert "паузе" in texts(event).lower()
    assert event.sent[0][1], "вместе с подтверждением приходит меню с перевёрнутой кнопкой (M9)";


async def test_resume_from_menu_switches_mode_back():
    local = FakeDb(bot_mode="paused")
    t = transport_for(local)
    await t._on_message(FakeMsg(menu.label(menu.MENU_OWNER_RESUME), chat_id=OWNER))

    assert ("bot_mode", "live") in local.settings_writes


async def test_paused_service_refuses_client_step():
    local = FakeDb(bot_mode="paused")
    t = transport_for(local)
    event = FakeMsg("хочу посмотреть квартиру", chat_id="777")
    await t._on_message(event)

    assert t._channels.calls == 0
    assert handlers.PAUSED_TEXT in texts(event)


# ---------- отчёт и статус ----------

async def test_report_shows_numbers_without_pii():
    local = FakeDb(pending=[{"id": 1, "client_name": "Аня", "client_phone": "+79990000000",
                             "listing_id": "L-1", "slot": "2026-10-05T10:00+03:00"}])
    local.bookings = [{"status": "confirmed"}]
    local.metrics = {"dialogues": 9, "llm_turns": 14}
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_REPORT), chat_id=OWNER)
    await t._on_message(event)

    body = texts(event)
    assert "Отчёт" in body and "Предстоящие просмотры" in body
    # Писатель расходов (трассы ходов) есть, поэтому «нет данных» в отчёте быть не должно:
    # если оно вернётся, значит числа снова перестали доходить до владельца.
    assert "расходы: $" in body, body
    assert "расходы: нет данных" not in body, body
    assert "+79990000000" not in body, "в отчёт не попадают ПД"


async def test_status_shows_mode_and_managers():
    local = FakeDb(managers={MANAGER: {"name": "petr", "active": True, "ready": True}})
    t = transport_for(local)
    event = FakeMsg(menu.label(menu.MENU_OWNER_STATUS), chat_id=OWNER)
    await t._on_message(event)

    body = texts(event)
    assert "Статус" in body and "принимает заявки" in body
    assert "1 активных" in body and "1 готовы" in body


async def test_owner_menu_stubs_are_replaced():
    """Пауза/отчёт/статус отвечают делом, а не заглушкой «Раздел появится на следующем этапе»."""
    for code in (menu.MENU_OWNER_PAUSE, menu.MENU_OWNER_REPORT, menu.MENU_OWNER_STATUS):
        local = FakeDb()
        t = transport_for(local)
        event = FakeMsg(menu.label(code), chat_id=OWNER)
        await t._on_message(event)
        assert handlers.SECTION_LATER_TEXT not in texts(event), f"{code} всё ещё заглушка"


# ---------- уведомления ----------


async def test_manager_tap_notifies_owner_and_ready_managers():
    local = FakeDb(managers={MANAGER: {"name": "petr", "active": True, "ready": True}})
    t = transport_for(local)
    await t._on_message(FakeMsg(menu.label(menu.MENU_MANAGER), chat_id="777"))

    recipients = {chat for chat, _ in queued_notices(t)}
    assert OWNER in recipients, "владелец должен узнать о тапе"
    assert MANAGER in recipients, "готовый менеджер тоже"
    assert t._client.sent == [], "прямой отправки быть не должно — уведомление идёт через очередь"


async def test_manager_tap_notification_is_debounced():
    local = FakeDb(managers={})
    t = transport_for(local)
    for _ in range(3):
        await t._on_message(FakeMsg(menu.label(menu.MENU_MANAGER), chat_id="777"))

    assert len([1 for chat, _ in queued_notices(t) if chat == OWNER]) == 1, "дебаунс: не чаще 1/мин"


async def test_pause_notification_skips_actor():
    local = FakeDb(managers={MANAGER: {"name": "petr", "active": True, "ready": True}})
    t = transport_for(local, telegram_owner_chat_id=OWNER, telegram_manager_chat_id=MANAGER)
    await t._on_message(FakeMsg(menu.label(menu.MENU_OWNER_PAUSE), chat_id=OWNER))

    recipients = {chat for chat, _ in queued_notices(t)}
    assert OWNER not in recipients, "актор не получает своё же уведомление"
    assert MANAGER in recipients


# ---------- тексты клиента ----------

async def test_client_start_uses_plan_greeting_without_llm():
    t = transport_for(FakeDb())
    event = FakeMsg("/start", chat_id="777")
    await t._on_message(event)

    assert t._channels.calls == 0, "приветствие клиента — фиксированный текст, а не LLM-ход"
    assert handlers.CLIENT_GREETING in texts(event)


async def test_empty_catalog_does_not_ask_number():
    t = transport_for(FakeDb(listings=[]))
    event = FakeMsg(menu.label(menu.MENU_OBJECTS))
    await t._on_message(event)

    body = texts(event)
    assert handlers.OBJECTS_EMPTY in body
    assert "Напишите номер объекта" not in body, "M11: у пустого каталога номер просить нечего"


# ---------- робастность и наблюдаемость ----------

async def test_signed_buttons_are_not_rendered_without_secret():
    """H8: без UI_SECRET подписанные кнопки были бы мёртвыми — их вообще не показываем."""
    from agent_core.ui.reply import INLINE, Reply
    from realty_agent.tg import render
    from realty_agent.ui.keyboards import manager_request_kb

    reply = Reply(text="карточка", rows=manager_request_kb(7), kind=INLINE)
    assert render.to_buttons(reply, secret="", chat_id=OWNER, token="") is None
    assert render.to_buttons(reply, secret="s" * 40, chat_id=OWNER, token="") is not None


async def test_unsigned_consent_buttons_survive_without_secret():
    from agent_core.ui.reply import INLINE, Reply
    from realty_agent.tg import render
    from realty_agent.ui.keyboards import consent_kb

    reply = Reply(text="согласие", rows=consent_kb(), kind=INLINE)
    buttons = render.to_buttons(reply, secret="", chat_id="777", token="")
    assert buttons, "согласие и политика — безподписные, они живут и без секрета"


async def test_metric_failure_does_not_break_tap():
    """Best-effort: сбой счётчика не мешает ответу на тап меню."""
    class BrokenMetrics(FakeDb):
        async def bump_metric(self, **kwargs):
            raise RuntimeError("db down")

    t = transport_for(BrokenMetrics())
    event = FakeMsg(menu.label(menu.MENU_OBJECTS))
    await t._on_message(event)

    assert event.sent, "клиент всё равно получает ответ, несмотря на сбой метрики"


# ---------- овнер-ветки: порядок и лимит ----------

async def test_owner_tap_order_and_limit():
    """Порядок ответов и лимит на овнер-ветках: тост/ответ есть, спам ограничен."""
    local = FakeDb()
    t = transport_for(local)
    for _ in range(11):
        await t._on_message(FakeMsg(menu.label(menu.MENU_OWNER_BOOKINGS), chat_id=OWNER))
    last = FakeMsg(menu.label(menu.MENU_OWNER_BOOKINGS), chat_id=OWNER)
    await t._on_message(last)

    assert handlers.TOO_OFTEN_TEXT in texts(last), "овнер-ветка тоже под антифлудом"


async def test_owner_branch_answers_before_dropping_buttons():
    """Тост обязан идти ДО снятия кнопок (иначе его подавляет edit())."""
    from agent_core.ui import tokens
    from realty_agent.ui.kinds import KIND_OWNER

    local = FakeDb(requests=[])
    t = transport_for(local)
    event = FakeCallback(tokens.sign("s" * 40, chat_id=OWNER, kind=KIND_OWNER, arg="list"), chat_id=OWNER)
    await t._on_callback(event)

    assert event.log and event.log[0] == "answer", f"сначала answer, потом edit: {event.log}"


# ---------- негативы с предлогами и сравнение по основе ----------

def test_negation_with_preposition_and_stem_matching():
    from fakes import LISTINGS_FLOORS

    from realty_agent.booking.filter import apply_filter, describe, parse_constraints

    spec = parse_constraints("выбери квартиры не у парка")
    assert "парк" in describe(spec) and describe(spec).startswith("без:"), describe(spec)
    assert not spec.get("keywords"), "позитив «парк» не должен остаться рядом с исключением"
    shown = [item.id for item in apply_filter(LISTINGS_FLOORS, spec)]
    assert "L-3" not in shown, "объект «вид на парк» исключается (формы «парка» ↔ «парк» сравниваются по основе)"
    assert len(shown) == 4


def test_positive_features_stay_positive():
    from fakes import LISTINGS_FLOORS

    from realty_agent.booking.filter import apply_filter, parse_constraints

    assert [x.id for x in apply_filter(LISTINGS_FLOORS, parse_constraints("у парка"))] == ["L-3"]
    assert [x.id for x in apply_filter(LISTINGS_FLOORS, parse_constraints("с ремонтом"))] == ["L-2"]
    assert [x.id for x in apply_filter(LISTINGS_FLOORS, parse_constraints("возле метро"))] == ["L-1"]

