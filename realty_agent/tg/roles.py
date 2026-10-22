"""Роли чата и права: `owner` | `manager` | `client`.

Почему отдельный модуль: до«роль» проверялась в ≥5 местах как равенство одному chat_id
(`can_confirm`, `_send_pending`, `_notify_manager`, `_greet_manager`, `_booking_buttons`). Теперь роль
определяется один раз и все гейты идут через `may`.
"""
from __future__ import annotations

import logging

log = logging.getLogger("realty_agent.tg")

OWNER = "owner"
MANAGER = "manager"
CLIENT = "client"

_RANK = {CLIENT: 0, MANAGER: 1, OWNER: 2}

# Признак → минимальная роль. Неизвестный признак — только владелец.
ROLE_RIGHTS: dict[str, str] = {
    "view_pending": MANAGER,
    "confirm": MANAGER,
    "view_views": MANAGER,     # DD-6: свои подтверждённые просмотры
    "pause": OWNER,
    "resume": OWNER,
    "report": OWNER,
    "status": OWNER,
    "export": OWNER,
    "manage_managers": OWNER,
}


def _configured(value: object) -> str:
    return str(value or "")


async def resolve_role(chat_id: str, *, settings, db) -> str:
    """Роль чата. БД недоступна ⇒ судим по настройкам (fail-closed: посторонний не станет владельцем)."""
    owner = _configured(getattr(settings, "telegram_owner_chat_id", ""))
    legacy = _configured(getattr(settings, "telegram_manager_chat_id", ""))
    if owner and chat_id == owner:
        return OWNER
    if legacy and chat_id == legacy:
        # M4: legacy-чат — менеджер, но снятие в таблице (`active=false`) его тоже снимает.
        try:
            if not await db.manager_row_exists(chat_id) or await db.is_active_manager(chat_id):
                return MANAGER
            return CLIENT
        except Exception as error:  # noqa: BLE001 — БД недоступна: остаёмся на настройках
            log.warning("роль legacy-чата: БД недоступна (%s) — считаем менеджером", type(error).__name__)
            return MANAGER
    try:
        return MANAGER if await db.is_active_manager(chat_id) else CLIENT
    except Exception as error:  # noqa: BLE001 — контур БД: роль не должна ронять ход
        log.warning("роль: БД недоступна (%s) — остаёмся на настройках", type(error).__name__)
        return CLIENT


def may(role: str, capability: str) -> bool:
    needed = ROLE_RIGHTS.get(capability)
    if needed is None:
        return role == OWNER
    return _RANK.get(role, 0) >= _RANK[needed]


def is_owner_chat(chat_id: str, *, settings) -> bool:
    owner = _configured(getattr(settings, "telegram_owner_chat_id", ""))
    return bool(owner) and chat_id == owner
