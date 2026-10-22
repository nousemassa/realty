"""Овнер-флоу управления менеджерами: подменю, ввод ника, список, снятиеb).

Почему отдельный модуль: `bot.py` остаётся транспортом (роль, гейты, диспатч по типу входа), а
логика «добавить менеджера по нику» живёт здесь. Единственная зависимость от Telegram — резолв ника
через инъектируемый `client_getter` (один шов `client.get_entity`), поэтому флоу тестируется офлайн.

Инварианты:
  * ключ прав — `chat_id` (= `user_id` в Telegram), ник живёт только как подпись; переиспользование
    ника в Telegram не переносит прав (у нового владельца ника другой `user_id`);
  * управление — только владелец в личном чате; пустой `TELEGRAM_OWNER_CHAT_ID` ⇒ меню недоступно;
  * состояние флоу — в ОТДЕЛЬНЫХ колонках `conversations.owner_flow(_at)`, а не в `continuum`
    (тот перезаписывается целиком на каждом ходе клиентского диалога);
  * флоу живёт 10 минут и НЕ съедает обычный текст: без активного флоу слова идут агенту как раньше.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from agent_core.ui.reply import INLINE, REPLY, Reply
from realty_agent.tg.handlers import (
    FLOW_CANCELLED,
    MANAGERS_ADD_ASK,
    MANAGERS_ADDED,
    MANAGERS_ADDED_NEED_START,
    MANAGERS_EMPTY,
    MANAGERS_EXISTS,
    MANAGERS_FORMAT_ERR,
    MANAGERS_MENU_HINT,
    MANAGERS_NO_NICK,
    MANAGERS_NOT_USER,
    MANAGERS_NOTHING_TO_REMOVE,
    MANAGERS_REMOVE_ASK,
    MANAGERS_REMOVE_LAST,
    MANAGERS_REMOVED,
    MANAGERS_SELF,
    MANAGERS_TITLE,
)
from realty_agent.ui.format import nick_at, nick_bare
from realty_agent.ui.keyboards import (
    flow_cancel_kb,
    manager_confirm_kb,
    manager_pick_kb,
    owner_managers_kb,
)

log = logging.getLogger("realty_agent.tg")

FLOW_ADD = "add_manager"
FLOW_DONE = "done"
FLOW_TTL_S = 600

CANCEL_WORDS = {"отмена", "отменить", "/cancel", "❌ отмена"}
# Ник: необязательные префиксы `https://`, `t.me/`, `@`; 5–32 символа латиницы/цифр/`_`.
USERNAME_RE = re.compile(r"(?:https?://)?(?:t\.me/)?@?([A-Za-z0-9_]{5,32})", re.IGNORECASE)


def normalize_username(raw: str) -> str | None:
    """«Nick», «@Nick», «t.me/nick», «https://t.me/@Nick» → «@nick»; мусор → None."""
    found = USERNAME_RE.fullmatch((raw or "").strip())
    return f"@{found.group(1).lower()}" if found else None


def manager_state(row: dict) -> str:
    """Состояние для списка: права есть только у активного, карточки — только у давшего /start."""
    if not row.get("active"):
        return "отключён"
    return "готов" if row.get("ready") else "ждёт /start"


def managers_list_text(rows: list[dict]) -> str:
    if not rows:
        return f"{MANAGERS_TITLE}\n{MANAGERS_EMPTY}"
    body = "\n".join(f"• {nick_at(row['name'])} — {manager_state(row)}" for row in rows)
    return f"{MANAGERS_TITLE}\n{body}"


