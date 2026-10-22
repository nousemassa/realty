"""Тесты напоминаний о подтверждённом визите.

**Почему ветви проверяются на двойниках, а возобновление — на настоящей базе.** Ветви
графа (одобрение, правка, молчание, перенос) не зависят ни от Telegram, ни от часов, и
проверять их через живого бота значило бы проверять бота. А смысл пункта — в том, что
состояние **переживает смерть процесса**, поэтому возобновление проверяется только на
настоящем чекпойнтере в PostgreSQL: восстановление «в памяти объекта» не проверяет ничего.

Тесты с базой пропускаются, если база не поднята (`pytest.skip`) — как в `test_turn_state.py`:
в CI нет ни векторной базы, ни моста, и падать из-за этого они не должны.
"""
from __future__ import annotations

import datetime as dt
import logging

import pytest

from realty_agent.booking.visits import (
    VISIT_RESULTS,
    classify_answer,
    is_treatment,
    normalize_answer,
    reminder_group,
)
from realty_agent.reminders import scheduler
from realty_agent.reminders.pipeline import (
    AWAIT_MANAGER,
    VisitDeps,
    build_visit_graph,
    pending_expect,
    resume_client,
    resume_manager,
    thread_id_for,
)

log = logging.getLogger(__name__)

NOW = dt.datetime(2026, 10, 20, 8, 0, tzinfo=dt.UTC)
SLOT = "2026-10-20T18:00:00+03:00"      # через 7 часов от NOW
PAST_SLOT = "2020-01-01T10:00:00+03:00"
MANAGER = "manager-1"
CLIENT = "client-1"


class Settings:
    """Настройки механизма: те же имена, что в `config.py`, значения — удобные для теста."""

    visit_reminder_enabled = True
    visit_reminder_hours_before = 20.0
    visit_reminder_repeat_h = 4.0
    visit_reminder_max_repeats = 2
    visit_reminder_tick_s = 300
    visit_reminder_treatment_share = 100


class FakeDb:
    """Двойник базы: ровно те методы, которые граф и планировщик действительно зовут."""

    def __init__(self, *, status: str = "confirmed", slot: str = SLOT, manager: str | None = MANAGER) -> None:
        self.status = status
        self.slot = slot
        self.manager = manager
        self.candidates: list[dict] = []
        self.marked: list[tuple[int, str]] = []

    async def booking_for_visit(self, *, booking_id: int) -> dict | None:
        return {
            "id": booking_id, "status": self.status, "slot": self.slot, "chat_id": CLIENT,
            "address": "ул. Садовая, 12", "confirmed_by": self.manager, "listing_id": "L-1",
            "visit_result": None,
        }

    async def visit_candidates(self, *, ahead_h: float, back_h: float = 0.0, limit: int = 50):
        return self.candidates[:limit]

    async def set_visit_result(self, *, booking_id: int, result: str, manager_chat_id: str, role: str = "manager"):
        assert result in VISIT_RESULTS
        self.marked.append((booking_id, result))
        return {"id": booking_id, "status": "confirmed", "visit_result": result}


class Recorder:
    """Что реально ушло наружу: без этого «текст одобрен» ничем не подтверждено."""

    def __init__(self) -> None:
        self.manager_cards: list[tuple[int, str]] = []
        self.client_messages: list[tuple[int, str]] = []

    async def notify_manager(self, booking_id: int, text: str) -> bool:
        self.manager_cards.append((booking_id, text))
        return True

    async def send_client(self, booking_id: int, text: str, chat_id: str) -> bool:
        self.client_messages.append((booking_id, text))
        return True


def resume_manager_value(decision: str, text: str = ""):
    """Продолжение от менеджера в виде `Command` — то же, что шлёт бот по кнопке."""
    from langgraph.types import Command

    return Command(resume=resume_manager(decision=decision, text=text))


def resume_client_value(answer: str | None, timeout: bool = False):
    """Продолжение от клиента в виде `Command`."""
    from langgraph.types import Command

    return Command(resume=resume_client(answer=answer, timeout=timeout))


def config(booking_id: int) -> dict:
    return {"configurable": {"thread_id": thread_id_for(booking_id)}}


async def _slots(_booking_id: int) -> list[str]:
    return ["2026-10-21T10:00:00+03:00", "2026-10-21T11:00:00+03:00", "2026-10-21T12:00:00+03:00"]


