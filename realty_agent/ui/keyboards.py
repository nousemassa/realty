"""Сборка клавиатур: раскладка кнопок, без данных БД и без Telethon.

Кнопки слотов группируются ПО ДНЯМ (кнопка дня → времена): так список не превращается в простыню,
а `arg` остаётся коротким индексом в серверном whitelist предложенного (см. `dialog.py`).
Тип клавиатуры (inline/reply) задаёт вызывающий в `Reply.kind` — по построению, без догадок.

В проде: согласие (`consent_kb`), меню клиента/овнера/менеджера, подменю менеджеров и заявок
(`owner_managers_kb`, `manager_request_kb`, `requests_list_kb`), самозаявка (`apply_check_kb`).
Готовы, но ждут диспатча: `listings_kb`, `day_grid`, `time_grid`, `confirm_kb`, `edit_menu_kb`.
"""
from __future__ import annotations

from agent_core.ui.reply import Action
from realty_agent.channels.turn_context import Listing
from realty_agent.ui import format as fmt
from realty_agent.ui import menu
from realty_agent.ui.kinds import (
    KIND_APPLY,
    KIND_APPLY_NO,
    KIND_APPLY_OK,
    KIND_APPLY_PICK,
    KIND_BACK,
    KIND_CANCEL,
    KIND_CONFIRM,
    KIND_CONSENT,
    KIND_DAY,
    KIND_EDIT,
    KIND_LISTING,
    KIND_MENU,
    KIND_OWNER,
    KIND_OWNER_OFF,
    KIND_OWNER_PICK,
    KIND_PHONE_REQUEST,
    KIND_POLICY,
    KIND_TIME,
)

DAYS_PER_ROW = 3
TIMES_PER_ROW = 3


def _chunk(actions: list[Action], per_row: int) -> list[list[Action]]:
    return [actions[i:i + per_row] for i in range(0, len(actions), per_row)]


def _menu_action(code: str, count: int | None = None) -> Action:
    return Action(menu.label(code, count), KIND_MENU, code)


def consent_kb() -> list[list[Action]]:
    """Согласие: «Согласен(а)» и «📄 Политика» — обе без подписи (только показ текста)."""
    return [[
        Action("✅ Согласен(а)", KIND_CONSENT),
        Action(menu.LABELS[menu.MENU_POLICY], KIND_POLICY),
    ]]


def client_menu_kb() -> list[list[Action]]:
    """Постоянное меню клиента (reply-клавиатура).

    «📞 Запрос звонка от менеджера» стоит в ОТДЕЛЬНОМ ряду (): в паре с короткой «📄 Политика»
    клиент Telegram переносил длинную подпись на две строки и выравнивал перенос по левому краю —
    в своём ряду кнопка занимает всю ширину и подпись остаётся однострочной (=> по центру).
    """
    return [
        [_menu_action(menu.MENU_OBJECTS), _menu_action(menu.MENU_BOOK)],
        [_menu_action(menu.MENU_MANAGER)],        #: длинная подпись — своя строка
        [_menu_action(menu.MENU_POLICY), _menu_action(menu.MENU_APPLY)],   #: самозаявка
    ]


def manager_menu_kb(pending: int, *, confirmed: int | None = None) -> list[list[Action]]:
    """Меню менеджера: очередь заявок и (после DD-6) его подтверждённые просмотры."""
    rows = [[_menu_action(menu.MENU_OWNER_BOOKINGS, pending)]]
    if confirmed is not None:
        rows.append([_menu_action(menu.MENU_OWNER_VIEWS, confirmed or None)])
    return rows


def owner_menu_kb(*, pending: int, paused: bool, confirmed: int | None = None) -> list[list[Action]]:
    """Меню овнера: счётчик в лейбле (образец 2); кнопка паузы показывает текущее состояние."""
    pause_code = menu.MENU_OWNER_RESUME if paused else menu.MENU_OWNER_PAUSE
    rows = [[_menu_action(menu.MENU_OWNER_BOOKINGS, pending)]]
    if confirmed is not None:
        rows.append([_menu_action(menu.MENU_OWNER_VIEWS, confirmed or None)])
    return [
        *rows,
        [_menu_action(menu.MENU_OWNER_MANAGERS)],
        [_menu_action(pause_code)],
        [_menu_action(menu.MENU_OWNER_REPORT), _menu_action(menu.MENU_OWNER_STATUS)],
    ]


def done_menu_kb() -> list[list[Action]]:
    """Меню после успешной заявки (): «🔄 Записаться ещё» + ПОЛНОЕ меню клиента.

    Почему полное: reply-клавиатура заменяется последней отправленной, поэтому урезанный набор
    стирал у клиента «🏠 Объекты», «📅 Записаться» и «👤 Я менеджер» — самозаявка становилась
    недоступной до /start (дефект). Ряд [Менеджер, Политика] уже входит в client_menu_kb.
    """
    return [[Action("🔄 Записаться ещё", KIND_MENU, menu.MENU_OBJECTS)], *client_menu_kb()]


def owner_managers_kb(pending: int = 0) -> list[list[Action]]:
    """Подменю управления менеджерами: выход обязателен; первым — заявки на роль менеджера ()."""
    # ВНИМАНИЕ: это INLINE-клавиатура ⇒ кнопка = inline-действие (KIND_OWNER), а не reply-метка:
    # reply-метка здесь пришла бы callback'ом с видом `m`, которого нет в диспатче (молчаливый no-op).
    request_row = [Action(menu.label(menu.MENU_OWNER_REQUESTS, pending or None), KIND_OWNER, "requests")]
    return [
        request_row,
        [Action("➕ Добавить", KIND_OWNER, "add")],
        [Action("📋 Список", KIND_OWNER, "list"), Action("🗑 Удалить", KIND_OWNER, "del")],
        [Action("❌ Отмена", KIND_CANCEL, "flow")],
    ]