class OwnerFlows:
    """Состояние овнер-флоу в БД + сборка ответов. Отправку делает транспорт (`_send`)."""

    def __init__(self, *, db, settings, client_getter: Callable[[], object], clock: Callable[[], float] | None = None) -> None:
        self._db = db
        self._settings = settings
        self._client_getter = client_getter
        self._clock = clock or time.monotonic

    @property
    def _owner_id(self) -> str:
        return str(getattr(self._settings, "telegram_owner_chat_id", "") or "")

    # ---------- состояние флоу ----------

    async def active(self, chat_id: str) -> str | None:
        """Активный флоу чата или None. Просроченный (10 мин) считается снятым."""
        flow, started_at = await self._db.load_owner_flow(platform="tg", chat_id=chat_id)
        if not flow:
            return None
        if started_at is not None and self._clock() - started_at > FLOW_TTL_S:
            log.info("owner_flow: тайм-аут %.0f с — снят (chat=%s)", self._clock() - started_at, chat_id)
            await self.clear(chat_id)
            return None
        return flow

    async def start(self, chat_id: str, flow: str) -> None:
        await self._db.set_owner_flow(platform="tg", chat_id=chat_id, flow=flow)

    async def clear(self, chat_id: str) -> None:
        await self._db.set_owner_flow(platform="tg", chat_id=chat_id, flow=None)

    # ---------- экраны ----------

    def submenu(self, *, pending_requests: int = 0) -> Reply:
        """Подменю «👥 Менеджеры»: первой строкой — заявки на роль менеджера ()."""
        return Reply(text=f"{MANAGERS_TITLE}\n{MANAGERS_MENU_HINT}",
                     rows=owner_managers_kb(pending=pending_requests), kind=INLINE)

    async def add_ask(self, chat_id: str) -> Reply:
        await self.start(chat_id, FLOW_ADD)
        return Reply(text=f"{MANAGERS_TITLE}\n{MANAGERS_ADD_ASK}", rows=flow_cancel_kb(), kind=INLINE)

    async def list_reply(self) -> Reply:
        return Reply(text=managers_list_text(await self._db.list_managers()), kind=INLINE)

    async def pick_reply(self) -> Reply:
        """Кого убираем: активные менеджеры; если их нет — честный текст, а не пустая клавиатура."""
        managers = [row for row in await self._db.list_managers() if row.get("active")]
        if not managers:
            return Reply(text=f"{MANAGERS_TITLE}\n{MANAGERS_NOTHING_TO_REMOVE}", kind=INLINE)
        body = "\n".join(f"• {nick_at(row['name'])} — {manager_state(row)}" for row in managers)
        return Reply(text=f"{MANAGERS_TITLE}\n{body}", rows=manager_pick_kb(managers), kind=INLINE)

    async def confirm_reply(self, target: str) -> Reply:
        nickname = await self._nickname(target)
        active = [row for row in await self._db.list_managers() if row.get("active")]
        text = MANAGERS_REMOVE_LAST if len(active) <= 1 else MANAGERS_REMOVE_ASK.format(nick=nickname)
        return Reply(text=text, rows=manager_confirm_kb(target), kind=INLINE)

    async def remove(self, *, target: str, actor: str) -> Reply:
        """Снятие прав: `active=false` (обратимо, аудит остаётся), не удаление строки."""
        nickname = await self._nickname(target)
        if target in {self._owner_id}:
            return Reply(text=MANAGERS_SELF, kind=INLINE)
        await self._db.deactivate_manager(chat_id=target, actor=actor)
        log.info("owner.manager_remove chat=%s nick=%s", target, nickname)
        return Reply(text=MANAGERS_REMOVED.format(nick=nickname), kind=INLINE)

    async def cancel_reply(self, chat_id: str) -> Reply:
        await self.clear(chat_id)
        return Reply(text=FLOW_CANCELLED, kind=REPLY)

    # ---------- ввод ника ----------

    async def handle_text(self, *, chat_id: str, text: str) -> Reply:
        """Шаг ввода ника. Ошибка формата/резолва флоу НЕ сбрасывает: владелец может повторить."""
        if text.strip().lower() in CANCEL_WORDS:
            return await self.cancel_reply(chat_id)
        nickname = normalize_username(text)
        if nickname is None:
            return Reply(text=MANAGERS_FORMAT_ERR, rows=flow_cancel_kb(), kind=INLINE)
        client = self._client_getter()
        if client is None:
            log.warning("owner_flow: транспорт недоступен — ник %s не проверен", nickname)
            return Reply(text=MANAGERS_NO_NICK, rows=flow_cancel_kb(), kind=INLINE)
        try:
            user = await client.get_entity(nickname)
        except Exception as error:  # noqa: BLE001 — сеть/Telegram/ник: любая ветка даёт адресный текст
            return Reply(text=self._resolve_error(nickname, error), rows=flow_cancel_kb(), kind=INLINE)

        user_id = str(getattr(user, "id", ""))
        if not user_id:
            return Reply(text=MANAGERS_NO_NICK, rows=flow_cancel_kb(), kind=INLINE)
        broadcast = getattr(user, "broadcast", None)
        megagroup = getattr(user, "megagroup", None)
        if (getattr(user, "bot", False) or broadcast is not None or megagroup is not None
                or not getattr(user, "username", None)):
            # Канал/супергруппа/бот/чат без ника: прав у них нет, ✅/❌ они не жмут (M5).
            return Reply(text=MANAGERS_NOT_USER, rows=flow_cancel_kb(), kind=INLINE)
        if user_id == self._owner_id:
            return Reply(text=MANAGERS_SELF, rows=flow_cancel_kb(), kind=INLINE)
        if await self._db.is_active_manager(user_id):
            return Reply(text=MANAGERS_EXISTS.format(nick=nick_bare(nickname)), kind=REPLY)

        await self._db.add_manager(
            chat_id=user_id, name=nick_bare(nickname), added_by=chat_id, role="manager"
        )
        log.info("owner.manager_add chat=%s nick=%s id=%s", chat_id, nickname, user_id)
        await self.clear(chat_id)
        return Reply(text=await self._greeting(nickname, user_id), kind=REPLY)

    async def _greeting(self, nickname: str, user_id: str) -> str:
        """Пробуем сразу выдать менеджеру инструкцию; отказ Telegram — штатная ветка «нажмите /start»."""
        client = self._client_getter()
        from realty_agent.tg.handlers import MANAGER_INSTRUCTION

        try:
            await client.send_message(int(user_id), MANAGER_INSTRUCTION)
        except Exception as error:  # noqa: BLE001 — «бот не может писать первым» ∈ штатных отказов
            log.info("owner.manager_add: приветствие не ушло (%s) — просим /start", type(error).__name__)
            return MANAGERS_ADDED_NEED_START.format(nick=nick_bare(nickname))
        return MANAGERS_ADDED.format(nick=nick_bare(nickname))

    @staticmethod
    def _resolve_error(nickname: str, error: Exception) -> str:
        name = type(error).__name__
        if name == "UsernameInvalidError":
            return MANAGERS_FORMAT_ERR
        if name == "FloodWaitError":
            return f"Telegram просит подождать {getattr(error, 'seconds', '—')} с. Попробуйте позже."
        if isinstance(error, ValueError):
            return MANAGERS_NO_NICK
        log.warning("owner_flow: резолв %s не удался (%s)", nickname, name)
        return MANAGERS_NO_NICK

    async def _nickname(self, chat_id: str) -> str:
        for row in await self._db.list_managers():
            if str(row["chat_id"]) == str(chat_id):
                return nick_bare(row["name"])
        return chat_id
