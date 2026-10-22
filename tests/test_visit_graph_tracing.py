"""Дерево спанов графа напоминаний: цепочка узлов проверяется без сети.

**Зачем отдельный файл.** Проверяется не «отправились ли спаны в платформу» (это видно только по SQL
снаружи), а **форма дерева**, которую платформа прочитает: один корень прохода, ребёнок-поток и
дети-узлы, каждый со своим типом.

**Почему это важно, а не косметика.** Без корня спаны узлов приезжают сиротами (`parent_id` пуст), и
«цепочка узлов одного потока» не читается вовсе: в интерфейсе это десять отдельных задач вместо
пайплайна. Форма атрибутов (`ag.type.span` во ВЛОЖЕННОМ словаре) живёт одним местом —
`agent_core/spans.py`.
"""
from __future__ import annotations

import pytest
from test_visit_reminders import CLIENT, MANAGER, SLOT, FakeDb, Recorder, Settings

from realty_agent.reminders import scheduler
from realty_agent.reminders.pipeline import VisitDeps, build_visit_graph

#: Дерево, которое обязано получиться при старте потока: корень прохода → поток → узлы.
EXPECTED_NODES = ("visit.schedule", "visit.draft_reminder", "visit.notify_manager")


class Boom(FakeDb):
    """База, которая падает на чтении брони: так проверяется пометка ошибки на СВОЁМ ребре."""

    async def booking_for_visit(self, *, booking_id: int):
        raise ConnectionError("база недоступна")


@pytest.fixture
def exporter_and_tracer():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider.get_tracer("test")


def _deps(db, recorder: Recorder, tracer) -> VisitDeps:
    """Зависимости графа с трейсером: в бою его даёт процесс (`graph_main.py`)."""
    async def no_slots(_booking_id: int) -> list[str]:
        return []

    return VisitDeps(
        db=db, notify_manager=recorder.notify_manager, send_client=recorder.send_client,
        free_slots=no_slots, format_slot=lambda iso: iso[:16], now=lambda: None, tracer=tracer,
    )


def _graph(deps: VisitDeps):
    from langgraph.checkpoint.memory import InMemorySaver

    return build_visit_graph(deps, checkpointer=InMemorySaver(), settings=Settings())


def by_name(spans) -> dict:
    return {span.name: span for span in spans}


@pytest.mark.asyncio
async def test_tick_opens_one_root_flow_and_the_node_chain(exporter_and_tracer):
    """Проход → поток → узлы: ровно та цепочка, которую потом ищут SQL-запросом в платформе."""
    exporter, tracer = exporter_and_tracer
    db = FakeDb()
    db.candidates = [{"id": 11, "slot": SLOT, "chat_id": CLIENT, "confirmed_by": MANAGER,
                      "address": "ул. Садовая, 12"}]
    deps = _deps(db, Recorder(), tracer)

    counts = await scheduler.tick(graph=_graph(deps), deps=deps, settings=Settings())

    assert counts["started"] == 1, "поток обязан начаться — иначе и цепочки не будет"
    spans = exporter.get_finished_spans()
    roots = [span for span in spans if span.parent is None]
    assert [span.name for span in roots] == ["visit.tick"], \
        "корень прохода ровно один — иначе иерархия в интерфейсе развалится"

    names = by_name(spans)
    flow = names["visit.flow"]
    assert flow.parent.span_id == roots[0].context.span_id, "поток — ребёнок прохода, а не сирота"
    for node in EXPECTED_NODES:
        assert names[node].parent.span_id == flow.context.span_id, f"{node}: узел вне потока"
    assert names["visit.schedule"].attributes["ag"]["type"]["span"] == "task", \
        "узлы — тип `task` (иначе «десять узлов» читались бы как десять агентов)"
    assert names["visit.flow"].attributes["ag"]["session"]["id"], \
        "у потока есть сессия — хеш чата, по которому поток и находят"


@pytest.mark.asyncio
async def test_flow_session_is_a_hash_not_a_chat_id(exporter_and_tracer):
    """В трейс уходит хеш чата, а не идентификатор человека: ПДн туда не попадают."""
    exporter, tracer = exporter_and_tracer
    db = FakeDb()
    db.candidates = [{"id": 11, "slot": SLOT, "chat_id": CLIENT, "confirmed_by": MANAGER,
                      "address": "ул. Садовая, 12"}]
    deps = _deps(db, Recorder(), tracer)

    await scheduler.tick(graph=_graph(deps), deps=deps, settings=Settings())

    session_id = by_name(exporter.get_finished_spans())["visit.flow"].attributes["ag"]["session"]["id"]
    assert CLIENT not in str(session_id), "идентификатор чата в трейс не уходит"
    assert len(str(session_id)) >= 16, "хеш, а не пустая строка: иначе поток не найти"


