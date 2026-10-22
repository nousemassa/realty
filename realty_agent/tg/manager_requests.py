"""Самозаявка «👤 Я менеджер»c,).

Почему отдельный модуль: поток не трогает ни диалог клиента, ни овнер-флоу — он про права.
Ключевые нормы, зашитые здесь:

* заявку может подать **только живой человек из личного чата** (группа/канал/бот отбиваются);
* ник берётся **из профиля Telegram** (текстового ввода нет), ключ прав — `chat_id` заявителя;
* одна ожидающая заявка на чат (дедуп на уровне БД), решение владельца — атомарное (CAS);
* уведомление о заявке получает **только владелец** (право `manage_managers`), без ПД;
* утверждение сразу делает менеджера «готовым» (он уже общается с ботом) и присылает ему меню менеджера.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from agent_core.ui.reply import INLINE, REPLY, Reply
from realty_agent.tg.handlers import (
    APPLY_ALREADY_MANAGER,
    APPLY_APPROVED,
    APPLY_NO_OWNER,
    APPLY_NO_USERNAME,
    APPLY_NOT_FOUND,
    APPLY_NOT_HUMAN,
    APPLY_OK,
    APPLY_OWNER,
    APPLY_PENDING,
    APPLY_PROFILE_FAIL,
    APPLY_REJECTED,
    REQUEST_APPROVED_TOAST,
    REQUEST_CARD,
    REQUEST_REJECTED_TOAST,
    REQUEST_REMINDER,
    REQUEST_UNDELIVERABLE,
    REQUESTS_EMPTY,
    REQUESTS_OVERFLOW,
    REQUESTS_TITLE,
)
from realty_agent.ui.format import nick_at, nick_bare
from realty_agent.ui.keyboards import apply_check_kb, manager_menu_kb, manager_request_kb, requests_list_kb

log = logging.getLogger("realty_agent.tg")

LIST_LIMIT = 10
REMIND_AFTER_S = 24 * 3600


class ManagerRequests:
    """Сборка ответов и решения по заявкам. Отправку делает транспорт (он же читает профиль)."""

    def __init__(
        self,
        *,
        db,
        settings,
        notify_owner: Callable[[str], Awaitable[None]],
        notify_applicant: Callable[..., Awaitable[bool]],
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._notify_owner = notify_owner
        self._notify_applicant = notify_applicant
        self._clock = clock or dt.datetime.now

    # ---------- заявка ----------

    async def apply(self, *, chat_id: str, sender, profile_error: str | None, role: str) -> Reply:
        """Проверки + создание заявки. Возвращает ответ заявителю (всегда, даже при отказе)."""
        if role == "owner":
            return Reply(text=APPLY_OWNER, kind=REPLY)
        if role == "manager":
            return Reply(text=APPLY_ALREADY_MANAGER, kind=REPLY)
        if (await self._db.pending_requests_count()
                and any(str(row["chat_id"]) == str(chat_id)
                        for row in await self._db.list_manager_requests(LIST_LIMIT))):
            return Reply(text=APPLY_PENDING, kind=REPLY)
        if profile_error is not None or sender is None:
            log.warning("заявка: профиль не прочитан (%s, chat=%s)", profile_error, chat_id)
            return Reply(text=APPLY_PROFILE_FAIL, rows=apply_check_kb(), kind=INLINE)
        if getattr(sender, "bot", False) or getattr(sender, "broadcast", None) is not None \
                or getattr(sender, "megagroup", None) is not None:
            return Reply(text=APPLY_NOT_HUMAN, kind=REPLY)
        nick = getattr(sender, "username", None)
        if not nick:
            return Reply(text=APPLY_NO_USERNAME, rows=apply_check_kb(), kind=INLINE)
        if not str(getattr(self._settings, "telegram_owner_chat_id", "") or ""):
            return Reply(text=APPLY_NO_OWNER, kind=REPLY)

        username = f"@{nick}"
        request = await self._db.create_manager_request(chat_id=str(chat_id), username=username)
        log.info("apply.request chat=%s nick=%s id=%s", chat_id, username, (request or {}).get("id"))
        await self._notify_owner(REQUEST_CARD.format(nick=nick))
        return Reply(text=APPLY_OK.format(nick=nick), kind=REPLY)

    # ---------- список и решение (владелец) ----------

    async def list_pending(self) -> Reply:
        rows = await self._db.list_manager_requests(LIST_LIMIT)
        total = await self._db.pending_requests_count()
        if not rows:
            return Reply(text=f"{REQUESTS_TITLE}\n{REQUESTS_EMPTY}", kind=REPLY)
        body = "\n".join(f"• {nick_at(row['username'])} — {self._fmt(row['created_at'])}" for row in rows)
        text = f"{REQUESTS_TITLE}\n{body}"
        if total > len(rows):
            text += f"\n{REQUESTS_OVERFLOW.format(rest=total - len(rows))}"
        return Reply(text=text, rows=requests_list_kb(rows), kind=INLINE)

    async def request_card(self, request_id) -> Reply:
        """Карточка одной заявки из списка (тап по строке) — с кнопками решения."""
        for row in await self._db.list_manager_requests(LIST_LIMIT):
            if str(row["id"]) == str(request_id):
                nick = nick_bare(row["username"])
                return Reply(text=REQUEST_CARD.format(nick=nick),
                             rows=manager_request_kb(row["id"]), kind=INLINE)
        return Reply(text=APPLY_NOT_FOUND, kind=REPLY)

    async def decide(self, *, request_id, status: str, actor: str) -> str:
        """CAS-решение. Уведомляет заявителя; возвращает тост владельцу."""
        decided = await self._db.decide_manager_request(
            request_id=int(request_id), status=status, actor=actor,
        )
        if decided is None:
            return APPLY_NOT_FOUND
        nick = nick_bare(decided["username"])
        applicant = str(decided["chat_id"])
        if status == "approved":
            #: меню утверждённого — с рядом «👁 Просмотры (N)» (у него пока своих нет — 0)
            rows = manager_menu_kb(await self._db.pending_count(),
                                   confirmed=await self._db.confirmed_count(chat_id=applicant))
            delivered = await self._notify_applicant(applicant, APPLY_APPROVED, rows, REPLY)
            log.info("apply.approved chat=%s nick=%s by=%s delivered=%s", applicant, nick, actor, delivered)
            if not delivered:
                await self._notify_owner(REQUEST_UNDELIVERABLE.format(nick=nick))
            return REQUEST_APPROVED_TOAST.format(nick=nick)
        await self._notify_applicant(applicant, APPLY_REJECTED, None, REPLY)
        log.info("apply.rejected chat=%s nick=%s by=%s", applicant, nick, actor)
        return REQUEST_REJECTED_TOAST.format(nick=nick)

    async def remind_if_stale(self) -> None:
        """Одно напоминание владельцу на заявку (не чаще раза в сутки)."""
        for row in await self._db.list_manager_requests(LIST_LIMIT):
            age = self._clock() - row["created_at"].timestamp() if row.get("created_at") else 0
            if age >= REMIND_AFTER_S:
                await self._notify_owner(REQUEST_REMINDER.format(nick=nick_bare(row["username"])))
                await self._db.mark_request_reminded(row["id"])

    @staticmethod
    def _fmt(value) -> str:
        return f"{value:%d.%m %H:%M}" if value else "—"
