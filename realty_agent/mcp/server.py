"""Сборка MCP-сервера как ASGI-приложения и монтаж в FastAPI-приложение.

**Где он живёт — отдельным процессом.** У листенера СВОЙ процесс (`realty_agent.mcp_main:app`, порт
8087) и свой сторож; в контейнерном режиме — служба `mcp` проекта `realty`. Монтаж в FastAPI остался
способом СОБРАТЬ приложение, а не местом его жизни.

**Почему не в одном процессе с мостом.** Инструменты читают ту же базу и те же настройки, что мост, и
отдельный процесс даёт свой пул и свою копию конфигурации — зато падение листенера не роняет бота, а
падение бота не уносит инструменты.

**Транспорт — `streamable-http`, и это требование платформы, а не наш выбор.**
MCP-клиент принимает только его (`transport: Literal["streamable-http"]`,
«Use a streamable-http MCP server»); stdio и SSE не поддерживаются. С транспортом нашего
HTTP API это не конфликтует: там SSE был отвергнут как лишний, здесь форма задана чужой
стороной.
"""
from __future__ import annotations

import ipaddress
import logging
from contextlib import asynccontextmanager

from agent_core import telemetry
from realty_agent.mcp.tools import build_server

log = logging.getLogger(__name__)

# Кто вправе звать инструменты. Удостоверения у сервера нет и быть не может: шлюз
# платформы пересылает только заголовки из своего allowlist (`content-type`, `accept`,
# `mcp-session-id`, `anthropic-*`, …), ни нашего секрета, ни идентичности пользователя в
# нём нет — проверить, что вызов «свой», по заголовкам нечем. Поэтому единственная
# доступная преграда — сетевой адрес вызывающего.
ALLOWED_CLIENT_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),      # сам хост: проверки, скрипты, smoke-прогон
    ipaddress.ip_network("172.16.0.0/12"),    # docker-сети: оттуда зовёт платформа
)

# Путь, по которому инструменты доступны. Вынесен константой: тем же путём пользуется
# регистрация в шлюзе платформы и проверочный скрипт.
# Префикс монтирования. **Канонический адрес эндпоинта — со слэшем на конце** (`/mcp/`):
# смонтированное ASGI-приложение отдаёт свой корень, и запрос без слэша получает 307.
# Для MCP-клиента это важно: POST с телом на редирект может уехать без тела, поэтому
# адрес для регистрации в шлюзе — именно `MCP_ENDPOINT`.
MCP_PATH = "/mcp"
MCP_ENDPOINT = "/mcp/"


def client_allowed(client: tuple | None) -> bool:
    """Пускаем только docker-сеть и сам хост; всё прочее — отказ.

    Проверка вынесена функцией: её проверяет тест, и она же нужна middleware. Закрыто
    «на всякий случай» — нет: `0.0.0.0` означает, что тот же порт слушает и на сетевом
    интерфейсе хоста, то есть каталог и слоты читались бы из локальной сети.
    """
    if not client:
        return False  # нет адреса — считаем вызывающего неизвестным, а не своим
    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    return any(address in network for network in ALLOWED_CLIENT_NETS)


def build_standalone_mcp_app(db, *, tz=None):
    """MCP как САМОСТОЯТЕЛЬНОЕ приложение — для отдельного слушателя.

    Зачем отдельно, а не только смонтированным в основной API. Платформа обращается к
    инструментам из контейнера, а основной API моста слушает 127.0.0.1 (это осознанно:
    доступ через ssh-туннель, наружу не публикуем). Контейнер до loopback не дотягивается,
    поэтому MCP поднимается своим сервером на своём порту — и на нём нет ничего, кроме
    инструментов. Публичный интерфейс генерации объявлений и ops-роуты как были на
    localhost, так и остались.

    Слушатель отвечает только docker-сети (`client_allowed`): порт-то открыт на всех
    интерфейсах, а инструменты — нет.
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    server = build_server(db, tz=tz)
    mcp_app = server.http_app(path="/", transport="streamable-http")

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Сцепка обязательна: у MCP своя задача-группа, без неё эндпоинт отвечает
        # «Task group is not initialized».
        async with mcp_app.router.lifespan_context(app):
            yield

    app = FastAPI(title="realty-mcp", lifespan=_lifespan,
                  telemetry=telemetry.fastapi_telemetry())
    app.mount(MCP_PATH, mcp_app)

    @app.middleware("http")
    async def _only_inner_network(request, call_next):
        """Отбить вызовы не из docker-сети.

        Почему отказ здесь, а не сужение привязки. Выглядит строже привязать слушатель
        к адресу docker-моста, но этот адрес Docker выделяет заново при пересоздании
        сети, и тогда слушатель просто не поднимется — **отказ привязки тихий** (MCP
        живёт отдельной задачей и бота не роняет). Отказ в обработке запроса, наоборот,
        попадает в журнал и не зависит от того, какой мост достался контейнерам.
        """
        if not client_allowed(request.client):
            peer = request.client.host if request.client else "неизвестен"
            log.warning("mcp: вызов с %s отклонён — вне внутренней сети", peer)
            return JSONResponse(status_code=403, content={"detail": "mcp: только внутренняя сеть"})
        return await call_next(request)

    # Проверка живёт на СВОЁМ пути, а не под /mcp: монтирование перехватывает весь
    # префикс, и /mcp/health ушло бы в MCP-приложение вместо этого обработчика.
    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        """Проверка, что слушатель поднят. Ею пользуется скрипт регистрации."""
        return {"status": "ok", "service": "mcp", "path": MCP_ENDPOINT}

    return app