def _deps(db: FakeDb, recorder: Recorder, *, now=NOW) -> VisitDeps:
    return VisitDeps(
        db=db, notify_manager=recorder.notify_manager, send_client=recorder.send_client,
        free_slots=_slots, format_slot=lambda iso: iso[:16], now=lambda: now,
    )


def make_graph(*, db: FakeDb | None = None, recorder: Recorder | None = None, now=NOW,
               settings=None, checkpointer=None):
    """Граф поверх двойников. Без чекпойнтера — `InMemorySaver`: ни сети, ни часов."""
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
    recorder = recorder or Recorder()
    db = db or FakeDb()
    graph = build_visit_graph(_deps(db, recorder, now=now), checkpointer=checkpointer,
                              settings=settings or Settings())
    return graph, db, recorder


# --- чистые функции: словарь и деление на группы --------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Да", "yes"), ("  ДА!  ", "yes"), ("приду", "yes"), ("Ок", "yes"),
        ("Перенесите", "move"), ("перенесём", "move"), ("не смогу", "move"),
        ("расскажите про ипотеку", None), ("", None), ("давайте обсудим да", None),
    ],
)
def test_answer_phrases_are_a_closed_exact_list(text, expected):
    """Список фраз закрытый: разбор произвольных формулировок — это второй FSM."""
    assert classify_answer(text) is expected


def test_answer_normalization_keeps_them_apart():
    """Нормализация убирает различия написания, но не смешивает «да» и «не смогу»."""
    assert normalize_answer("Перенесём!") == normalize_answer("перенесем")
    assert normalize_answer("да.") == "да"
    assert classify_answer("да нет наверное") is None


def test_split_depends_on_manager_and_day_not_on_client():
    """Группа выводится из менеджера и даты слота — иначе замер мерил бы привычки клиентов."""
    day = dt.date(2026, 10, 20)
    assert reminder_group(manager_chat_id=MANAGER, slot_date=day, share=100) == "treatment"
    assert reminder_group(manager_chat_id=MANAGER, slot_date=day, share=0) == "control"
    assert is_treatment(manager_chat_id=None, slot_date=day, share=100) is False
    assert len({reminder_group(manager_chat_id=MANAGER, slot_date=day, share=50) for _ in range(20)}) == 1


# --- ветви графа ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_does_not_get_text_before_manager_approves():
    """Текст уходит клиенту только после одобрения — это требование спецификации пункта."""
    graph, _, recorder = make_graph()

    await graph.ainvoke({"booking_id": 1}, config(1))
    assert len(recorder.manager_cards) == 1, "менеджер должен увидеть черновик"
    assert recorder.client_messages == [], "клиенту до одобрения отправлять нечего"

    await graph.ainvoke(resume_manager_value("approve"), config(1))
    assert len(recorder.client_messages) == 1


@pytest.mark.asyncio
async def test_rewrite_sends_managers_own_text():
    """«Переписать» — это текст менеджера; служебные пометки клиенту не попадают."""
    graph, _, recorder = make_graph()
    await graph.ainvoke({"booking_id": 2}, config(2))

    await graph.ainvoke(
        resume_manager_value("revise", text="Добрый день! Ждём вас завтра в 18:00."), config(2))
    await graph.ainvoke(resume_manager_value("approve"), config(2))

    sent = recorder.client_messages[-1][1]
    assert sent == "Добрый день! Ждём вас завтра в 18:00."
    assert "правка" not in sent


@pytest.mark.asyncio
async def test_empty_rewrite_keeps_waiting():
    """«Переписать» без текста не отправляет клиенту шаблон: ждём текст, а не одобряем молча."""
    graph, _, recorder = make_graph()
    await graph.ainvoke({"booking_id": 21}, config(21))

    result = await graph.ainvoke(resume_manager_value("revise", text="   "), config(21))
    assert recorder.client_messages == []
    assert any(item.value.get("expect") == AWAIT_MANAGER for item in result.get("__interrupt__", []))


@pytest.mark.asyncio
async def test_yes_closes_the_flow():
    graph, _, recorder = make_graph()
    await graph.ainvoke({"booking_id": 3}, config(3))
    await graph.ainvoke(resume_manager_value("approve"), config(3))

    await graph.ainvoke(resume_client_value("yes"), config(3))
    state = await graph.aget_state(config(3))
    assert state.values.get("outcome") == "closed"
    assert state.next == (), "после ответа клиента поток завершён"
    assert len(recorder.client_messages) == 1


