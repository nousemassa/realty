"""FastAPI-приложение bridge: health + генерация объявлений (F2).

Что поднимается в lifespan:
  * БД (asyncpg-пул, DDL, настройки);
  * клиент канального пути платформы (connection/agent — идемпотентно);
  * роутеры HTTP API.
Telegram-транспорт (Telethon/MTProto через прокси) поднимается в том же lifespan и при
недоступности Telegram деградирует мягко (API продолжает работать, /health отдаёт telegram:false).
"""
from __future__ import annotations

import gc
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from agent_core import log_trace, logging_redact, metrics, startup, telemetry
from agent_core.db.store import Db
from agent_core.vendors.runtime import runtime_for
from realty_agent.agent_roles import roles as agent_roles
from realty_agent.api.listings_router import router as listings_router
from realty_agent.api.ops_router import router as ops_router
from realty_agent.db.repo import RealtyDb
from realty_agent.retrieval.retriever import RealtyRetriever
from realty_agent.settings import settings
from realty_agent.tg.bot import TelegramTransport
from realty_agent.turns import TurnStore

# Без этого INFO-логи приложения (в т.ч. «бот запущен») не попадают в stderr: uvicorn
# настраивает только свои логгеры, а root остаётся без обработчиков.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("telethon").setLevel(logging.WARNING)  # иначе каждый реконнект — пачка INFO
# httpx печатает URL запроса в INFO, а приватный iCal-адрес — это токен на чтение календаря.
#: Уровень WARNING гасит запись У ИСТОЧНИКА: фильтр на обработчиках не спасает, потому что uvicorn
#: перенастраивает логирование и токен попадает в журнал.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging_redact.install()  # вторая линия обороны: маскировка /private-<токен> в своих сообщениях

log = logging.getLogger(__name__)

