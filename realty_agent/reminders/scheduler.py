"""Планировщик потоков напоминаний: кого и когда будить.

Время живёт **здесь**, а не в графе. Это принципиально: узел `wait_client` не спит и не
считает часы — он ставит срок `wait_until` и уходит в прерывание. Благодаря этому ветви
графа проверяются тестом без ожидания реальных часов, а «клиент молчит» становится
обычным решением по расписанию.

Две задачи планировщика:

1. **старт** — кому пора напоминать (за `N` часов до визита) и кто вообще попадает в
   группу с напоминанием (деление по менеджеру и дате слота, `realty_agent/booking/visits.py`);
2. **сроки** — у кого истёк `wait_until`, а ответа нет: графу уходит «молчание», и он сам
   решает, повторять (не больше `K` раз) или закрываться.

Повтор при молчании снова проходит через одобрение менеджера — так устроен граф, и это
же ограничитель спама: клиент не получает второе напоминание без решения человека.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager

from realty_agent.booking.visits import is_treatment
from realty_agent.reminders.pipeline import (
    AWAIT_CLIENT,
    VisitDeps,
    as_datetime,
    flow_lock,
    pending_expect,
    resume_client,
    thread_id_for,
)

log = logging.getLogger(__name__)

# Окно поиска потоков, которые ждут ответа: с запасом в прошлое (мост мог лежать) и
# на неделю вперёд (визит, о котором уже напомнили, обычно в пределах нескольких дней).
TIMEOUT_BACK_H = 24.0
TIMEOUT_AHEAD_H = 24.0 * 7


def _config(booking_id: int) -> dict:
    return {"configurable": {"thread_id": thread_id_for(booking_id)}}


async def tick(*, graph, deps: VisitDeps, settings) -> dict:
    """Один проход планировщика. Возвращает счётчики — их пишет петля в журнал.

    Проход обёрнут корневым спаном `visit.tick`: без него спаны узлов приезжали бы в платформу
    сиротами (у каждого `parent_id` пуст), и «цепочка узлов одного потока» не читалась бы вовсе.
    """
    from agent_core import spans

    if not getattr(settings, "visit_reminder_enabled", False):
        return {"started": 0, "timed_out": 0, "off": True}

    with spans.span(deps.tracer, "visit.tick", spans.workflow_attributes()):
        started = await _start_due(graph=graph, deps=deps, settings=settings)
        timed_out = await _resume_overdue(graph=graph, deps=deps, settings=settings)
    return {"started": started, "timed_out": timed_out, "off": False}


async def _start_due(*, graph, deps: VisitDeps, settings) -> int:
    """Поставить потоки тем, кому пора напомнить.

    Контрольная группа пропускается молча и **намеренно не пишется в базу**: группу
    выводят из менеджера и даты слота в момент замера, поэтому хранить её не нужно, а
    лишнее поле разошлось бы с правилом деления.
    """
    share = int(getattr(settings, "visit_reminder_treatment_share", 50))
    ahead_h = float(getattr(settings, "visit_reminder_hours_before", 20.0))
    started = 0

    for row in await deps.db.visit_candidates(ahead_h=ahead_h):
        booking_id = int(row["id"])
        slot = as_datetime(row.get("slot"))
        if slot is None:
            log.warning("граф: у брони %s неразобранное время слота — пропускаю", booking_id)
            continue
        if not is_treatment(
            manager_chat_id=row.get("confirmed_by"), slot_date=slot.date(), share=share
        ):
            continue

        config = _config(booking_id)
        snapshot = await graph.aget_state(config)
        if snapshot.values:
            continue  # поток уже идёт или уже прошёл — второй раз не начинаем
        lock = flow_lock(booking_id)
        if lock.locked():
            # Ответ человека идёт прямо сейчас: второй проход продублировал бы узлы и карточки.
            log.info("граф: поток брони %s занят ответом человека — старт пропускаю", booking_id)
            continue
        # Спан потока — ребёнок прохода: в платформе это «прогон напоминания по этой броне», и хеш
        # чата вместо идентификатора (ПДн в трейс не уходят).
        with flow_span(deps, row.get("chat_id")):
            async with lock:
                await graph.ainvoke({"booking_id": booking_id}, config)
        started += 1
        log.info("граф: поток напоминания начат для брони %s (слот %s)", booking_id, slot)

    return started


async def _resume_overdue(*, graph, deps: VisitDeps, settings) -> int:
    """Разбудить потоки, у которых истёк срок ожидания ответа клиента.

    Поток, ждущий **менеджера**, срока не имеет — его не трогаем: человек думает столько,
    сколько нужно, и будить его по часам было бы ошибкой. Проверяем это по прерыванию
    (`pending_expect`), а не по полю состояния: у потока, который уже разбудили по таймауту
    и который снова ждёт менеджера, `wait_until` в состоянии **просрочен** — по нему поток
    будился бы каждый проход, то есть каждые несколько минут и без всякой пользы.
    """
    timed_out = 0
    for row in await deps.db.visit_candidates(
        ahead_h=TIMEOUT_AHEAD_H, back_h=TIMEOUT_BACK_H, limit=50
    ):
        config = _config(int(row["id"]))
        snapshot = await graph.aget_state(config)
        if not snapshot.values or not snapshot.next:
            continue  # потока нет или он уже завершён
        if pending_expect(snapshot) != AWAIT_CLIENT:
            continue  # ждём не клиента (или ждём вообще не ответа) — срок не наш
        wait_until = as_datetime(snapshot.values.get("wait_until"))
        if wait_until is None or deps.now() < wait_until:
            continue
        lock = flow_lock(int(row["id"]))
        if lock.locked():
            log.info("граф: поток брони %s занят ответом человека — разбор срока пропускаю", row["id"])
            continue
        with flow_span(deps, row.get("chat_id")):
            async with lock:
                await graph.ainvoke(resume_timeout_command(), config)
        timed_out += 1
        log.info("граф: у брони %s истёк срок ожидания ответа клиента", row["id"])

    return timed_out


@contextmanager
def flow_span(deps: VisitDeps, chat_id):
    """Спан потока (`visit.flow`) — ребёнок прохода планировщика.

    Хеш чата вместо идентификатора: в трейсе видно «прогон напоминания по этой броне», но не то, кто
    этот человек (`realty_agent/pii.py::chat_hash`).
    """
    from agent_core import spans

    session_id = spans.session_id_for(str(chat_id or ""))
    with spans.span(deps.tracer, "visit.flow", spans.workflow_attributes(session_id=session_id)):
        yield


def resume_timeout_command():
    """Продолжение «клиент промолчал». Вынесено, чтобы планировщик не собирал его сам.

    Импорт LangGraph — внутри функции, как и в `checkpointer.py`: необязательная
    зависимость деградирует, а не валит основной путь моста.
    """
    from langgraph.types import Command

    return Command(resume=resume_client(timeout=True))


async def run_forever(*, graph, deps: VisitDeps, settings) -> None:
    """Петля планировщика. Форма та же, что у `_turn_sweep_loop` в транспорте.

    Ошибка одного прохода не убивает петлю: иначе одна недоступная база выключила бы
    напоминания до перезапуска процесса. Отмену (`CancelledError`) пропускаем наружу —
    иначе остановка моста повисла бы.
    """
    tick_s = max(30, int(getattr(settings, "visit_reminder_tick_s", 300)))
    while True:
        try:
            await asyncio.sleep(tick_s)
            counts = await tick(graph=graph, deps=deps, settings=settings)
            if counts.get("started") or counts.get("timed_out"):
                log.info(
                    "граф: проход планировщика — начато потоков %s, сроков истекло %s",
                    counts["started"], counts["timed_out"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("граф: проход планировщика упал")