@pytest.mark.asyncio
async def test_move_proposes_slots_and_returns_to_manager():
    """«Перенести» → новые слоты → снова одобрение менеджера (ветвь переноса)."""
    graph, _, recorder = make_graph()
    await graph.ainvoke({"booking_id": 4}, config(4))
    await graph.ainvoke(resume_manager_value("approve"), config(4))

    result = await graph.ainvoke(resume_client_value("move"), config(4))
    state = await graph.aget_state(config(4))
    assert state.values.get("proposal"), "перенос обязан предложить новые слоты"
    assert any(item.value.get("expect") == AWAIT_MANAGER for item in result.get("__interrupt__", []))
    assert len(recorder.manager_cards) == 2, "новое предложение снова показывают менеджеру"


@pytest.mark.asyncio
async def test_silence_repeats_at_most_k_times():
    """Молчание даёт не больше K повторов, и каждый повтор одобряет человек."""
    graph, _, recorder = make_graph()
    await graph.ainvoke({"booking_id": 5}, config(5))

    for _ in range(6):                      # больше, чем K+1: цикл обязан остановиться сам
        await graph.ainvoke(resume_manager_value("approve"), config(5))
        await graph.ainvoke(resume_client_value(None, timeout=True), config(5))

    state = await graph.aget_state(config(5))
    assert state.next == ()
    assert state.values.get("outcome") == "stopped"
    assert "молчит" in (state.values.get("blocked") or "")
    assert len(recorder.client_messages) == Settings.visit_reminder_max_repeats + 1


@pytest.mark.asyncio
async def test_foreign_answer_does_not_approve_the_text():
    """Ответ клиента, пришедший к узлу менеджера, не должен одобрять черновик."""
    graph, _, recorder = make_graph()
    await graph.ainvoke({"booking_id": 6}, config(6))

    result = await graph.ainvoke(resume_client_value("yes"), config(6))
    assert recorder.client_messages == []
    assert any(item.value.get("expect") == AWAIT_MANAGER for item in result.get("__interrupt__", []))


@pytest.mark.asyncio
async def test_cancelled_booking_stops_before_sending():
    """Перед отправкой проверяется бронь: по отменённой визит не напоминают."""
    db = FakeDb()
    graph, _, recorder = make_graph(db=db)
    await graph.ainvoke({"booking_id": 7}, config(7))

    db.status = "rejected"                 # бронь отменили, пока менеджер думал над текстом
    await graph.ainvoke(resume_manager_value("approve"), config(7))

    state = await graph.aget_state(config(7))
    assert recorder.client_messages == []
    assert state.values.get("outcome") == "stopped"
    assert "перестала" in (state.values.get("blocked") or "")


@pytest.mark.asyncio
async def test_past_visit_is_not_reminded():
    """Визит, который уже прошёл (мост лежал), напоминанием не догоняется."""
    graph, _, recorder = make_graph(db=FakeDb(slot=PAST_SLOT))
    await graph.ainvoke({"booking_id": 8}, config(8))
    await graph.ainvoke(resume_manager_value("approve"), config(8))

    state = await graph.aget_state(config(8))
    assert state.values.get("outcome") == "stopped"
    assert "прошёл" in (state.values.get("blocked") or "")
    assert recorder.client_messages == []


@pytest.mark.asyncio
async def test_booking_without_manager_stops_without_card():
    """Бронь без подтвердившего менеджера: карточка не уходит, поток останавливается честно."""
    graph, _, recorder = make_graph(db=FakeDb(manager=None))
    await graph.ainvoke({"booking_id": 9}, config(9))

    state = await graph.aget_state(config(9))
    assert state.values.get("outcome") == "stopped"
    assert recorder.manager_cards == []


@pytest.mark.asyncio
async def test_attendance_marking_accepts_each_result():
    """Отметка явки принимает обе отметки, а неизвестную — отвергает (это уже не данные)."""
    db = FakeDb()
    await db.set_visit_result(booking_id=10, result="attended", manager_chat_id=MANAGER)
    await db.set_visit_result(booking_id=10, result="no_show", manager_chat_id=MANAGER)
    assert db.marked == [(10, "attended"), (10, "no_show")]
    with pytest.raises(AssertionError):
        await db.set_visit_result(booking_id=10, result="был-не-был", manager_chat_id=MANAGER)


# --- планировщик ----------------------------------------------------------------

