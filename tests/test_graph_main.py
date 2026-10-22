"""Процесс графа напоминаний — ручки и шов доставки, без сети.

**Что проверяется и почему именно это.**
1. **Ручки процесса**: `resume` и `waiting` требуют ключ (иначе кто угодно с хоста мог бы продолжать
   потоки и выпытывать, какая бронь чего ждёт), а `health` — нет (его дёргает сторож без ключа).
2. **Чужого потока не создаём**: `resume` на бронь без потока отвечает `resumed=false`, а не запускает
   граф — иначе нажатие кнопки из старой истории чата прислало бы клиенту напоминание «из ниоткуда».
3. **Шов доставки**: зависимости графа кладут исходящее в ОЧЕРЕДЬ (кнопки — дескрипторами), а не
   отправляют сами: клиента Telegram у процесса нет по правилу «одна сессия — один процесс».
4. **Тип параметра SQL**: `visit_candidates` принимает `timedelta`, а не строку (`interval`). На
   двойнике базы ошибка типа не видна, поэтому проверка идёт в живую базу — и честно пропускается,
   если базы нет.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

KEY = "k" * 40


class _FakeGraph:
    """Двойник графа: помнит, что у него спросили и что ему передали."""

    def __init__(self, *, exists: bool = True, awaiting: str = "manager",
                 client: str = "111") -> None:
        self.exists = exists
        self.awaiting = awaiting
        self.client = client
        self.resumed: list[tuple[int, dict]] = []

    async def aget_state(self, config):
        from types import SimpleNamespace

        if not self.exists:
            return SimpleNamespace(values=None, next=(), tasks=())
        # Форма снимка — как у настоящего графа: кого поток ждёт, видно из ПРЕРЫВАНИЯ, а не из
        # состояния (`pending_expect` читает `tasks[].interrupts[].value` — узел, вызвавший
        # `interrupt`, не возвращается и поле `awaiting` записать не успевает).
        interrupt = SimpleNamespace(value={"expect": self.awaiting, "draft": "текст"})
        return SimpleNamespace(values={"manager_chat_id": "555", "chat_id": self.client},
                               next=("manager_review",),
                               tasks=[SimpleNamespace(interrupts=[interrupt])])

    async def ainvoke(self, command, config):
        self.resumed.append((config["configurable"]["thread_id"], getattr(command, "resume", None)))
        self.exists = False
        return {}


class _FakeDb:
    async def visit_candidates(self, **kwargs):
        return [{"id": 7, "slot": None, "confirmed_by": "555", "chat_id": "111"}]

    async def booking_for_visit(self, *, booking_id: int):
        return {"id": booking_id, "confirmed_by": "555", "listing_id": "L-1"}

    async def enqueue_outbox(self, *, kind: str, payload: dict) -> int:
        rows = getattr(self, "rows", None)
        if rows is None:
            rows = self.rows = []
        rows.append({"kind": kind, "payload": payload})
        return len(rows)


@pytest.fixture
def client(monkeypatch):
    """Клиент БЕЗ запуска lifespan: ручки проверяются на подставленных графе и базе."""
    import realty_agent.reminders_main as main
    from realty_agent.settings import settings

    monkeypatch.setattr(settings, "bridge_api_key", KEY)
    main.app.state.graph = _FakeGraph()
    main.app.state.db = _FakeDb()
    return TestClient(main.app)


def test_health_is_open_and_names_the_service(client):
    """`/health` без ключа (сторож ходит без него) и называет СЕБЯ — иначе чужой порт сошёл бы за наш."""
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "visit-reminders"
    assert body["enabled"] is True


def test_resume_requires_the_key(client):
    """Без ключа поток продолжать нельзя: это действие от имени менеджера."""
    assert client.post("/api/ops/visit/resume", json={"booking_id": 1, "payload": {}}).status_code == 401


def test_resume_continues_the_flow(client):
    """Продолжение доходит до графа ровно с тем payload, который передал мост."""
    from realty_agent.reminders.pipeline import thread_id_for

    response = client.post("/api/ops/visit/resume",
                           json={"booking_id": 7, "payload": {"kind": "manager", "decision": "approve"}},
                           headers={"X-API-Key": KEY})

    assert response.status_code == 200 and response.json() == {"status": "ok", "resumed": True}
    thread, payload = client.app.state.graph.resumed[0]
    assert thread == thread_id_for(7), "продолжение обязано уйти в поток ЭТОЙ брони, а не в чужой"
    assert payload == {"kind": "manager", "decision": "approve"}


def test_resume_of_unknown_flow_does_not_start_a_new_one(client):
    """Нет потока — не начинаем: кнопка из старой истории не должна слать напоминание «из ниоткуда»."""
    client.app.state.graph = _FakeGraph(exists=False)

    response = client.post("/api/ops/visit/resume", json={"booking_id": 7, "payload": {"kind": "manager"}},
                           headers={"X-API-Key": KEY})

    assert response.json() == {"status": "ok", "resumed": False}
    assert client.app.state.graph.resumed == []


def test_waiting_requires_the_key_and_reports_the_booking(client):
    """Свободному тексту менеджера нужен ответ «какая бронь ждёт» — и только своему."""
    assert client.get("/api/ops/visit/waiting", params={"chat_id": "555"}).status_code == 401
    body = client.get("/api/ops/visit/waiting", params={"chat_id": "555"},
                      headers={"X-API-Key": KEY}).json()
    assert body["booking_id"] == 7 and body["awaiting"] == "manager"


def test_waiting_answers_the_side_that_is_actually_awaited(client):
    """Сторона сверяется с ОЖИДАНИЕМ: ждущего клиента находит его чат, а менеджер — нет.

    Инвариант: сверяться надо с ОЖИДАЕМОЙ стороной, а не только с `manager_chat_id` — единственный
    вызов ручки клиентский (`_visit_answer`). Иначе фраза клиента («приду») не находит свой поток и
    уходит в диалог к модели, а фраза МЕНЕДЖЕРА принимается за ответ клиента — роли меняются местами.
    Чат клиента — это `row["chat_id"]`.
    """
    import realty_agent.reminders_main as main

    main.app.state.graph = _FakeGraph(awaiting="client", client="111")

    ours = client.get("/api/ops/visit/waiting", params={"chat_id": "111"},
                      headers={"X-API-Key": KEY}).json()
    assert ours["booking_id"] == 7 and ours["awaiting"] == "client", \
        "чат клиента обязан найти поток, который ждёт клиента"

    managers = client.get("/api/ops/visit/waiting", params={"chat_id": "555"},
                          headers={"X-API-Key": KEY}).json()
    assert managers["booking_id"] is None, \
        "чат менеджера не отвечает за клиента: поток ждёт не его"


def test_waiting_answers_the_manager_of_a_manager_waiting_flow(client):
    """Обратная сторона того же: ждущего менеджера находит его чат — и только он."""
    import realty_agent.reminders_main as main

    main.app.state.graph = _FakeGraph(awaiting="manager", client="111")

    ours = client.get("/api/ops/visit/waiting", params={"chat_id": "555"},
                      headers={"X-API-Key": KEY}).json()
    assert ours["booking_id"] == 7 and ours["awaiting"] == "manager"

    others = client.get("/api/ops/visit/waiting", params={"chat_id": "111"},
                        headers={"X-API-Key": KEY}).json()
    assert others["booking_id"] is None, "клиент не отвечает за менеджера"


@pytest.mark.asyncio
async def test_visit_deps_enqueue_with_button_descriptors():
    """Доставка графа идёт в очередь, кнопки — ДЕСКРИПТОРАМИ, и ярлыки те же, что были в мосте.

    Готовые объекты Telethon в чужой процесс не передать, поэтому в payload едут `label`+`action`, а
    подпись собирает мост тем же построителем. Проверяем и ярлыки: если они разойдутся, менеджер
    увидит другие кнопки, и заметили бы это только в бою.
    """
    from realty_agent.reminders.visit_deps import make_visit_deps

    class _Settings:
        tz = None

        def __getattr__(self, name):
            raise AttributeError(name)

    db = _FakeDb()
    deps = make_visit_deps(db=db, settings=_Settings())

    accepted = await deps.notify_manager(7, "черновик текста")
    sent = await deps.send_client(7, "напоминание", "111")

    assert accepted is True and sent is True
    kinds = [row["kind"] for row in db.rows]
    assert kinds == ["visit_reminder", "visit_reminder"], "оба исходящих идут в очередь"
    manager_row = db.rows[0]["payload"]
    assert manager_row["chat_id"] == "555" and manager_row["booking_id"] == 7
    assert [item["label"] for item in manager_row["buttons"]] == ["✅ Отправить клиенту", "✏️ Переписать"]
    assert [item["action"] for item in db.rows[1]["payload"]["buttons"]] == ["vy", "vm"]


@pytest.mark.asyncio
async def test_visit_deps_show_a_human_slot_not_a_machine_stamp():
    """Слот в тексте клиенту — «15.10 10:13», а не `2026-10-15 07:13:35.013830+00:00`.

    Живой дефект: в состоянии графа слот лежит СТРОКОЙ (`slot_iso`), а `format_slot` понимает только
    `datetime` и на строке возвращает её же — клиент получал машинную метку, причём в UTC (то есть
    на три часа раньше настоящего времени визита). Здесь проверяются ДЕПЫ, а не отдельная функция:
    именно в них выбирается, какой из двух форматтеров позвать.
    """
    from zoneinfo import ZoneInfo

    from realty_agent.reminders.visit_deps import make_visit_deps

    class _Settings:
        tz = ZoneInfo("Europe/Moscow")

    deps = make_visit_deps(db=_FakeDb(), settings=_Settings())

    assert deps.format_slot("2026-10-15 07:13:35.013830+00:00") == "15.10 10:13"
    # Наивную строку привязываем к бизнес-таймзоне (как `parse_slot` в базе): «07:13» — это МСК.
    assert deps.format_slot("2026-10-15T07:13") == "15.10 07:13"


@pytest.mark.asyncio
async def test_visit_deps_survive_a_broken_queue():
    """Недоступная очередь — не падение графа: функция говорит «не принято», и поток останавливается."""
    from realty_agent.reminders.visit_deps import make_visit_deps

    class _Broken(_FakeDb):
        async def enqueue_outbox(self, *, kind: str, payload: dict) -> int:
            raise ConnectionError("очередь недоступна")

    class _Settings:
        tz = None

        def __getattr__(self, name):
            raise AttributeError(name)

    deps = make_visit_deps(db=_Broken(), settings=_Settings())

    assert await deps.notify_manager(7, "черновик") is False


@pytest.mark.asyncio
async def test_visit_candidates_works_on_the_live_database(empty_test_db: str):
    """Параметр окна — `timedelta`, а не строка «168 hours».

    Строка валит вечерний тик планировщика в `DataError` (asyncpg ждёт `interval`), и двойником базы
    это не проверить: запрос выполняет ТОЛЬКО планировщик графа. Поэтому проверка идёт в НАСТОЯЩУЮ
    базу и пропускается, если её нет.
    """
    pytest.importorskip("asyncpg")   # без драйвера тест не имеет смысла — пропускаем честно
    from realty_agent.db.repo import RealtyDb

    dsn = empty_test_db   # СВОЯ база: боевая не должна зависеть от прогона тестов
    db = RealtyDb(dsn, apply_ddl=False)
    try:
        await db.connect()
    except Exception:  # noqa: BLE001
        pytest.skip("база моста не поднята — проверьте docker ps и DB_URL")

    try:
        rows = await db.visit_candidates(ahead_h=48, back_h=168, limit=5)
        assert isinstance(rows, list), "запрос обязан выполняться, а не падать на типе параметра"
    finally:
        await db.close()


def test_busy_flow_is_not_the_same_as_absent_flow(client):
    """Поток занят проходом планировщика → `status=busy`, а НЕ `resumed=false`.

    Один `thread_id` будят два входа: тик планировщика (раз в 300 с) и ответ человека. LangGraph
    сериализует курсор чекпойнтера, а не исполнение, поэтому два `ainvoke` на один поток — это два
    прохода узлов и **две** карточки менеджеру/клиенту. Отказ ручки нельзя выдавать за «напоминание
    не ведётся»: человек увидел бы ложную причину.
    """
    import asyncio
    import threading

    from realty_agent.reminders.pipeline import flow_lock

    #: `TestClient` синхронный, а замок — асинхронный: держим его из чужой петли, пока идёт запрос.
    holder_ready, release = threading.Event(), threading.Event()
    outcome: dict = {}

    def hold() -> None:
        async def scenario() -> None:
            async with flow_lock(7):
                holder_ready.set()
                await asyncio.get_running_loop().run_in_executor(None, release.wait)
        asyncio.run(scenario())

    holder = threading.Thread(target=hold)
    holder.start()
    holder_ready.wait(timeout=5)

    outcome["response"] = client.post(
        "/api/ops/visit/resume",
        json={"booking_id": 7, "payload": {"kind": "manager", "decision": "approve"}},
        headers={"X-API-Key": KEY},
    )
    release.set()
    holder.join(timeout=5)

    response = outcome["response"]
    assert response.json() == {"status": "busy", "resumed": False}
    assert client.app.state.graph.resumed == [], "занятый поток нельзя будить вторым проходом"
