"""Граф напоминания о подтверждённом визите: узлы, циклы, маршрутизация.

Полное обоснование «почему LangGraph» — в `realty_agent/graph/__init__.py`. Кратко: состояние
живёт днями между ходами, есть прерывание на человека и есть циклы, которые надо
ограничивать. Ни один узел не вызывается с пути диалога.

**Зависимости внедряются (`VisitDeps`).** Граф не знает ни про Telegram, ни про кнопки,
ни про формат времени — он получает их функциями. Иначе ветви графа нельзя было бы
проверить без живого бота, а проверка ветвей — половина ценности пункта.

**Ответ клиента и решение менеджера приходят прерыванием** (`interrupt`), а не поллингом:
кнопка и короткая фраза находят нужный поток по `thread_id` (`thread_id_for`), и графу
достаточно продолжить с места. Какое прерывание сейчас активно, видно по полю
`awaiting` — по нему мост строит продолжение и не может перепутать адресата.

**Время в графе не течёт.** Узел `wait_client` не спит: срок ожидания ставится при
отправке (`wait_until`), а решение «клиент молчит» принимает планировщик по часам
(`realty_agent/graph/scheduler.py`). Поэтому ветви проверяются без ожидания реальных часов.

**«Переписать» — это текст менеджера, а не пометка к тексту.** Модель в этом пункте не
участвует (черновик — шаблон), поэтому «учесть замечание» нечем: менеджер присылает
готовую формулировку, и клиенту уходит именно она. Служебные пометки в текст клиенту
не попадают — это отдельно проверяется тестом.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from realty_agent.booking.visits import VISIT_REMINDER_TEXT

log = logging.getLogger(__name__)

# Кто ждёт ответа. Нужно и графу, и мосту: по этому полю строится продолжение, поэтому
# ответ клиента не может «одобрить» текст за менеджера.
AWAIT_MANAGER = "manager"
AWAIT_CLIENT = "client"

# Исходы потока. `closed` — клиент ответил, что придёт; `stopped` — поток прекращён с
# причиной в `blocked`; `moved` — перенос не удалось предложить (нет свободных слотов).
OUTCOME_CLOSED = "closed"
OUTCOME_STOPPED = "stopped"


class VisitState(TypedDict, total=False):
    """Состояние потока: всё, что нужно, чтобы продолжить после перезапуска процесса."""

    booking_id: int
    chat_id: str
    address: str
    slot_iso: str
    manager_chat_id: str
    draft: str                 # текст, который уйдёт клиенту после одобрения
    proposal: list[str]        # новые слоты, предложенные при переносе
    sent_count: int            # сколько раз напоминание реально ушло клиенту
    repeats: int               # сколько повторов вызвано молчанием (ограничено K)
    answer: str                # yes | move | "" — ответ клиента
    awaiting: str              # manager | client
    wait_until: str            # до какого момента ждём ответа клиента (ISO, UTC)
    outcome: str               # closed | stopped
    blocked: str               # причина остановки (человекочитаемая, для журнала)


@dataclass(frozen=True)
class VisitDeps:
    """Всё внешнее, что нужно графу. Ни одного импорта Telegram — иначе не проверить."""

    db: Any
    notify_manager: Callable[[int, str], Awaitable[bool]]
    send_client: Callable[[int, str, str], Awaitable[bool]]
    free_slots: Callable[[int], Awaitable[list[str]]]
    format_slot: Callable[[str], str]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Трейсер процесса: узлы открывают по спану каждый. `None` — спанов нет, и это
    #: рабочий режим: тесты графа собирают его на двойниках и трейсер им не нужен.
    tracer: Any = None


def thread_id_for(booking_id: int) -> str:
    """Идентификатор потока = бронь. Один визит — один поток, и это видно из номера."""
    return f"visit:{booking_id}"


#: Замки на поток брони. Один `thread_id` будят ДВА входа — тик планировщика (раз в 300 с) и ответ
#: человека (HTTP-продолжение). LangGraph сериализует только курсор чекпойнтера, а не исполнение:
#: то есть два `ainvoke` на один поток — это два прохода узлов и **две** карточки менеджеру/клиенту.
#:
#: ❗ Замок ЖИВЁТ В ПРОЦЕССЕ, и этого достаточно: оба входа — в процессе напоминаний. Замок в базе
#: (`pg_try_advisory_xact_lock`) здесь не работает: пул чекпойнтера создан с `autocommit=True`, и
#: транзакционный лок снимается на самом `SELECT`. Кросс-процессный случай (юнит + контейнер на одной
#: базе) назван нерешённым в ADR `0036` — сериализовать его этим замком нельзя, и делать вид не будем.
_FLOW_LOCKS: dict[int, asyncio.Lock] = {}


def flow_lock(booking_id: int) -> asyncio.Lock:
    """Замок потока этой брони. Берётся БЕЗ ожидания: занято — второй вход говорит «занято»."""
    key = int(booking_id)
    lock = _FLOW_LOCKS.get(key)
    if lock is None:
        lock = _FLOW_LOCKS[key] = asyncio.Lock()
    return lock


def resume_manager(*, decision: str, text: str = "") -> dict:
    """Продолжение от менеджера: `approve` либо `revise` с готовым текстом."""
    return {"kind": AWAIT_MANAGER, "decision": decision, "text": text}


def resume_client(*, answer: str | None = None, timeout: bool = False) -> dict:
    """Продолжение от клиента: `yes` / `move` либо молчание (`timeout`)."""
    return {"kind": AWAIT_CLIENT, "answer": answer, "timeout": timeout}


def build_visit_graph(deps: VisitDeps, *, checkpointer, settings):
    """Собрать граф напоминаний. `settings` — параметры N, M, K и доля групп замера.

    Граф собирается функцией, а не модульным синглтоном: в тестах нужен экземпляр поверх двойников
    (как у MCP-сервера), общее состояние этому только мешало бы.
    """
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    repeat_h = float(getattr(settings, "visit_reminder_repeat_h", 4.0))
    max_repeats = int(getattr(settings, "visit_reminder_max_repeats", 2))

    async def schedule(state: VisitState) -> VisitState:
        """Начало потока: берём данные брони и проверяем, что она ещё подтверждена.

        Проверка не формальность: между постановкой задачи планировщиком и стартом графа
        бронь могла быть отклонена, а напоминать о несуществующем визите нельзя.
        """
        row = await deps.db.booking_for_visit(booking_id=int(state["booking_id"]))
        if not row or row.get("status") != "confirmed":
            return {"outcome": OUTCOME_STOPPED, "blocked": "бронь не подтверждена"}
        # Без подтвердившего менеджера поток бессмысленен: карточку некому показать, а группа
        # замера у такой брони — контрольная (`realty_agent/booking/visits.py:is_treatment`). Планировщик
        # её и не ставит, но если поток всё-таки начат — он обязан остановиться с причиной,
        # а не слать карточку в пустоту.
        if not str(row.get("confirmed_by") or ""):
            return {"outcome": OUTCOME_STOPPED, "blocked": "нет подтвердившего менеджера"}
        return {
            "chat_id": str(row.get("chat_id") or ""),
            "address": str(row.get("address") or ""),
            "slot_iso": str(row.get("slot") or ""),
            "manager_chat_id": str(row.get("confirmed_by") or ""),
            "sent_count": 0,
            "repeats": 0,
            "answer": "",
        }

    async def draft_reminder(state: VisitState) -> VisitState:
        """Черновик — по шаблону, без вызова модели (`realty_agent/booking/visits.py`).

        При переносе в текст добавляются предложенные слоты: менеджер одобряет конкретное
        предложение, а не «напоминание вообще».
        """
        text = VISIT_REMINDER_TEXT.format(
            slot=deps.format_slot(state["slot_iso"]), address=state["address"])
        proposal = state.get("proposal") or []
        if proposal:
            options = "; ".join(deps.format_slot(slot) for slot in proposal)
            text = f"{text}\n\nВозможное время: {options}"
        return {"draft": text}

    async def notify_manager(state: VisitState) -> VisitState:
        """Показать черновик менеджеру с кнопками.

        Отдельный узел, а не строка перед `interrupt()`: **побочный эффект перед
        прерыванием выполнился бы дважды** — узел перезапускается при продолжении.
        Здесь отправка идёт один раз, а узел прерывания остаётся чистым.
        """
        if not await deps.notify_manager(int(state["booking_id"]), state["draft"]):
            return {"outcome": OUTCOME_STOPPED, "blocked": "карточка менеджеру не ушла"}
        return {}

    async def manager_review(state: VisitState) -> VisitState:
        """Прерывание на менеджера: одобрить черновик или прислать свой текст."""
        payload = interrupt({"expect": AWAIT_MANAGER, "draft": state["draft"]})
        if not _is_kind(payload, AWAIT_MANAGER):
            # Чужое продолжение (например, ответ клиента) не должно одобрять текст:
            # возвращаемся на повторный вход и снова встаём в прерывание.
            log.warning("граф: в узел менеджера пришло чужое продолжение: %r", payload)
            return {"awaiting": AWAIT_MANAGER}
        if payload.get("decision") == "approve":
            return {"awaiting": "", "draft": state["draft"]}
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"awaiting": AWAIT_MANAGER}   # «переписать» без текста — ждём текст
        return {"awaiting": "", "draft": text}

    async def send(state: VisitState) -> VisitState:
        """Отправить клиенту — но только если визит ещё в силе.

        Проверка перед каждой отправкой: клиент не должен получить напоминание о визите,
        который уже отменён или перенесён.
        """
        row = await deps.db.booking_for_visit(booking_id=int(state["booking_id"]))
        if not row or row.get("status") != "confirmed":
            return {"outcome": OUTCOME_STOPPED, "blocked": "бронь перестала быть подтверждённой"}
        # Визит мог пройти, пока поток ждал ответа (например, мост лежал сутки). Напоминать
        # о прошедшем визите нельзя, и «догоняющая» отправка тут была бы худшим исходом.
        # Нечитаемое время — тоже отказ: отправлять по неразобранному сроку нельзя.
        slot = as_datetime(row.get("slot"))
        if slot is None:
            return {"outcome": OUTCOME_STOPPED, "blocked": "не удалось прочитать время визита"}
        if slot <= deps.now():
            return {"outcome": OUTCOME_STOPPED, "blocked": "визит уже прошёл"}
        if not await deps.send_client(
            int(state["booking_id"]), state["draft"], str(row.get("chat_id") or "")):
            return {"outcome": OUTCOME_STOPPED, "blocked": "сообщение клиенту не ушло"}
        # Срок ожидания ответа ставится здесь — в одном месте, сразу после отправки.
        wait_until = (deps.now() + timedelta(hours=repeat_h)).isoformat()
        return {"sent_count": int(state.get("sent_count", 0)) + 1, "wait_until": wait_until}

    async def wait_client(state: VisitState) -> VisitState:
        """Прерывание на клиента. Молчание обрабатывает планировщик, а не этот узел."""
        payload = interrupt({
            "expect": AWAIT_CLIENT,
            "draft": state["draft"],
            "wait_until": state.get("wait_until", ""),
        })
        if not _is_kind(payload, AWAIT_CLIENT):
            log.warning("граф: в узел клиента пришло чужое продолжение: %r", payload)
            return {"awaiting": AWAIT_CLIENT}
        answer = payload.get("answer")
        if payload.get("timeout"):
            return {"awaiting": "", "answer": ""}
        if answer not in ("yes", "move"):
            return {"awaiting": AWAIT_CLIENT}
        return {"awaiting": "", "answer": str(answer)}

    async def close_client(state: VisitState) -> VisitState:
        """Клиент подтвердил, что придёт."""
        return {"outcome": OUTCOME_CLOSED}

    async def reschedule(state: VisitState) -> VisitState:
        """Перенос: предложить новые слоты тем же кодом, что у бота и MCP-инструмента."""
        slots = await deps.free_slots(int(state["booking_id"]))
        if not slots:
            return {"outcome": OUTCOME_STOPPED, "blocked": "нет свободных слотов для переноса"}
        return {"proposal": slots[:3]}

    async def repeat_or_stop(state: VisitState) -> VisitState:
        """Молчание клиента: повторяем не больше K раз, иначе закрываем поток.

        Ограничение обязательно: без него цикл превращается в спам клиенту.
        """
        repeats = int(state.get("repeats", 0))
        if repeats >= max_repeats:
            return {"outcome": OUTCOME_STOPPED, "blocked": f"клиент молчит после {repeats} повторов"}
        return {"repeats": repeats + 1}

    async def stopped(state: VisitState) -> VisitState:
        return {}

    def after_schedule(state: VisitState) -> str:
        return "stopped" if state.get("blocked") else "draft_reminder"

    def after_notify(state: VisitState) -> str:
        return "stopped" if state.get("blocked") else "manager_review"

    def after_manager(state: VisitState) -> str:
        return "manager_review" if state.get("awaiting") == AWAIT_MANAGER else "send"

    def after_send(state: VisitState) -> str:
        return "stopped" if state.get("blocked") else "wait_client"

    def after_wait(state: VisitState) -> str:
        if state.get("awaiting") == AWAIT_CLIENT:
            return "wait_client"          # чужое продолжение — ждём снова
        if state.get("answer") == "yes":
            return "close_client"
        if state.get("answer") == "move":
            return "reschedule"
        return "repeat_or_stop"

    def after_repeat(state: VisitState) -> str:
        return "stopped" if state.get("outcome") == OUTCOME_STOPPED else "draft_reminder"

    def after_reschedule(state: VisitState) -> str:
        return "stopped" if state.get("blocked") else "draft_reminder"

    graph = StateGraph(VisitState)

    def traced(name: str, node):
        """Обернуть узел спаном `visit.<узел>`.

        Почему обёртка, а не `with span(...)` внутри каждого узла: узлов десять, и в теле каждого
        пришлось бы повторять одну и ту же строку — а разъехавшись, спаны перестали бы называться
        одинаково, и «цепочку узлов» в SQL читать было бы нечем. Здесь она одна на всех.

        Падение помечается на СВОЁМ ребре и поднимается дальше: сбой узла не должен выглядеть как сбой
        потока, но и глотать исключение нельзя — на него опирается `after_*`-развилка.

        **Приостановка (`GraphInterrupt`) ошибкой НЕ помечается**: узел, вставший на прерывание, —
        нормальный ход графа, а не сбой. Автоматическая пометка OTel записала бы `visit.wait_client`
        ошибкой (её `status_message` выглядела бы как `GraphInterrupt: (Interrupt(value={'expect': …`).
        Поэтому у узлов статус решаем мы (`record_exceptions=False`), а не библиотека.
        """
        from langgraph.errors import GraphInterrupt

        from agent_core import spans

        async def wrapper(state: VisitState) -> VisitState:
            with spans.span(deps.tracer, f"visit.{name}", spans.node_attributes(name=name),
                            record_exceptions=False) as current:
                try:
                    return await node(state)
                except GraphInterrupt:
                    raise  # поток встал ждать человека или клиента — это работа, а не сбой
                except Exception as error:  # пометить своё ребро и пропустить дальше
                    spans.mark_error(current, f"{type(error).__name__}: {error}",
                                     error_type=type(error).__name__)
                    raise

        return wrapper

    graph.add_node("schedule", traced("schedule", schedule))
    graph.add_node("draft_reminder", traced("draft_reminder", draft_reminder))
    graph.add_node("notify_manager", traced("notify_manager", notify_manager))
    graph.add_node("manager_review", traced("manager_review", manager_review))
    graph.add_node("send", traced("send", send))
    graph.add_node("wait_client", traced("wait_client", wait_client))
    graph.add_node("close_client", traced("close_client", close_client))
    graph.add_node("reschedule", traced("reschedule", reschedule))
    graph.add_node("repeat_or_stop", traced("repeat_or_stop", repeat_or_stop))
    graph.add_node("stopped", traced("stopped", stopped))

    graph.add_edge(START, "schedule")
    graph.add_conditional_edges("schedule", after_schedule, ["draft_reminder", "stopped"])
    graph.add_edge("draft_reminder", "notify_manager")
    graph.add_conditional_edges("notify_manager", after_notify, ["manager_review", "stopped"])
    graph.add_conditional_edges("manager_review", after_manager, ["manager_review", "send"])
    graph.add_conditional_edges("send", after_send, ["wait_client", "stopped"])
    graph.add_conditional_edges(
        "wait_client", after_wait,
        ["wait_client", "close_client", "reschedule", "repeat_or_stop"])
    graph.add_conditional_edges("repeat_or_stop", after_repeat, ["draft_reminder", "stopped"])
    graph.add_conditional_edges("reschedule", after_reschedule, ["draft_reminder", "stopped"])
    graph.add_edge("close_client", END)
    graph.add_edge("stopped", END)

    return graph.compile(checkpointer=checkpointer)


def _is_kind(payload: Any, kind: str) -> bool:
    """Продолжение адресовано этому узлу? Иначе узел его не применяет."""
    return isinstance(payload, dict) and payload.get("kind") == kind


def pending_expect(snapshot: Any) -> str | None:
    """Кого ждёт поток: `manager`, `client` или `None` (не ждёт либо завершён).

    **Читается из прерывания, а не из состояния, и это не придирка.** Узел, вызвавший
    `interrupt()`, не успевает записать `awaiting`: запись узла применяется при его
    возврате, а возврата нет — поток стоит на прерывании. Поэтому поле `awaiting` в
    состоянии означает только «предыдущее продолжение оказалось чужим», а надёжный
    источник — само прерывание.

    Цена ошибки здесь высокая, а не косметическая: по этому признаку мост решает, какое
    продолжение строить (ответ клиента не должен одобрять текст), а планировщик — кого
    будить по часам (поток, ждущий менеджера, ждёт человека, а не таймаут).
    """
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, dict) and value.get("expect"):
                return str(value["expect"])
    return None


def as_datetime(value: Any) -> datetime | None:
    """Разобрать срок к одному типу: из состояния он строка, из базы — `datetime`.

    `None` означает «прочитать не удалось», и вызывающий обязан трактовать это как отказ,
    а не как «время в порядке»: отправка по неразобранному сроку — хуже, чем пропуск.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
