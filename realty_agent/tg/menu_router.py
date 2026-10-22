"""Маршрутизация reply-меток меню: клиентские пункты и очередь заявок.

Почему отдельный модуль: `bot.py` остаётся транспортом (lifecycle, хендлеры, гейты), а сборка ответов
на тапы живёт здесь и не знает про Telethon — её тестируют офлайн (плана).
"""
from __future__ import annotations

import logging

from agent_core.ui.reply import INLINE, Reply
from realty_agent.tg.handlers import (
    OBJECTS_EMPTY,
    OBJECTS_HINT,
    OBJECTS_TITLE,
    book_ask_text,
    manager_contact_text,
)
from realty_agent.ui import format as fmt
from realty_agent.ui import menu
from realty_agent.ui.keyboards import listings_kb

log = logging.getLogger("realty_agent.tg")

# Клиентские пункты (овнер-коды обрабатывает транспорт: очередь — это N сообщений, не один Reply).
CLIENT_CODES = (menu.MENU_OBJECTS, menu.MENU_BOOK, menu.MENU_MANAGER, menu.MENU_POLICY)
# «👤 Я менеджер» — код клиентского меню, но обрабатывает его транспорт: нужен профиль отправителя
# (get_sender), которого у чистого роутера нет. Держим его здесь, чтобы карта меток была полной
# (CLIENT_CODES ∪ OWNER_CODE_RIGHTS ∪ TRANSPORT_CODES == LABELS).
TRANSPORT_CODES = (menu.MENU_APPLY,)


class MenuRouter:
    """Собирает ответ на тап меню. `None` = «код не наш» ⇒ ход идёт обычным путём.

    Дополнительно умеет строить каталог из ПЕРЕДАННОГО списка (фильтр клиента, DD-7/DD-8) —
    нумерация при этом совпадает с тем, что видит клиент (DD-1).
    """

    def __init__(self, *, settings, listings, load_state, count_listings=None) -> None:
        self._settings = settings
        self._listings = listings
        self._load_state = load_state
        self._count_listings = count_listings

    async def route(self, code: str, *, chat_id: str, role: str) -> Reply | None:
        if code not in CLIENT_CODES:
            return None
        if code == menu.MENU_POLICY:
            return Reply(text=_privacy_text())
        if code == menu.MENU_MANAGER:
            return Reply(text=manager_contact_text(getattr(self._settings, "manager_contact", "")))
        state = await self._load_state("tg", chat_id)
        listings = await self._listings()
        if code == menu.MENU_BOOK and getattr(state, "listing_id", None):
            return Reply(text=book_ask_text(fmt.address_for(state.listing_id, listings)))
        # Этап 3: объекты выбираются кнопками (индекс — в серверном списке), текст остаётся фолбэком.
        rows = listings_kb(listings) if listings else None
        return Reply(text=_objects_text(listings, await self.limit_note(len(listings))), rows=rows or [], kind=INLINE)

    async def limit_note(self, shown: int) -> str:
        """Приписка «Показаны первые N из M» — честное M берём из счётчика, а не из среза (DD-4)."""
        if self._count_listings is None:
            return ""
        try:
            total = int(await self._count_listings())
        except Exception as error:  # noqa: BLE001 — приписка не важнее каталога
            log.warning("счётчик объектов недоступен: %s", type(error).__name__)
            return ""
        return fmt.catalog_note(total, shown)


def _objects_text(listings, note: str = "") -> str:
    if not listings:
        # M11: при пустом каталоге не просим «напишите номер объекта» — объектов нет
        return f"{OBJECTS_TITLE}\n{OBJECTS_EMPTY}"
    body = f"{OBJECTS_TITLE}\n{fmt.object_lines(listings)}\n\n{OBJECTS_HINT}"
    return f"{body}\n\n{note}" if note else body


def _privacy_text() -> str:
    from realty_agent.tg.handlers import PRIVACY_TEXT

    return PRIVACY_TEXT