def manager_request_kb(request_id) -> list[list[Action]]:
    """Карточка заявки владельцу: утвердить/отклонить (arg — id заявки; ключ прав — chat_id)."""
    return [
        [Action("✅ Утвердить", KIND_APPLY_OK, str(request_id))],
        [Action("❌ Отклонить", KIND_APPLY_NO, str(request_id))],
    ]


def requests_list_kb(requests: list[dict]) -> list[list[Action]]:
    """Список ожидающих заявок: по одной кнопке-заявке на ряд + выход из подменю."""
    rows = [[Action(f"{fmt.nick_at(row['username'])} · {row['created_at']:%d.%m %H:%M}", KIND_APPLY_PICK, str(row["id"]))]
            for row in requests]
    return [*rows, [Action("❌ Отмена", KIND_CANCEL, "flow")]]


def phone_request_kb() -> list[list[Action]]:
    """Шаг телефона (): reply-кнопка запроса номера + «👤 Я менеджер».

    Второй ряд — требование владельца (): самозаявка «это отдельный функционал для теста бота,
    его убирать нельзя». Ввод номера текстом остаётся рабочим, так что шаг не ломается.
    Тип REPLY — смешивать с inline нельзя.
    """
    return [
        [Action("📱 Отправить мой номер", KIND_PHONE_REQUEST)],
        [_menu_action(menu.MENU_APPLY)],
    ]


def apply_check_kb() -> list[list[Action]]:
    """Заявителю без ника: перепроверить профиль после того, как он завёл @username (H8: нет UI_SECRET ⇒ None)."""
    return [[Action("🔄 Проверить снова", KIND_APPLY, "check")]]


def manager_pick_kb(managers: list[dict]) -> list[list[Action]]:
    """Кого убираем: по кнопке на активного менеджера (arg — chat_id, ключ прав, НЕ ник)."""
    rows = [[Action(f"🗑 {fmt.nick_at(row['name'])}", KIND_OWNER_PICK, str(row["chat_id"]))]
            for row in managers if row.get("active")]
    return [*rows, [Action("❌ Отмена", KIND_CANCEL, "flow")]]


def manager_confirm_kb(chat_id: str) -> list[list[Action]]:
    """Подтверждение снятия: необратимого удаления нет — менеджер лишь теряет права."""
    return [
        [Action("🗑 Убрать", KIND_OWNER_OFF, str(chat_id))],
        [Action("❌ Отмена", KIND_CANCEL, "flow")],
    ]


def flow_cancel_kb() -> list[list[Action]]:
    """Выход из шага ввода ника (иначе флоу висел бы до тайм-аута)."""
    return [[Action("❌ Отмена", KIND_CANCEL, "flow")]]


def listings_kb(listings: list[Listing]) -> list[list[Action]]:
    """Объекты: по одной кнопке в ряд — длинные адреса не влезают в 2-3 колонки."""
    return [[Action(fmt.object_label(i, item), KIND_LISTING, str(i - 1))]
            for i, item in enumerate(listings, start=1)]


def _day_key(slot: str) -> str:
    """Дата слота в канонической форме: «2026-10-02 10:00» и «…T10:00» — один день."""
    return fmt.canonical(slot).split("T")[0]


def _days(offered: list[str]) -> list[str]:
    days: list[str] = []
    for slot in offered:
        day = _day_key(slot)
        if day and day not in days:
            days.append(day)
    return days


def day_grid(offered: list[str]) -> list[list[Action]]:
    """Уникальные дни из предложенных слотов; `arg` — индекс дня (порядок сохраняется)."""
    labels = []
    for index, day in enumerate(_days(offered)):
        first = next(slot for slot in offered if _day_key(slot) == day)
        labels.append(Action(fmt.day_label(first), KIND_DAY, str(index)))
    return _chunk(labels, DAYS_PER_ROW)


def time_grid(offered: list[str], day_index: int) -> list[list[Action]]:
    """Времена выбранного дня; `arg` — индекс слота в общем списке `offered`."""
    days = _days(offered)
    if not (0 <= day_index < len(days)):
        return []
    day = days[day_index]
    actions = [Action(fmt.time_label(slot), KIND_TIME, str(index))
               for index, slot in enumerate(offered) if _day_key(slot) == day]
    return [*_chunk(actions, TIMES_PER_ROW), [Action("‹ Все дни", KIND_BACK)]]


def confirm_kb() -> list[list[Action]]:
    """Карточка подтверждения: одна широкая primary + вторичные действия отдельным рядом."""
    return [
        [Action("✅ Подтвердить запись", KIND_CONFIRM)],
        [Action("✏️ Изменить", KIND_EDIT), Action("❌ Отменить", KIND_CANCEL)],
    ]


def edit_menu_kb() -> list[list[Action]]:
    """Что именно меняем (правка поля сохраняет остальные)."""
    return [
        [Action("Объект", KIND_EDIT, "listing"), Action("Дата и время", KIND_EDIT, "slot")],
        [Action("Имя", KIND_EDIT, "name"), Action("Телефон", KIND_EDIT, "phone")],
    ]