@pytest.mark.asyncio
async def test_suspension_is_not_marked_as_an_error(exporter_and_tracer):
    """Приостановка графа — работа, а не сбой.

    Инвариант: приостановка — не ошибка. Спан `visit.wait_client` приезжает в платформу со статусом
    ошибки и текстом `GraphInterrupt: (Interrupt(value={'expect': …`, а узел, вставший на `interrupt()`,
    останавливает прогон НАМЕРЕННО — и ошибка на нём превращает обычное ожидание ответа человека в
    тревогу, которую разбирают руками.
    """
    from opentelemetry.trace import StatusCode

    exporter, tracer = exporter_and_tracer
    db = FakeDb()
    db.candidates = [{"id": 11, "slot": SLOT, "chat_id": CLIENT, "confirmed_by": MANAGER,
                      "address": "ул. Садовая, 12"}]
    deps = _deps(db, Recorder(), tracer)

    counts = await scheduler.tick(graph=_graph(deps), deps=deps, settings=Settings())

    assert counts["started"] == 1, "поток встал на ожидание менеджера — это и есть норма"
    names = by_name(exporter.get_finished_spans())
    assert names["visit.manager_review"].status.status_code is not StatusCode.ERROR, \
        "ожидание ответа человека помечено ошибкой — в бою это ложная тревога"


@pytest.mark.asyncio
async def test_failing_node_marks_its_own_span(exporter_and_tracer):
    """Падение узла помечает ЕГО ребро: иначе сбой узла выглядел бы как сбой всего потока."""
    exporter, tracer = exporter_and_tracer
    from opentelemetry.trace import StatusCode

    db = Boom()
    db.candidates = [{"id": 11, "slot": SLOT, "chat_id": CLIENT, "confirmed_by": MANAGER,
                      "address": "ул. Садовая, 12"}]
    deps = _deps(db, Recorder(), tracer)

    with pytest.raises(ConnectionError):
        await scheduler.tick(graph=_graph(deps), deps=deps, settings=Settings())

    names = by_name(exporter.get_finished_spans())
    assert names["visit.schedule"].status.status_code == StatusCode.ERROR, \
        "упавший узел обязан быть помечен — иначе его не отличить от завершившегося"
    # Второй потребитель того же механизма обязан вести себя ОДИНАКОВО: стандарт
    # OTEL требует статус И `error.type`. Проверяем здесь, а не только у новостей: разъехавшись,
    # две копии поведения дали бы разную разметку ошибок в двух контурах.
    assert names["visit.schedule"].attributes["error.type"] == "ConnectionError", \
        "признак ошибки обязан быть и у графа напоминаний"
    # Цепочка при падении НЕ рвётся: узел остаётся ребёнком потока, поток — ребёнком прохода.
    # Без этого разбор сбоя по трейсу был бы невозможен: вместо шага была бы одна ошибка наверху.
    assert names["visit.schedule"].parent.span_id == names["visit.flow"].context.span_id, \
        "спан упавшего узла потерял связь с потоком"


@pytest.mark.asyncio
async def test_graph_without_a_tracer_works_as_before(exporter_and_tracer):
    """Без трейсера граф работает как раньше: спанов нет, поток идёт.

    Это не «мелочь»: 23 теста графа собирают его на двойниках без трейсера, и любая проверка
    «включена ли трассировка» сломала бы их — здесь зафиксировано, что такой проверки нет.
    """
    exporter, _tracer = exporter_and_tracer
    db = FakeDb()
    db.candidates = [{"id": 11, "slot": SLOT, "chat_id": CLIENT, "confirmed_by": MANAGER,
                      "address": "ул. Садовая, 12"}]
    deps = _deps(db, Recorder(), None)

    counts = await scheduler.tick(graph=_graph(deps), deps=deps, settings=Settings())

    assert counts["started"] == 1
    assert exporter.get_finished_spans() == (), "без трейсера спанов быть не может"
