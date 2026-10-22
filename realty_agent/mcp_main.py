"""MCP-листенер отдельным процессом.

**Зачем отдельный процесс.** До этого пункта листенер жил **корутиной внутри процесса моста**
(`realty_agent/main.py`, символ `_serve_mcp`), и это давало три следствия сразу: порт 8087 держал тот же pid,
что и бот (проверено `ss -ltnp`: 8087 → pid моста); посторонний протокол жил на порту бота — MCP был
**вторым маунтом** (`127.0.0.1:8085/mcp` отвечал `307`); рестарт инструментов означал рестарт бота, а
падение листенера могло унести бота с собой. Теперь это своя служба: свой процесс, свой порт, свой
лог, свой `/health`, свой сторож.

**Что здесь есть и чего здесь нет.** Модуль намеренно тонкий: приложение листенера уже собрано в
`realty_agent/mcp/server.py::build_standalone_mcp_app` — со своим `/health` (на СВОЁМ пути, не под `/mcp`),
своей сцепкой lifespan и ACL по адресу клиента (`403` для чужих сетей). Здесь только то, что
отличает процесс от модуля: своя база, ожидание готовности схемы и логирование.

**Почему `apply_ddl=False` и `expected_tables`.** Схему создаёт и мигрирует основной мост — второй
создатель схемы означал бы второй снапшот DDL и расхождение, которое находится в бою. Поэтому листенер
только **проверяет**, что нужные ему таблицы есть, и падает громко, если схема ещё не применена
(`realty_agent/db/pg.py::connect`), — вместо тихих пустых ответов инструментов.

**Какие таблицы «его».** Набор вычислен по коду инструментов, а не назначен: `db.manager_role` читает
`managers` (роль вызывающего —a`), `active_listings` — `listings` (каталог), `listing_notes` —
`listing_jobs` (заметки менеджеров), `db.setting` — `settings` (`schedule` для слотов), `db.busy_slots`
— `bookings` (занятость слотов). Совпадение набора с реальной схемой закрыто тестом: риск
`apply_ddl=False` именно в том, что набор забудут обновить вместе с инструментами.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_core import logging_redact, startup, telemetry
from realty_agent.db.repo import MCP_TABLES, REVISION, RealtyDb
from realty_agent.mcp.server import build_standalone_mcp_app
from realty_agent.settings import settings

log = logging.getLogger(__name__)

#: Как и в остальных процессах проекта: без этого INFO-логи не попадают в stderr, а URL-содержащие
# строки сторонних библиотек глушатся.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging_redact.install()

#: Живая база процесса. Инструменты берут её **на каждом вызове**, а не на сборке приложения: на
#: момент импорта базы ещё нет, а после переподключения ссылка должна остаться рабочей.
_live: dict = {}


def _db():
    """Поставщик базы для инструментов (тот же приём, что был у смонтированного приложения)."""
    return _live.get("db")


# Приложение листенера строится на импорте (нужно для монтирования), а база подключается в lifespan
# ниже: поэтому поставщик, а не сама база.
_inner = build_standalone_mcp_app(_db, tz=settings.tz)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Своя база, проверка схемы и сцепка lifespan листенера.

    Сцепка обязательна: у MCP своя задача-группа, и без входа в её lifespan эндпоинт отвечает
    «Task group is not initialized». Порядок: сначала база, затем листенер, выход — в обратном.
    """
    db = RealtyDb(
        settings.db_url, tz=settings.tz, apply_ddl=False,
        expected_tables=MCP_TABLES,  # схему создаёт мост, листенер только проверяет свои таблицы
        expected_component="realty",
        expected_revision=REVISION,
    )
    await startup.wait_ready(db.connect, what="БД (схема инструментов MCP)")
    _live["db"] = db
    app.state.db = db
    log.info("mcp: листенер поднят своим процессом на %s:%s", settings.mcp_bind_host, settings.mcp_port)
    try:
        async with _inner.router.lifespan_context(app):
            yield
    finally:
        await db.close()


app = FastAPI(title="realty-mcp", lifespan=lifespan,
              telemetry=telemetry.fastapi_telemetry())
# Монтирование в корень: у листенера свои пути (`/mcp` — инструменты, `/health` — проверка живости),
# и они должны остаться такими же, как были на порту моста. Иначе поехал бы адрес, который знает
# MCP-клиент (`host.docker.internal:8087/mcp/`).
app.mount("/", _inner)
