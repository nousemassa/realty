"""Зависимости графа напоминаний для ОТДЕЛЬНОГО процесса.

**Что здесь и почему отдельным модулем.** Граф получает всё внешнее через `VisitDeps` (это его
собственный контракт, `realty_agent/graph/reminders.py`). Раньше замыкания строил мост (`bot.py::_visit_deps`) и
отправлял сообщения сам — потому что владеет сессией Telegram. Теперь граф живёт своим процессом, а
сессия остаётся у моста (правило «одна сессия — один процесс»), поэтому доставка меняется на **очередь**
: граф кладёт строку, а отправляет мост.

**Почему это не потеря качества.** Ярлыки и действия кнопок перенесены из прежних
`_visit_notify_manager`/`_visit_send_client` дословно (`va`/`vr` менеджеру, `vy`/`vm` клиенту), а
`callback_data` собирает тот же построитель подписи в мосте — значит кнопка, поставленная графом,
работает ровно как раньше (в это доказано сравнением подписи, а не на слово).

**Смена смысла возврата, названная вслух.** Прежние функции возвращали «доставлено», теперь — «принято
к доставке». Для графа разница только в одном месте: ветка «карточка не ушла ⇒ остановить поток»
(`notify_manager → stopped`) срабатывает теперь тогда, когда не удалось даже поставить в очередь. Это
честный компромисс выноса: гарантию доставки даёт очередь, а не момент вызова.
"""
from __future__ import annotations

import datetime as dt
import logging

from realty_agent.booking.slots import free_slots
from realty_agent.reminders.pipeline import VisitDeps
from realty_agent.tg.limits import VISIT_DRAFT_TITLE, format_slot_iso

log = logging.getLogger(__name__)

#: Ярлыки кнопок — перенесены из моста дословно (менеджер и клиент видят ТО ЖЕ, что видели).
MANAGER_ACTIONS = (("va", "✅ Отправить клиенту"), ("vr", "✏️ Переписать"))
CLIENT_ACTIONS = (("vy", "✅ Приду"), ("vm", "🗓 Перенести"))


def make_visit_deps(*, db, settings, tracer=None) -> VisitDeps:
    """Собрать зависимости графа для процесса напоминаний.

    `notify_manager` и `send_client` кладут исходящее в очередь; `free_slots`/`format_slot`/`now`
    остаются прежними — они не касаются транспорта и работают с базой напрямую. `tracer` — трейсер
    процесса: узлы графа открывают по спану каждый.

    **Время в тексте — `format_slot_iso`, а не `format_slot`.**
    В состоянии графа слот лежит СТРОКОЙ (`slot_iso`), а `format_slot` понимает только `datetime`:
    на строке он возвращает её же. Клиент получал «Напоминаем о просмотре:
    2026-10-15 07:13:35.013830+00:00» — машинную метку вместо «15.10 07:13». `format_slot_iso`
    существует ровно для этого входа (`realty_agent/tg/limits.py`).
    """

    async def notify_manager(booking_id: int, text: str) -> bool:
        booking = await db.booking_for_visit(booking_id=booking_id)
        manager_chat = str((booking or {}).get("confirmed_by") or "")
        if not manager_chat:
            log.warning("граф: у брони %s нет подтвердившего менеджера — карточка не ставится", booking_id)
            return False
        return await _enqueue(
            db, chat_id=manager_chat, booking_id=booking_id,
            text=f"{VISIT_DRAFT_TITLE}\n\n{text}", buttons=MANAGER_ACTIONS,
        )

    async def send_client(booking_id: int, text: str, chat_id: str) -> bool:
        if not chat_id:
            return False
        return await _enqueue(
            db, chat_id=chat_id, booking_id=booking_id, text=text, buttons=CLIENT_ACTIONS,
        )

    async def slots_for(booking_id: int) -> list[str]:
        booking = await db.booking_for_visit(booking_id=booking_id)
        if not booking:
            return []
        return await free_slots(db, tz=settings.tz, listing_id=booking.get("listing_id"))

    return VisitDeps(
        db=db,
        notify_manager=notify_manager,
        send_client=send_client,
        free_slots=slots_for,
        format_slot=lambda iso: format_slot_iso(iso, settings.tz),
        now=lambda: dt.datetime.now(settings.tz),
        tracer=tracer,
    )


async def _enqueue(db, *, chat_id: str, booking_id: int, text: str,
                   buttons: tuple[tuple[str, str], ...]) -> bool:
    """Поставить исходящее графа в очередь. `True` — принято (а не «доставлено»).

    Кнопки идут ДЕСКРИПТОРАМИ: готовые объекты Telethon в другой процесс не передать, а подпись мост
    соберёт тем же построителем, что и для своих карточек.
    """
    try:
        await db.enqueue_outbox(kind="visit_reminder", payload={
            "chat_id": str(chat_id),
            "text": text,
            "booking_id": int(booking_id),
            "buttons": [{"label": label, "action": action} for action, label in buttons],
        })
    except Exception as error:  # noqa: BLE001 — недоступная очередь не должна ронять граф
        log.warning("граф: исходящее по броне %s не поставлено в очередь (%s: %s)",
                    booking_id, type(error).__name__, error)
        return False
    return True