def _candidate(booking_id: int) -> dict:
    return {"id": booking_id, "slot": SLOT, "chat_id": CLIENT, "confirmed_by": MANAGER,
            "address": "ул. Садовая, 12"}


@pytest.mark.asyncio
async def test_scheduler_starts_once_and_respects_switch():
    """Поток ставится один раз; при выключенном механизме не ставится вовсе."""
    db = FakeDb()
    db.candidates = [_candidate(11)]
    graph, _, _ = make_graph(db=db)

    assert (await scheduler.tick(graph=graph, deps=_deps(db, Recorder()), settings=Settings()))["started"] == 1
    # Второй проход видит существующий поток и не начинает второй: иначе клиент получил бы
    # два напоминания об одном визите.
    assert (await scheduler.tick(graph=graph, deps=_deps(db, Recorder()), settings=Settings()))["started"] == 0

    class Off(Settings):
        visit_reminder_enabled = False

    counts = await scheduler.tick(graph=graph, deps=_deps(db, Recorder()), settings=Off())
    assert counts["off"] is True


@pytest.mark.asyncio
async def test_scheduler_skips_control_group():
    """Контрольной группе напоминание не уходит — иначе замер бессмыслен."""
    db = FakeDb()
    db.candidates = [_candidate(12)]

    class NoTreatment(Settings):
        visit_reminder_treatment_share = 0

    recorder = Recorder()
    graph, _, _ = make_graph(db=db, recorder=recorder, settings=NoTreatment())
    counts = await scheduler.tick(graph=graph, deps=_deps(db, recorder), settings=NoTreatment())
    assert counts["started"] == 0
    assert recorder.manager_cards == []


@pytest.mark.asyncio
async def test_pending_expect_reports_who_is_awaited():
    """Адресат ожидания читается из ПРЕРЫВАНИЯ, а не из состояния (найденный дефект).

    Узел, вставший в `interrupt()`, не успевает записать `awaiting`: запись применяется при
    возврате узла, а возврата нет. Если бы ожидание читалось из состояния, ответ клиента не
    нашёл бы свой поток, а планировщик будил бы поток, ждущий человека.
    """
    graph, _, _ = make_graph()
    await graph.ainvoke({"booking_id": 31}, config(31))
    assert pending_expect(await graph.aget_state(config(31))) == "manager"

    await graph.ainvoke(resume_manager_value("approve"), config(31))
    snapshot = await graph.aget_state(config(31))
    assert pending_expect(snapshot) == "client"
    assert snapshot.values.get("awaiting") != "client", "поле состояния не называет адресата — это и есть причина"


@pytest.mark.asyncio
async def test_scheduler_does_not_wake_a_flow_waiting_for_manager():
    """Поток, ждущий менеджера, по часам не будим — даже если срок прошлого ожидания истёк.

    Найденный дефект: после повтора по таймауту в состоянии остаётся ПРОСРОЧЕННЫЙ
    `wait_until`, и по нему поток будился бы каждый проход планировщика — то есть каждые
    несколько минут и без пользы. Признак «ждём клиента» обязан решать это.
    """
    db = FakeDb()
    db.candidates = [_candidate(32)]
    graph, _, _ = make_graph(db=db)
    await scheduler.tick(graph=graph, deps=_deps(db, Recorder()), settings=Settings())
    await graph.ainvoke(resume_manager_value("approve"), config(32))

    # Клиент промолчал: поток сделал повтор и снова ждёт МЕНЕДЖЕРА, а срок в состоянии просрочен.
    await graph.ainvoke(resume_client_value(None, timeout=True), config(32))
    snapshot = await graph.aget_state(config(32))
    assert pending_expect(snapshot) == "manager"

    far_future = NOW + dt.timedelta(hours=10)
    counts = await scheduler.tick(
        graph=graph, deps=_deps(db, Recorder(), now=far_future), settings=Settings())
    assert counts["timed_out"] == 0, "поток ждёт человека, а не таймаут"


@pytest.mark.asyncio
async def test_scheduler_resumes_after_wait_deadline_only():
    """Срок ожидания считает планировщик: до него поток не трогают, после — будят."""
    db = FakeDb()
    db.candidates = [_candidate(13)]
    graph, _, _ = make_graph(db=db)
    await scheduler.tick(graph=graph, deps=_deps(db, Recorder()), settings=Settings())
    await graph.ainvoke(resume_manager_value("approve"), config(13))

    assert (await scheduler.tick(graph=graph, deps=_deps(db, Recorder()), settings=Settings()))["timed_out"] == 0

    late_now = NOW + dt.timedelta(hours=5)
    late = _deps(db, Recorder(), now=late_now)
    assert (await scheduler.tick(graph=graph, deps=late, settings=Settings()))["timed_out"] == 1


