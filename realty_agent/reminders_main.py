"""Процесс графа напоминаний о визите.

**Зачем отдельным процессом.** У графа свои ресурсы и своя судьба: состояние живёт днями, свой пул
чекпойнтера, свой темп опроса (`visit_reminder_tick_s`). Пока он поднимался внутри моста, рестарт
напоминаний означал рестарт бота, а падение бота уносило незавершённые потоки. Теперь это своя служба:
свой процесс, свой `/health`, свой лог, свой сторож.

**Чего здесь НЕТ и почему.**
* **Клиента Telegram здесь нет** — и это не упущение, а правило проекта «одна сессия — один процесс»
  (единый механизм подключения Telegram). Доставку выполняет мост, а граф кладёт
  исходящие в очередь через дескрипторы кнопок: подпись собирает тот же построитель, что и для
  собственных карточек бота, поэтому кнопка из очереди работает так же.
* **Телеграм-специфики в ручках нет**: процесс принимает продолжение потока (ответ менеджера или
  клиента) и отвечает, какая бронь ждёт ответа в чате — всё остальное решает мост, у которого есть
  сессия.

**Почему ручка `resume` — HTTP, а не строка в базе.** Продолжение потока должно происходить сразу после
нажатия кнопки: ожидание опроса добавило бы к напоминаниям десятки секунд и сделало бы поведение
зависимым от тика планировщика. Форма ручки взята у `news_main.py` (`POST /api/ops/news`): тот же
`X-API-Key` и та же проверка ключа.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent_core import logging_redact, startup, telemetry
from agent_core.api_auth import check_api_key
from realty_agent.db.repo import REVISION, RealtyDb
from realty_agent.reminders.visit_deps import make_visit_deps
from realty_agent.settings import settings

log = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging_redact.install()


class ResumeBody(BaseModel):
    """Продолжение потока: какая бронь и что именно она получает."""

    booking_id: int
    payload: dict


def thread_config(booking_id: int) -> dict:
    """Конфигурация потока LangGraph для брони (тот же ключ, что был в мосте)."""
    from realty_agent.reminders.pipeline import thread_id_for

    return {"configurable": {"thread_id": thread_id_for(booking_id)}}


@contextmanager
def flow_span_for(tracer, chat_id):
    """Спан потока напоминания (`visit.flow`) для продолжений, приходящих по HTTP.

    Хеш чата вместо идентификатора — как у спанов, которые открывает планировщик: форма одна на оба
    входа (`realty_agent/spans.py`). Без этого узлы, разбуженные ответом человека, приезжали бы в платформу
    сиротами, и «цепочка узлов потока» рвалась бы ровно на человеческом шаге.
    """
    from agent_core import spans

    with spans.span(tracer, "visit.flow",
                    spans.workflow_attributes(session_id=spans.session_id_for(str(chat_id or "")))):
        yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Своя база, свой чекпойнтер, своя петля планировщика.

    Порядок важен: сначала база (по ней планировщик выбирает визиты), затем чекпойнтер (по нему
    продолжается поток), затем граф и петля. Незавершённые потоки живут в чекпойнтере, поэтому
    перезапуск процесса их не теряет — это и проверяет приёмка.

    Трейсер настраивается ПЕРВЫМ: его получают зависимости графа (`VisitDeps.tracer`), а через них —
    спаны узлов. Настроить его позже значило бы, что первый проход планировщика идёт без спанов.
    """
    from contextlib import AsyncExitStack

    telemetry.register_roles("visit")
    tracer = telemetry.setup_telemetry(settings, agent="visit")
    app.state.tracer = tracer

    db = RealtyDb(settings.db_url, tz=settings.tz, apply_ddl=False,
                  expected_component="realty", expected_revision=REVISION)
    await startup.wait_ready(db.connect, what="БД моста (напоминания о визите)")

    graph = None
    task = None
    exit_stack = AsyncExitStack()
    try:
        if settings.visit_reminder_enabled:
            from realty_agent.reminders.checkpointer import checkpoint_dsn, open_checkpointer
            from realty_agent.reminders.pipeline import build_visit_graph
            from realty_agent.reminders.scheduler import run_forever

            dsn = checkpoint_dsn(settings.reminders_checkpoint_dsn)
            saver = await exit_stack.enter_async_context(open_checkpointer(dsn))
            deps = make_visit_deps(db=db, settings=settings, tracer=tracer)
            graph = build_visit_graph(deps, checkpointer=saver, settings=settings)
            task = asyncio.create_task(
                run_forever(graph=graph, deps=deps, settings=settings), name="visit-reminders")
            log.info("граф: петля напоминаний запущена (тик %s с)", settings.visit_reminder_tick_s)
        else:
            log.info("граф: напоминания выключены (VISIT_REMINDER_ENABLED=false)")
    except Exception as error:  # noqa: BLE001 — без графа процесс обязан подняться и сказать об этом
        log.warning("граф: напоминания не подняты (%s: %s)", type(error).__name__, error)

    app.state.db = db
    app.state.graph = graph
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await exit_stack.aclose()
        telemetry.flush_telemetry()
        await db.close()
        log.info("граф: процесс остановлен")


