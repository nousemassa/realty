"""Раскладки кнопок и карточки: подписи, индексы вместо данных, группировка по дням."""
from __future__ import annotations

import re

from agent_core.ui.reply import Action
from realty_agent.channels.turn_context import Listing
from realty_agent.ui import format as fmt
from realty_agent.ui import keyboards as kb
from realty_agent.ui import menu
from realty_agent.ui.kinds import (
    KIND_BACK,
    KIND_CONFIRM,
    KIND_CONSENT,
    KIND_DAY,
    KIND_LISTING,
    KIND_MENU,
    KIND_PHONE_REQUEST,
    KIND_POLICY,
    KIND_TIME,
)

LISTINGS = [
    Listing(id="L-1", address="ул. Пушкина, 10, кв. 42", price="8900000", rooms="2"),
    Listing(id="L-2", address="пр. Мира, 3, кв. 15", price="6400000", rooms="1"),
]
OFFERED = ["2026-10-02T10:00", "2026-10-02T11:00", "2026-10-03T10:00"]
DAY_LABEL_RE = re.compile(r"^\d{2}\.\d{2} (пн|вт|ср|чт|пт|сб|вс)$")


def test_short_price_and_rooms():
    assert fmt.short_price("8900000") == "8,9 млн ₽"
    assert fmt.short_price("520 000 ₽") == "520 тыс ₽"
    assert fmt.short_price("") == ""
    assert fmt.rooms_label("2") == "2к"
    assert fmt.rooms_label("2-комн., 54 м²") == "2-комн., 54 м²"


def test_day_and_time_labels_from_slot_strings():
    assert fmt.time_label("2026-10-02T10:00") == "10:00"
    assert DAY_LABEL_RE.match(fmt.day_label("2026-10-02T10:00"))
    assert fmt.pretty_date("2026-10-02T10:00").endswith("10:00 МСК")
    assert fmt.time_label("2026-10-02 10:00") == "10:00", "слоты приходят и с пробелом"


def test_object_labels_are_indexed_and_short():
    rows = kb.listings_kb(LISTINGS)
    assert len(rows) == 2, "по одной кнопке в ряд (длинные адреса)"
    assert rows[0][0].arg == "0" and rows[1][0].arg == "1"
    assert rows[0][0].label.startswith("1 · ул. Пушкина, 10"), "улица + номер дома, без квартиры"
    assert "8,9 млн ₽" in rows[0][0].label
    assert all(action.kind == KIND_LISTING for row in rows for action in row)


def test_day_grid_groups_slots_by_day():
    rows = kb.day_grid(OFFERED)
    flat = [action for row in rows for action in row]
    assert [action.arg for action in flat] == ["0", "1"], "два уникальных дня"
    assert all(DAY_LABEL_RE.match(action.label) for action in flat)
    assert all(action.kind == KIND_DAY for action in flat)


def test_time_grid_args_are_offered_indices():
    rows = kb.time_grid(OFFERED, 0)
    times = [action for row in rows for action in row if action.kind == KIND_TIME]
    assert [action.label for action in times] == ["10:00", "11:00"]
    assert [action.arg for action in times] == ["0", "1"], "индекс в общем списке предложенного"
    assert rows[-1][0].kind == KIND_BACK, "есть возврат ко всем дням"


def test_time_grid_unknown_day_is_empty():
    assert kb.time_grid(OFFERED, 5) == []
    assert kb.time_grid([], 0) == []


def test_space_form_slots_group_like_t_form():
    """Обе законные формы слота — один и тот же день (грабля: split("T") на пробеле)."""
    spaced = ["2026-10-02 10:00", "2026-10-02 11:00", "2026-10-03 12:00"]
    days = [action.arg for row in kb.day_grid(spaced) for action in row]
    assert days == ["0", "1"], "два дня, а не «день = весь слот»"
    times = [action.label for row in kb.time_grid(spaced, 0) for action in row
             if action.kind == KIND_TIME]
    assert times == ["10:00", "11:00"], "оба времени дня достижимы"


def test_labels_survive_garbage_slot():
    """Мусор в слоте не должен ронять ход (критика A: ValueError в разборе)."""
    assert fmt.day_label("без времени") == "без времени"
    assert fmt.time_label("") == "—"
    assert fmt.pretty_date("2026-13-99T99:99") == "2026-13-99T99:99"
    assert kb.day_grid(["мусор"]) == [[Action("мусор", KIND_DAY, "0")]]


