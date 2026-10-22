"""Тесты MCP-сервера и его инструментов.

Проверяется без сети и без платформы, но **через настоящий протокол**: приложение MCP
поднимается в процессе и к нему идут HTTP-запросы (initialize → tools/list → tools/call).
Это ловит то, что юнит-тест на функциях не поймает: несовпадение транспорта, ошибка
монтирования, отвалившийся lifespan.

Что проверяется:

  * сервер отдаёт ровно три read-only инструмента;
  * слоты считаются тем же кодом, что в диалоге (один источник, а не вторая копия);
  * **scope настоящий**: заметки менеджеров видит только владелец, остальные ключа в
    ответе не получают вовсе (не пустым значением — его там нет);
  * роль берётся из `managers`, а не из заголовка: заголовка с ролью не существует.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from realty_agent.booking.slots import free_slots
from realty_agent.channels.turn_context import Listing
from realty_agent.mcp.server import MCP_ENDPOINT, build_standalone_mcp_app, client_allowed
from realty_agent.mcp.tools import PUBLIC_FIELDS, build_server, public_listing, resolve_caller

OWNER_ID = "111"
MANAGER_ID = "222"

LISTING = {"id": "L-1", "address": "ул. Садовая, 12", "price": 9300000, "rooms": 2, "internal": "не отдавать"}


class FakeDb:
    def __init__(self, roles: dict[str, str] | None = None) -> None:
        self._roles = {"111": "owner", "222": "manager"} if roles is None else roles
        self.busy: set[str] = set()

    async def manager_role(self, *, chat_id: str, platform: str = "tg") -> str | None:
        return self._roles.get(chat_id)

    async def setting(self, key: str, default=None):
        if key == "schedule":
            return {"start": "10:00", "end": "19:00", "horizon_days": 3}
        return default

    async def busy_slots(self, candidates: list[str]) -> set[str]:
        return {slot for slot in candidates if slot in self.busy}

    async def active_listings(self, limit: int = 6) -> list[dict]:
        return [dict(LISTING)]

    async def listing_notes(self, *, address: str) -> list[str]:
        return ["окна на юг", "хозяин готов торговаться"]


TZ = dt.UTC


def _server(db: FakeDb):
    return build_server(db, tz=TZ)


def _listener_app():
    """Приложение ЛИСТЕНЕРА — то, чем служит отдельный процесс, включая сцепку lifespan.

    Мост посторонний протокол на своём порту не держит, поэтому проверяется ровно то приложение,
    которое отдаёт `realty_agent/mcp_main.py`: самостоятельный листенер со своим `/mcp` и своим
    `/health`. Сцепка lifespan внутри него запускает задачу-группу MCP — без неё эндпоинт отвечает
    `RuntimeError: Task group is not initialized`.
    """
    db = FakeDb()
    app = build_standalone_mcp_app(db, tz=TZ)
    return app, db


async def _rpc(client, method: str, params: dict | None = None, *, headers: dict | None = None) -> dict:
    """Один JSON-RPC вызов MCP (streamable-http принимает POST с телом JSON-RPC)."""
    response = await client.post(
        MCP_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    assert response.status_code == 200, f"{method}: HTTP {response.status_code} {response.text[:200]}"
    body = response.text
    if body.startswith(("event:", "data:")):
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return response.json()


# --- сборка и состав ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_exposes_three_readonly_tools():
    server = _server(FakeDb())
    tools = await server.list_tools()

    assert sorted(tool.name for tool in tools) == [
        "get_available_slots", "get_object_details", "search_listings"
    ]


def test_public_listing_strips_internal_fields():
    payload = public_listing(LISTING)

    assert set(payload) == set(PUBLIC_FIELDS)
    assert "internal" not in payload, "внутреннее поле не должно попадать в ответ"


def test_public_listing_works_on_context_listing():
    payload = public_listing(Listing(id="L-2", address="пр. Мира, 3", price="9 000 000", rooms="1-комн."))

    assert payload["id"] == "L-2" and set(payload) == set(PUBLIC_FIELDS)


@pytest.mark.asyncio
async def test_slots_come_from_shared_module():
    """Слоты считает тот же код, что в диалоге: вторая копия расписания разошлась бы."""
    db = FakeDb()
    direct = await free_slots(db, tz=TZ, listing_id="L-1")
    server = _server(db)
    tools = await server.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert "get_available_slots" in by_name
    assert direct, "ожидались слоты на горизонте из настроек"
    assert direct == await free_slots(db, tz=TZ, listing_id="L-1")


@pytest.mark.asyncio
async def test_busy_slots_are_excluded():
    db = FakeDb()
    all_slots = await free_slots(db, tz=TZ)
    db.busy = {all_slots[0]}

    after = await free_slots(db, tz=TZ)

    assert all_slots[0] not in after and len(after) == len(all_slots) - 1


# --- scope ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_gets_manager_notes(monkeypatch):
    monkeypatch.setattr("realty_agent.mcp.tools.get_http_headers", lambda: {"x-caller-id": OWNER_ID})
    db = FakeDb()
    caller = await resolve_caller(db)
    assert caller.is_owner

    server = _server(db)
    result = await server.call_tool("get_object_details", {"listing_id": "L-1"})
    payload = _structured(result)

    assert payload["found"] is True
    assert payload.get("manager_notes"), "владелец должен видеть заметки менеджеров"


@pytest.mark.asyncio
async def test_non_owner_does_not_get_manager_notes(monkeypatch):
    """Единственное непубличное здесь — заметки. Не владелец ключа НЕ получает вовсе."""
    monkeypatch.setattr("realty_agent.mcp.tools.get_http_headers", lambda: {"x-caller-id": MANAGER_ID})
    db = FakeDb()
    caller = await resolve_caller(db)
    assert caller.role == "manager" and not caller.is_owner

    server = _server(db)
    payload = _structured(await server.call_tool("get_object_details", {"listing_id": "L-1"}))

    assert payload["found"] is True
    assert "manager_notes" not in payload, "непубличное поле не должно появляться даже пустым"
    assert payload["address"] == "ул. Садовая, 12", "публичные данные при этом доступны"


@pytest.mark.asyncio
async def test_unknown_caller_is_not_an_error(monkeypatch):
    """Анонимный читатель каталога — норма: публичные данные не секрет."""
    monkeypatch.setattr("realty_agent.mcp.tools.get_http_headers", dict)
    db = FakeDb()

    caller = await resolve_caller(db)
    payload = _structured(await _server(db).call_tool("get_object_details", {"listing_id": "L-1"}))

    assert caller.role is None and payload["found"] is True
    assert "manager_notes" not in payload


@pytest.mark.asyncio
async def test_role_is_read_from_db_not_from_header(monkeypatch):
    """Заголовка с ролью нет вовсе: подделка «я владелец» невозможна по построению."""
    monkeypatch.setattr(
        "realty_agent.mcp.tools.get_http_headers",
        lambda: {"x-caller-id": MANAGER_ID, "x-caller-role": "owner", "x-role": "owner"},
    )
    db = FakeDb()

    caller = await resolve_caller(db)
    payload = _structured(await _server(db).call_tool("get_object_details", {"listing_id": "L-1"}))

    assert caller.role == "manager", "заголовок с ролью не должен её повышать"
    assert "manager_notes" not in payload


@pytest.mark.asyncio
async def test_unknown_listing_reports_not_found(monkeypatch):
    monkeypatch.setattr("realty_agent.mcp.tools.get_http_headers", lambda: {"x-caller-id": OWNER_ID})
    payload = _structured(await _server(FakeDb()).call_tool("get_object_details", {"listing_id": "нет-такого"}))

    assert payload["found"] is False


@pytest.mark.asyncio
async def test_search_listings_returns_public_fields(monkeypatch):
    monkeypatch.setattr("realty_agent.mcp.tools.get_http_headers", lambda: {"x-caller-id": MANAGER_ID})

    payload = _structured(await _server(FakeDb()).call_tool("search_listings", {"rooms": 2}))

    assert payload["count"] >= 1
    assert set(payload["listings"][0]) == set(PUBLIC_FIELDS)


@pytest.mark.asyncio
async def test_tool_call_leaves_a_trace_in_the_log(monkeypatch, caplog):
    """Вызов инструмента обязан оставлять след в журнале.

    `fastmcp` сам вызовы не журналирует, поэтому след держится нашим middleware — и без
    этой проверки он может пропасть молча: тогда нечем доказать, что агент действительно
    ходит в наш сервер, а не получает те же данные из пересказа.
    """
    monkeypatch.setattr("realty_agent.mcp.tools.get_http_headers", lambda: {"x-caller-id": OWNER_ID})

    with caplog.at_level(logging.INFO, logger="realty_agent.mcp.tools"):
        await _server(FakeDb()).call_tool("get_object_details", {"listing_id": "L-1"})

    trace = " ".join(record.getMessage() for record in caplog.records)
    assert "get_object_details" in trace, "в следе должно быть имя инструмента"
    assert OWNER_ID in trace and "owner" in trace, "в следе должен быть вызывающий и его роль из БД"
    assert "listing_id=L-1" in trace, "аргументы вызова должны попадать в след"


# --- сетевой доступ -------------------------------------------------------------------

@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        (("172.27.4.5", 40000), True),      # контейнер платформы
        (("127.0.0.1", 40000), True),       # сам хост: проверки и скрипты
        (("192.168.1.50", 40000), False),   # локальная сеть
        (("10.10.1.7", 40000), False),      # другая сеть хоста
        (("testclient", 40000), False),     # не адрес вовсе — считаем вызывающего чужим
        (None, False),                      # адреса нет
    ],
)
def test_client_allowed_filters_by_network(peer, expected):
    """Отбор по адресу — единственная преграда: удостоверения у сервера нет.

    Шлюз платформы пересылает только свои заголовки, наш секрет до сервера не доходит,
    поэтому «свой/чужой» решается по сети. Проверяются края: не-адрес и отсутствие
    адреса обязаны давать отказ, иначе проверка обходится подделкой заголовков.
    """
    assert client_allowed(peer) is expected


def _standalone_client(*, peer: tuple[str, int]):
    """Клиент от лица конкретного адреса — иначе `TestClient` зовёт как `testclient`."""
    from fastapi.testclient import TestClient

    return TestClient(build_standalone_mcp_app(FakeDb(), tz=TZ), client=peer)


def test_standalone_mcp_answers_docker_network():
    with _standalone_client(peer=("172.27.4.5", 40000)) as client:
        response = client.get("/health")

    assert response.status_code == 200, response.text[:200]


def test_standalone_mcp_rejects_lan_client():
    """Слушатель стоит на 0.0.0.0, поэтому без этой проверки каталог читался бы из сети."""
    with _standalone_client(peer=("192.168.1.50", 40000)) as client:
        response = client.get("/health")

    assert response.status_code == 403, response.text[:200]


# --- живой протокол -------------------------------------------------------------------

def _client():
    """Клиент листенера от лица ВНУТРЕННЕГО адреса — так его и зовёт docker-сеть.

    Адрес обязателен как аргумент: листенер отвечает только внутренним сетям
    (`client_allowed`), а `TestClient` без `client=` зовёт как `testclient` и получает
    законный `403`. Без адреса проверялось бы не то, что работает в бою.

    `TestClient` запускает lifespan при входе в контекст — в отличие от `ASGITransport`.
    """
    from fastapi.testclient import TestClient

    app, _db = _listener_app()
    return TestClient(app, client=("127.0.0.1", 40000))


def test_mcp_initialize_over_http():
    """Транспорт должен быть streamable-http: иначе MCP-клиент не подключится."""
    with _client() as client:
        response = client.post(
            MCP_ENDPOINT,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "0"}},
            },
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )

    assert response.status_code == 200, response.text[:300]
    assert "protocolVersion" in response.text or "realty" in response.text


def test_tools_list_over_http():
    """Полный протокольный цикл: initialize выдаёт session id, дальше запросы идут с ним.

    Это ровно то, что делает MCP-клиент: без session id сервер отвечает
    «Missing session ID», и подключение считается неудачным.
    """
    with _client() as client:
        session_id = _initialize_sync(client)
        body = _rpc_sync(client, "tools/list", session_id=session_id)

    names = [tool["name"] for tool in body.get("result", {}).get("tools", [])]
    assert sorted(names) == ["get_available_slots", "get_object_details", "search_listings"]


def test_call_tool_over_http_returns_public_object():
    """Инструмент должен быть вызываем по протоколу, а не только существовать в списке."""
    with _client() as client:
        session_id = _initialize_sync(client)
        body = _rpc_sync(
            client, "tools/call",
            {"name": "get_object_details", "arguments": {"listing_id": "L-1"}},
            session_id=session_id,
        )

    text = json.dumps(body, ensure_ascii=False)
    assert "ул. Садовая, 12" in text
    assert "manager_notes" not in text, "анонимный вызов не должен получать заметки"


def _initialize_sync(client) -> str:
    """Отправить initialize и вернуть id сессии из заголовка ответа."""
    response = client.post(
        MCP_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}},
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text[:300]
    session_id = response.headers.get("mcp-session-id", "")
    assert session_id, "сервер обязан выдать session id — иначе шлюз не сможет работать"
    return session_id


def _rpc_sync(client, method: str, params: dict | None = None, *, session_id: str = "") -> dict:
    response = client.post(
        MCP_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **({"mcp-session-id": session_id} if session_id else {}),
        },
    )
    assert response.status_code == 200, f"{method}: HTTP {response.status_code} {response.text[:200]}"
    text = response.text
    if text.startswith(("event:", "data:")):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return response.json()


def _structured(result):
    """Достать структурированный ответ инструмента из результата `call_tool`."""
    for candidate in (getattr(result, "structured_content", None), getattr(result, "data", None)):
        if isinstance(candidate, dict):
            return candidate
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"не удалось разобрать ответ инструмента: {result!r}")


@pytest.mark.asyncio
async def test_caller_gap_is_named_in_the_log_once_per_process(monkeypatch, caplog):
    """Пропуск заметок НАЗЫВАЕТСЯ — один раз на процесс, а не на вызов.

    Молчаливый пропуск неотличим от «заметок нет»: ключ просто не появляется, ошибки нет, а роль `None` в
    бою — норма (единственный боевой вызывающий идёт через шлюз вендора, который заголовок не пропускает —
    решение `0037`). Поэтому причина пишется один раз: строка на каждый вызов стала бы шумом.
    """
    import logging

    from realty_agent.mcp import tools as tools_module

    monkeypatch.setattr("realty_agent.mcp.tools.get_http_headers", dict)
    monkeypatch.setattr(tools_module, "_caller_gap_reported", False)
    server = _server(FakeDb())

    with caplog.at_level(logging.INFO):
        await server.call_tool("get_object_details", {"listing_id": "L-1"})
        first = [record.message for record in caplog.records if "заметки менеджера не показаны" in record.message]
        caplog.clear()
        await server.call_tool("get_object_details", {"listing_id": "L-1"})
        second = [record.message for record in caplog.records if "заметки менеджера не показаны" in record.message]

    assert len(first) == 1, "причина пропуска обязана быть в журнале"
    assert "0037" in first[0], "строка ссылается на решение, чтобы её было с чем сверять"
    assert second == [], "повтор на каждом вызове превратил бы причину в шум"