#: ❗ Заголовок и `/health.service` — ОДНО имя (`visit-reminders`), и его читает сторож
#: (`infra/health/health-reminders.sh`), а не человек: переименование в `realty-reminders` потребовало бы
#: синхронной правки сторожа и теста. Имя называет ДОМЕН (напоминания о визитах), а не службу — это
#: законно по правилу имён, поэтому в окне 239 оно оставлено осознанно.
app = FastAPI(title="visit-reminders", lifespan=lifespan,
              telemetry=telemetry.fastapi_telemetry())


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    """Живость процесса: включён ли граф и сколько потоков ждут ответа человека.

    Число ждущих — не украшение: по нему видно, что чекпойнтер читается и граф действительно работает,
    а не просто «процесс отвечает на 200».
    """
    graph = getattr(app.state, "graph", None)
    awaiting = 0
    if graph is not None:
        try:
            awaiting = await _awaiting_count(graph)
        except Exception as error:  # noqa: BLE001 — health не роняет ответ
            log.warning("граф: ожидающие потоки не посчитаны (%s)", type(error).__name__)
    return {"status": "ok", "service": "visit-reminders", "enabled": graph is not None, "awaiting": awaiting}


@app.post("/api/ops/visit/resume", include_in_schema=False)
async def visit_resume(body: ResumeBody, x_api_key: str | None = Header(default=None)) -> dict:
    """Продолжить поток брони. Форма та же, что у `POST /api/ops/news`: ключ в `X-API-Key`.

    `resumed=false` — потока нет (кнопка из старого сообщения). Это НЕ ошибка: мост по этому ответу
    просто ничего не делает, а создавать поток из нажатия кнопки нельзя — клиент получил бы напоминание
    «из ниоткуда».
    """
    if not check_api_key(x_api_key, settings.bridge_api_key):
        raise HTTPException(status_code=401, detail="invalid X-API-Key")
    graph = getattr(app.state, "graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="граф напоминаний не поднят")

    from langgraph.types import Command

    from realty_agent.reminders.pipeline import flow_lock

    config = thread_config(body.booking_id)
    lock = flow_lock(body.booking_id)
    if lock.locked():
        # ❗ «Занято» — НЕ «потока нет». Пока тик планировщика исполняет этот поток, второй `ainvoke`
        # прогнал бы узлы дважды: две карточки менеджеру, два сообщения клиенту. Отвечаем ОТДЕЛЬНЫМ
        # полем, чтобы мост сказал человеку правду, а не «напоминание не ведётся».
        log.info("граф: поток брони %s занят проходом планировщика — ответ не будим", body.booking_id)
        return {"status": "busy", "resumed": False}
    snapshot = await graph.aget_state(config)
    if not (snapshot.values and snapshot.next):
        log.info("граф: потока брони %s нет — продолжать нечего", body.booking_id)
        return {"status": "ok", "resumed": False}
    # Спан есть и у продолжения: узлы, разбуженные ответом человека, иначе приезжали бы в платформу
    # сиротами, и «цепочка узлов потока» рвалась бы ровно на человеческом шаге. Трейсер берём
    # необязательным (`getattr`): без него спанов просто нет — как и во всём остальном коде.
    with flow_span_for(getattr(app.state, "tracer", None), snapshot.values.get("chat_id")):
        async with lock:
            await graph.ainvoke(Command(resume=body.payload), config)
    log.info("граф: поток брони %s продолжен (%s)", body.booking_id, body.payload.get("kind"))
    return {"status": "ok", "resumed": True}


@app.get("/api/ops/visit/waiting", include_in_schema=False)
async def visit_waiting(chat_id: str, x_api_key: str | None = Header(default=None)) -> dict:
    """Какая бронь ждёт ответа ЭТОГО чата. Нужна ответу клиента короткой фразой.

    У кнопок бронь есть в подписи (`booking_id` в `callback_data`), а у свободного текста её взять
    неоткуда: чат связан с бронью только через состояние графа. Отвечает владелец состояния — процесс
    графа, а не мост (иначе ресурсы графа «протекли» бы обратно в мост).

    **Сторона сверяется с ОЖИДАНИЕМ, а не одна на оба случая.** Поток, ждущий менеджера, отвечает тому,
    кто записан менеджером (`manager_chat_id`), а ждущий клиента — чату клиента (`chat_id`). Проверка
    «только менеджер» ломает путь клиента дважды: его фраза не находит свой поток, а фраза МЕНЕДЖЕРА
    принимается за ответ клиента — роли меняются местами.
    """
    if not check_api_key(x_api_key, settings.bridge_api_key):
        raise HTTPException(status_code=401, detail="invalid X-API-Key")
    graph = getattr(app.state, "graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="граф напоминаний не поднят")

    from realty_agent.reminders.pipeline import AWAIT_CLIENT, AWAIT_MANAGER, pending_expect

    db = app.state.db
    rows = await db.visit_candidates(ahead_h=48, back_h=168, limit=50)
    for row in rows:
        snapshot = await graph.aget_state(thread_config(int(row["id"])))
        if not (snapshot.values and snapshot.next):
            continue
        expect = pending_expect(snapshot)
        if expect == AWAIT_MANAGER:
            party = str(snapshot.values.get("manager_chat_id") or "")
        elif expect == AWAIT_CLIENT:
            party = str(snapshot.values.get("chat_id") or row.get("chat_id") or "")
        else:
            continue  # поток не ждёт ответа человека (или ждёт не того, кого можно назвать)
        if party and party == str(chat_id):
            return {"status": "ok", "booking_id": int(row["id"]), "awaiting": expect}
    return {"status": "ok", "booking_id": None, "awaiting": None}


async def _awaiting_count(graph) -> int:
    """Сколько потоков стоят на прерывании (ждут человека)."""
    from realty_agent.reminders.pipeline import pending_expect

    db = app.state.db
    rows = await db.visit_candidates(ahead_h=48, back_h=168, limit=50)
    total = 0
    for row in rows:
        snapshot = await graph.aget_state(thread_config(int(row["id"])))
        if snapshot.values and snapshot.next and pending_expect(snapshot):
            total += 1
    return total