def test_confirm_keyboard_has_single_primary_first():
    rows = kb.confirm_kb()
    assert rows[0][0].kind == KIND_CONFIRM
    assert "Подтвердить" in rows[0][0].label
    assert len(rows[0]) == 1, "primary — один в ряду (образец интерфейса)"


def test_client_menu_rows_and_codes():
    """Ряды меню: [Объекты|Записаться], [Запрос звонка], [Политика|Я менеджер]."""
    rows = kb.client_menu_kb()
    assert [len(row) for row in rows] == [2, 1, 2]
    codes = {action.arg for row in rows for action in row}
    assert codes == {menu.MENU_OBJECTS, menu.MENU_BOOK, menu.MENU_MANAGER, menu.MENU_POLICY, menu.MENU_APPLY}
    assert all(action.kind == KIND_MENU for row in rows for action in row)


def test_manager_call_row_is_alone():
    """Подпись «📞 Запрос звонка от менеджера» — в своём ряду (иначе Telegram переносит её
    на две строки и выравнивает перенос по левому краю — «надпись не по центру»)."""
    rows = kb.client_menu_kb()
    manager_rows = [row for row in rows if any(a.arg == menu.MENU_MANAGER for a in row)]
    assert len(manager_rows) == 1 and len(manager_rows[0]) == 1
    assert manager_rows[0][0].label == "📞 Запрос звонка от менеджера"


def test_client_menu_keeps_all_items_after_layout_change():
    """Состав пунктов не пострадал от переноса подписи в свой ряд."""
    codes = [a.arg for row in kb.client_menu_kb() for a in row]
    for code in (menu.MENU_OBJECTS, menu.MENU_BOOK, menu.MENU_MANAGER, menu.MENU_POLICY, menu.MENU_APPLY):
        assert code in codes


def test_menu_label_map_is_complete():
    """Инвариант: каждая метка меню обрабатывается либо роутером, либо овнер-правами, либо транспортом."""
    from realty_agent.tg import bot as tg_bot
    from realty_agent.tg.menu_router import CLIENT_CODES, TRANSPORT_CODES

    covered = set(CLIENT_CODES) | set(tg_bot.OWNER_CODE_RIGHTS) | set(TRANSPORT_CODES)
    assert covered == set(menu.LABELS), f"метки без обработчика: {set(menu.LABELS) - covered}"


def test_owner_menu_shows_counter_and_pause_state():
    paused = kb.owner_menu_kb(pending=3, paused=True)
    assert paused[0][0].label == "📋 Заявки (3)"
    assert paused[1][0].arg == menu.MENU_OWNER_MANAGERS, "второй ряд — управление менеджерами"
    assert paused[2][0].arg == menu.MENU_OWNER_RESUME, "на паузе предлагаем запуск"
    running = kb.owner_menu_kb(pending=0, paused=False)
    assert running[2][0].arg == menu.MENU_OWNER_PAUSE


def test_consent_keyboard_has_consent_and_policy():
    rows = kb.consent_kb()
    assert rows[0][0].kind == KIND_CONSENT
    assert rows[0][1].kind == KIND_POLICY, "политика — безподписная кнопка, а не пункт reply-меню"
    assert rows[0][1].label == menu.LABELS[menu.MENU_POLICY]


def test_client_card_masks_phone_and_lists_fields():
    card = fmt.client_card(listing=LISTINGS[0], slot_iso="2026-10-02T10:00",
                           name="Иван Петров", phone_mask="+7 999 •••-••-67")
    assert "Проверьте заявку" in card
    for title in ("Объект", "Дата и время", "Имя", "Телефон"):
        assert title in card
    assert "+7 999 •••-••-67" in card
    assert "2026-10-02T10:00" not in card, "в карточке — человекочитаемая дата"


def test_manager_card_shows_contact_and_id():
    card = fmt.manager_card(booking_id=12, listing=LISTINGS[1], slot_iso="2026-10-02T10:00",
                            name="Иван", phone="+79991234567")
    assert "#12" in card and "заявка" in card.lower()
    assert "+79991234567" in card, "менеджер видит полный телефон"
    assert "10:00 МСК" in card
    assert "пр. Мира, 3" in card, "адрес с номером дома"