# --- возобновление после перезапуска (настоящий чекпойнтер) ----------------------

# Чекпойнты тестов живут в ТЕСТОВОЙ базе, а не в базе корпуса RAG: адрес по умолчанию смотрит на
# `…/vectors`, поэтому прогон пересоздал бы чекпойнт-таблицы боевого контура. Таблицы чекпойнтов
# создаёт сам saver — отдельной подготовки не надо.


@pytest.mark.asyncio
async def test_flow_survives_a_restart_of_the_process(bridge_test_dsn: str):
    """Состояние поднимается из чекпойнта НОВЫМ объектом графа — это и есть durable-процесс.

    Объект между «запусками» выбрасывается, как при перезапуске процесса, и работа
    продолжается по базе. Восстановление внутри одного объекта ничего бы не доказало.
    """
    pytest.importorskip("psycopg")
    from realty_agent.reminders.checkpointer import open_checkpointer

    try:
        context = open_checkpointer(bridge_test_dsn)
        saver = await context.__aenter__()
    except Exception as error:  # noqa: BLE001 — недоступная база это пропуск, а не отказ
        pytest.skip(f"чекпойнт-база недоступна: {type(error).__name__}")

    thread = config(901)
    try:
        first, _, first_recorder = make_graph(checkpointer=saver)
        await first.ainvoke({"booking_id": 901}, thread)
        assert len(first_recorder.manager_cards) == 1

        # «Перезапуск»: новый объект графа, состояние берётся из базы.
        second, _, second_recorder = make_graph(checkpointer=saver)
        snapshot = await second.aget_state(thread)
        assert snapshot.values.get("draft"), "черновик обязан подняться из чекпойнта"
        assert snapshot.next, "поток должен ждать менеджера, а не начинаться заново"

        await second.ainvoke(resume_manager_value("approve"), thread)
        assert len(second_recorder.client_messages) == 1, "поток продолжился в новом объекте"
    finally:
        try:
            await saver.adelete_thread(thread_id_for(901))
        except Exception as error:  # noqa: BLE001 — ошибка удаления потока не маскирует результат теста
            log.warning("удаление потока чекпойнтера не удалось: %s", type(error).__name__)
        await context.__aexit__(None, None, None)


# --- признак явки в настоящей схеме ---------------------------------------------