# Отладочный лог ввода-вывода хода. Поднимаем уровень ТОЛЬКО у своих логгеров
# (`realty_agent.*` и `agent_core.*`) и только по явному флагу: в отладочном режиме в локальный файл
# попадает текст клиента, поэтому это осознанное действие, а не «включим на всякий случай».
# Чужой шум (httpx, uvicorn) при этом не поднимается — иначе ход в журнале не найти.
if settings.turn_io_debug:
    #: ❗ Имена НАШИХ логгеров после переименования пакета — `realty_agent.*` и `agent_core.*`; со старым
    #: списком (`app`/`bridge`) флаг `TURN_IO_DEBUG` молча ничего не делал бы. Заодно убрано смешанное
    #: именование: явные `bridge.*` переименованы в `realty_agent.*` (см. `dialog.py`, `tg/*`).
    for name in ("realty_agent", "agent_core"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    log.info(
        "main: ОТЛАДОЧНЫЙ лог ввода-вывода хода включён (TURN_IO_DEBUG=1): в журнал идут "
        "полный текст хода и полный ответ агента"
    )

# Живая база для инструментов MCP: заполняется в lifespan, читается поставщиком.

STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = RealtyDb(settings.db_url, tz=settings.tz)
    # После перезагрузки хоста Postgres поднимается позже моста — ждём, вместо падения с exit 3 (a).
    await startup.wait_ready(db.connect, what="БД (BRIDGE_DB_URL)")

    #: Векторная база RAG. Схему здесь НЕ создаём (`apply_ddl=False`): её накатывает накат схемы
    #: продукта (компонент `vectors`). Запуск диалогового контура не должен зависеть от состояния базы
    #: знаний — если она не поднята, мост работает без базы знаний, а не падает. Поэтому подключение
    #: обёрнуто в try. `expected_tables` не задаём: проверять её здесь нечем и незачем.
    vector_db = Db(settings.vector_db_url, apply_ddl=False)
    retriever = None
    try:
        await startup.wait_ready(vector_db.connect, what="БД базы знаний (VECTOR_DB_URL)", budget_s=15.0)
        retriever = RealtyRetriever(
            vector_db.pool,
            embed_url=settings.llama_embed_url,
            embed_api_key=settings.llama_embed_api_key,
            embed_model=settings.llama_embed_model,
            embed_timeout_s=settings.llama_embed_timeout_s,
        )
    except Exception as error:  # noqa: BLE001 — без базы знаний мост обязан работать
        log.warning("main: база знаний недоступна (%s: %s) — ход пойдёт без RAG",
                    type(error).__name__, error)

    # Два независимых канала: диалог клиента (запись на просмотр) и генерация объявлений.
    # ❗ Через ПОРТ рантайма (`agent_core/vendors/runtime/`): сегодня это делегат над прежним клиентом
    # (поведение не меняется), потом сможет быть нашим харнессом — сайты при этом не меняются.
    roles = agent_roles()
    dialog = runtime_for(settings, role=roles["dialog"])
    listing = runtime_for(settings, role=roles["listing"])
    runtimes = {"dialog": dialog, "listing": listing}

    #: Своему рантайму диалога нужна история реплик — загрузчик ставим ЗДЕСЬ: у моста уже есть пул базы,
    #: а второй пул в том же процессе означал бы две правды о подключении. У объявлений истории нет по
    #: смыслу (каждый ход — новый тред), поэтому загрузчик им не ставится ВОВСЕ, а не «ставится пустой».
    if getattr(dialog, "name", "") == "own":
        async def _dialog_history(user: str, limit: int) -> list[dict]:
            platform, _, chat_id = user.partition(":")
            return await db.load_dialog_history(platform=platform or "tg", chat_id=chat_id, limit=limit)
        dialog.history_loader = _dialog_history

    #: ❗ Готовность проверяется у КАЖДОЙ роли своим жизненным циклом. Раньше блоки были скопированы под
    #: роль и гейтились наличием `*_workflow_id/*_revision_id` — у своего рантайма этих ключей нет, и
    #: проверка не выполнялась бы вовсе (первый ход узнавал бы о незаданном промпте вместо старта).
    for role_name, role in roles.items():
        runtime = runtimes[role_name]
        is_own = getattr(runtime, "name", "") == "own"
        if is_own or (role.workflow_id and role.revision_id):
            await startup.wait_ready(
                lambda runtime=runtime, slug=role.connection_slug: runtime.ensure_connection(slug=slug),
                what=f"рантайм {role_name} ({getattr(runtime, 'name', '')}.ensure_connection)",
            )
            await startup.wait_ready(
                lambda runtime=runtime, w=role.workflow_id, r=role.revision_id, slug=role.bot:
                runtime.ensure_agent(workflow_id=w, workflow_revision_id=r, slug=slug),
                what=f"рантайм {role_name} ({getattr(runtime, 'name', '')}.ensure_agent)",
            )

    #: Правда о ДВУХ ролях — в `runtimes`; плоская пара оставлена как была (её читают доки и скрипты) и
    #: означает РОЛЬ ДИАЛОГА. Так `/health` не соврёт про объявления, не меняя старый контракт.
    app.state.runtimes = {
        role: {"name": getattr(runtime, "name", ""), "ready": bool(getattr(runtime, "ready", False))}
        for role, runtime in runtimes.items()
    }
    app.state.agent_runtime = app.state.runtimes["dialog"]["name"]
    app.state.agent_runtime_ready = app.state.runtimes["dialog"]["ready"]

    # Durable-состояние хода. Реконсиляция идёт ДО приёма сообщений: залипшие ходы
    # разбираются на старте, а не когда до них дойдёт очередь. Залипший ход в `sent`/
    # `awaiting` НЕ переигрывается — ответ мог уйти клиенту, и повтор дал бы второй ответ.
    turn_store = TurnStore(db.pool)
    try:
        for state in await turn_store.reconcile():
            log.warning("main: залипший ход %s разобран при старте — статус %s",
                        state.turn_key, state.status)
    except Exception as error:  # noqa: BLE001 — реконсиляция не должна мешать старту моста
        log.error("main: реконсиляция ходов не удалась (%s: %s)", type(error).__name__, error)

    # Трассировка: трейсеры собираются ЗДЕСЬ, один раз на процесс, и передаются потребителям.
    # Ролей ДВЕ, и это не украшение: в приёмнике `service.name` — ключ группировки, поэтому диалог и
    # генерация объявлений видны как разные агенты (`agent-dialog` / `agent-listing`), а не как одна
    # безымянная служба моста. Пустой `OTEL_ENDPOINT` — штатное состояние: трейсер-пустышка.
    telemetry.register_roles("dialog", "listing")
    tracer = telemetry.setup_telemetry(settings, agent="dialog")
    listing_tracer = telemetry.setup_telemetry(settings, agent="listing")
    # Фактический приём спанов — от АКТИВНОГО адаптера вендора, а не из `OTEL_ENDPOINT`: при
    # `PLATFORM_VENDOR=openlit` адрес платформы остаётся прежним, а спаны уходят в другой приёмник.
    # Показываем это в `/health`: иначе на вопрос «куда уходят спаны» ответить нечем.
    from agent_core import vendors

    app.state.otel_endpoint = vendors.active().receiver(settings).endpoint
    #: `trace_id` в журнале — ПОСЛЕ uvicorn: он перенастраивает логирование уже после импорта
    #: приложения, поэтому форматёры оборачиваются здесь.
    log_trace.install("realty_agent")

    telegram = TelegramTransport(
        settings=settings, db=db, channels=dialog, retriever=retriever, turns=turn_store,
        tracer=tracer,
    )
    await telegram.start()

    # MCP-листенер здесь БОЛЬШЕ НЕ ПОДНИМАЕТСЯ: он вынесен своим процессом (,
    # `realty/bridge-up-mcp.sh`, юнит `bridge-mcp`). Держать его в мосте значило
    # держать на порту бота посторонний протокол и платить за инструменты простоем бота:
    # теперь падение листенера не касается бота, а падение бота не уносит инструменты.

    app.state.settings = settings
    app.state.db = db
    app.state.vector_db = vector_db
    app.state.retriever = retriever
    app.state.turns = turn_store
    app.state.dialog_channels = dialog
    app.state.listing_channels = listing
    app.state.vision = settings.vision_credentials()
    app.state.telegram = telegram
    app.state.tracer = tracer
    app.state.listing_tracer = listing_tracer

    gc.collect()
    gc.freeze()
    yield
    await telegram.stop()
    await dialog.aclose()
    await listing.aclose()
    await vector_db.close()
    await db.close()


app = FastAPI(title="realty-bridge", lifespan=lifespan,
              telemetry=telemetry.fastapi_telemetry())
app.include_router(listings_router)
app.include_router(ops_router)


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Метрики в формате Prometheus — для алертов и Grafana.

    Числа дня читаются на момент опроса, а не кэшируются: окно у метрик то же, что у отчёта владельцу
    (`turn_report`), и «одно число из двух источников» было бы хуже, чем лишний запрос при скрейпе.

    Недоступная база не роняет ручку: отдаём состояние процесса и `bridge_db_up 0` — иначе мониторинг
    потерял бы и признаки жизни сервиса тоже.
    """
    db = getattr(app.state, "db", None)
    stats = getattr(getattr(app.state, "telegram", None), "stats", dict)()
    day = None
    outbox = None
    if db is not None:
        try:
            day = await db.turn_report(days=1)
        except Exception as error:  # noqa: BLE001 — метрики не имеют права падать
            log.warning("main: метрики дня не прочитаны (%s)", type(error).__name__)
        try:
            # Числа очереди лежат в `meta_metrics` — отдельным запросом, потому что их пишет
            # не ход диалога, а воркер доставки.
            outbox = await db.day_metrics(None, names=["outbox_sent", "outbox_failed",
                                                       "outbox_retries", "outbox_lag_s"])
        except Exception as error:  # noqa: BLE001 — без очереди метрики процесса остаются
            log.warning("main: метрики очереди не прочитаны (%s)", type(error).__name__)
    text = metrics.render(
        prefix="bridge",
        started_at=STARTED_AT,
        telegram_running=bool(getattr(app.state, "telegram", None) and app.state.telegram.running),
        stats=stats if isinstance(stats, dict) else {},
        day=day,
        outbox=outbox,
        extra=[("day_bookings", (day or {}).get("bookings", 0), "броней за окно")] if day else [],
    )
    return Response(content=text, media_type="text/plain; version=0.0.4")


@app.get("/health")
async def health() -> dict:
    db_ok = True
    jobs_today = None
    try:
        jobs_today = await app.state.db.jobs_today()
    except Exception:  # noqa: BLE001 - health не должен падать
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "uptime_s": int(time.time() - STARTED_AT),
        "bot_mode": settings.bridge_bot_mode,
        "provider": settings.llm_provider,
        "platform_api": settings.platform_api_url,
        # Вендор наблюдаемости и ФАКТИЧЕСКИЙ приём спанов (`F1`). Поля нужны человеку и сторожу: после
        # переключения `PLATFORM_VENDOR` вопрос «куда уходят спаны» задаётся первым, а по одному
        # `platform_api` ответить на него нельзя — адрес платформы остаётся прежним даже когда спаны
        # идут в другой приёмник. Названо НАШИМ именем (`otel_endpoint`), а не вендорским.
        "platform_vendor": settings.platform_vendor,
        # Рантайм хода: КТО исполнил ход — платформа или мы сами, и готов ли он. Без этих
        # полей «какой рантайм работает» неотличимо от «какой указали»: `AGENT_RUNTIME=own` при пустом
        # пути к промпту падал бы на первом ходе клиента, а `/health` показывал бы `ok`.
        #: Плоская пара — РОЛЬ ДИАЛОГА (оставлена для совместимости: её читают доки и скрипты).
        #: Правда о всех ролях — в `runtimes`; сторож читает только status/telegram/возраст апдейта.
        "agent_runtime": getattr(app.state, "agent_runtime", ""),
        "agent_runtime_ready": bool(getattr(app.state, "agent_runtime_ready", False)),
        "runtimes": getattr(app.state, "runtimes", {}),
        "otel_endpoint": getattr(app.state, "otel_endpoint", ""),
        "listing_jobs_today": jobs_today,
        #: По этим числам сторож отличает «бот молчит» от «просто тихо».
        **getattr(getattr(app.state, "telegram", None), "stats", dict)(),
        "telegram": bool(getattr(app.state, "telegram", None) and app.state.telegram.running),
        "telegram_transport": "mtproto",
        "rss_mb": metrics.rss_mb(),
    }