def test_report_text_has_numbers_without_pii():
    report = fmt.report_text(day="03.10", total=12, confirmed=7, rejected=1, pending=4,
                             upcoming=5, dialogues=9, llm_turns=14, cost_usd=1.13)
    assert "Заявки: 12" in report and "ожидают 4" in report
    assert "LLM-ходов: 14" in report and "$1.13" in report
    no_cost = fmt.report_text(day="03.10", total=0, confirmed=0, rejected=0, pending=0,
                              upcoming=0, dialogues=0, llm_turns=0, cost_usd=None)
    assert "нет данных" in no_cost


# ---------- переименование и «👤 Я менеджер» как обязательный пункт ----------

def test_manager_call_label_renamed_and_old_label_is_alias():
    """Новая метка резолвится; старая остаётся алиасом (клавиатуры, отправленные до обновления)."""
    assert menu.label(menu.MENU_MANAGER) == "📞 Запрос звонка от менеджера"
    assert menu.resolve_menu("📞 Запрос звонка от менеджера") == menu.MENU_MANAGER
    assert menu.resolve_menu("📞 Менеджер") == menu.MENU_MANAGER


def test_two_buttons_do_not_cross_resolve():
    """«Запрос звонка» и «👤 Я менеджер» — разные кнопки, метки не пересекаются."""
    assert menu.MENU_MANAGER != menu.MENU_APPLY
    assert menu.resolve_menu(menu.label(menu.MENU_MANAGER)) != menu.MENU_APPLY
    assert menu.resolve_menu(menu.label(menu.MENU_APPLY)) != menu.MENU_MANAGER
    assert menu.resolve_menu(menu.label(menu.MENU_APPLY)) == menu.MENU_APPLY


def _codes(rows) -> set[str]:
    return {action.arg for row in rows for action in row if action.kind == KIND_MENU}


def test_apply_button_present_in_every_client_keyboard():
    """«👤 Я менеджер» есть в каждой reply-клавиатуре клиентского пути (кроме согласия)."""
    assert menu.MENU_APPLY in _codes(kb.client_menu_kb())
    assert menu.MENU_APPLY in _codes(kb.done_menu_kb())
    assert menu.MENU_APPLY in _codes(kb.phone_request_kb())
    assert menu.MENU_MANAGER in _codes(kb.client_menu_kb()), "запрос звонка тоже остаётся в меню"


def test_done_menu_is_full_client_menu_without_duplicates():
    """После заявки меню не урезано и без дублей ряда [звонок, политика]."""
    rows = kb.done_menu_kb()
    actions = [action for row in rows for action in row if action.kind == KIND_MENU]
    labels = [action.label for action in actions]
    assert len(labels) == len(set(labels)), f"дубли подписей в меню после заявки: {labels}"
    codes = [action.arg for action in actions]
    assert codes.count(menu.MENU_MANAGER) == 1
    assert codes.count(menu.MENU_POLICY) == 1
    assert menu.MENU_OBJECTS in codes


def test_phone_step_keeps_phone_button_and_apply():
    """Кнопка номера на месте, ввод текстом по-прежнему возможен."""
    rows = kb.phone_request_kb()
    kinds = [action.kind for row in rows for action in row]
    assert KIND_PHONE_REQUEST in kinds
    assert KIND_MENU in kinds


# ---------- адрес для человека вместо внутреннего id ----------

def test_address_label_prefers_full_address_without_features():
    from realty_agent.ui import format as fmt2

    listings = [Listing(id="L-1", address="ул. Пушкина, 10, кв. 42 (2-комн., 54 м², 7/9 эт.)",
                        price="8900000", rooms="2")]
    assert fmt2.address_label("L-1", listings) == "ул. Пушкина, 10, кв. 42"
    assert fmt2.address_label("L-9", listings) == "L-9", "объекта нет в каталоге — остаётся id (резерв)"
    assert fmt2.address_label(None, listings) == "—"

def test_replace_listing_ids_in_client_text():
    """Если агент всё же упомянул id в тексте — клиент увидит адрес (страховка)."""
    from realty_agent.ui import format as fmt2

    listings = [Listing(id="L-1", address="ул. Пушкина, 10, кв. 42 (2-комн.)", price="1", rooms="2")]
    out = fmt2.replace_listing_ids("Сводка: объект L-1, подтвердите", listings)
    assert "L-1" not in out and "ул. Пушкина, 10, кв. 42" in out
    assert fmt2.replace_listing_ids("объект L-9", listings) == "объект L-9", "неизвестный id не трогаем"