@pytest.fixture
async def bridge_db(empty_test_db: str):
    """СХЕМА моста, но в тестовой базе: отметка явки — часть схемы, и проверять её надо
    в схеме, а не на двойнике."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(empty_test_db, min_size=1, max_size=2)
    except OSError:
        pytest.skip("база моста не поднята — проверьте docker ps и DB_URL")
    except asyncpg.PostgresError:
        pytest.skip("база моста недоступна для подключения — проверьте DB_URL")
    created: dict[str, list] = {"bookings": [], "listings": []}
    try:
        yield pool, created
    finally:
        # Порядок обязателен: сначала брони, потом объекты. У `bookings.listing_id` стоит
        # `ON DELETE SET NULL`, поэтому при обратном порядке бронь осталась бы сиротой.
        if created["bookings"]:
            await pool.execute("DELETE FROM bookings WHERE id = ANY($1::bigint[])", created["bookings"])
        if created["listings"]:
            await pool.execute("DELETE FROM listings WHERE id = ANY($1::text[])", created["listings"])
        await pool.close()


async def _archived_test_listing(pool, created: dict[str, list]) -> str:
    """Тестовый объект — и он обязан быть НЕВИДИМ каталогу клиента.

    `status='archived'` здесь не украшение и не подстраховка на всякий случай. Каталог
    клиента выбирает `status='active'` (`pg.py: active_listings`), а значение по умолчанию
    — `active`, поэтому объект, вставленный без статуса, попадает в каталог, который видит
    человек, и его можно забронировать. Поэтому тест создаёт объект невидимым **для продукта**,
    а не надеется на удаление: удаление может не сработать, а каталог виден сразу.

    `ON CONFLICT ... DO UPDATE` нужен против остатка от прежнего запуска: если объект уже
    лежит активным, повторный прогон обязан его обезвредить, а не «уже есть — пропустить».
    """
    await pool.execute(
        """INSERT INTO listings (id, address, price, rooms, status)
           VALUES ('test-visit-e17', 'ул. Тест, 1', 1, 1, 'archived')
           ON CONFLICT (id) DO UPDATE SET status = 'archived'""")
    created["listings"].append("test-visit-e17")
    return "test-visit-e17"


@pytest.mark.asyncio
async def test_test_listing_is_invisible_to_the_client_catalog(bridge_db):
    """Тестовый объект не должен попадать в каталог, который видит клиент.

    Проверяется тем же условием, по которому каталог и строится (`status='active'`), — иначе
    тест проверял бы не продукт, а своё представление о нём.
    """
    pool, created = bridge_db
    listing_id = await _archived_test_listing(pool, created)

    status = await pool.fetchval("SELECT status FROM listings WHERE id = $1", listing_id)
    seen_by_catalog = await pool.fetchval(
        "SELECT count(*) FROM listings WHERE id = $1 AND status = 'active'", listing_id)

    assert status == "archived"
    assert seen_by_catalog == 0, "тестовый объект виден каталогу клиента — так его забронируют"


@pytest.mark.asyncio
async def test_visit_result_column_holds_three_states(bridge_db):
    """Колонка различает «не отмечено», «пришёл» и «не пришёл» — иначе метрика врёт.

    Слот берётся заведомо прошлый и фиксированный: он не должен совпасть с настоящей
    бронью, иначе уникальный индекс слота отвергнет вставку по чужой вине.
    """
    pool, created = bridge_db
    listing_id = await _archived_test_listing(pool, created)
    booking = await pool.fetchrow(
        """INSERT INTO bookings (listing_id, slot, client_name, client_phone, platform, chat_id,
                                status, confirmed_by)
           VALUES ($1, '2020-01-01T03:00:00+03:00'::timestamptz, 'тест', '+70000000000',
                   'tg', 'test-visit-e17', 'confirmed', 'test-manager')
           RETURNING id, visit_result""", listing_id)
    created["bookings"].append(int(booking["id"]))
    assert booking["visit_result"] is None, "по умолчанию явка не отмечена"

    await pool.execute("UPDATE bookings SET visit_result='attended' WHERE id=$1", booking["id"])
    assert await pool.fetchval("SELECT visit_result FROM bookings WHERE id=$1", booking["id"]) == "attended"

    await pool.execute("UPDATE bookings SET visit_result='no_show' WHERE id=$1", booking["id"])
    assert await pool.fetchval("SELECT visit_result FROM bookings WHERE id=$1", booking["id"]) == "no_show"


@pytest.mark.asyncio
async def test_marking_does_not_touch_pending_booking(bridge_db):
    """Отметка явки возможна только по подтверждённой брони — на `pending` её нет."""
    pool, created = bridge_db
    listing_id = await _archived_test_listing(pool, created)
    booking = await pool.fetchrow(
        """INSERT INTO bookings (listing_id, slot, client_name, client_phone, platform, chat_id, status)
           VALUES ($1, '2020-01-02T03:00:00+03:00'::timestamptz, 'тест', '+70000000000',
                   'tg', 'test-visit-e17-pending', 'pending')
           RETURNING id""", listing_id)
    created["bookings"].append(int(booking["id"]))

    updated = await pool.execute(
        "UPDATE bookings SET visit_result='attended' WHERE id=$1 AND status='confirmed'", booking["id"])
    assert updated == "UPDATE 0", "отметка на pending не должна применяться"


@pytest.mark.asyncio
async def test_scheduler_does_not_start_a_flow_that_a_human_is_resuming():
    """Поток, который прямо сейчас будит ответ человека, проход не трогает.

    Второй `ainvoke` на один `thread_id` — это второй проход узлов и вторые карточки: LangGraph
    сериализует курсор чекпойнтера, а не исполнение. Пропуск безопасен: следующий проход через 300 с
    увидит уже сдвинувшееся состояние.
    """
    from realty_agent.reminders.pipeline import flow_lock

    db = FakeDb()
    db.candidates = [_candidate(11)]
    recorder = Recorder()
    graph, _, _ = make_graph(db=db, recorder=recorder)

    async with flow_lock(11):
        counts = await scheduler.tick(graph=graph, deps=_deps(db, recorder), settings=Settings())

    assert counts["started"] == 0
    assert recorder.manager_cards == [], "занятый поток нельзя будить вторым проходом"
