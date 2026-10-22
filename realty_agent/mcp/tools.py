"""Три read-only инструмента MCP и правило доступа к ним.

**Что здесь read-only и почему это важно.** Ни один инструмент не пишет: запись брони
по-прежнему делает только мост через блок `BOOKING|`, который валидируется. Агент
получает **чтение** каталога и слотов — это расширение инварианта «агент видит только
данные этого хода», и оно называется явно, а не протаскивается молча.

**Где здесь настоящий scope, а где его нет — честно.** Идентичность вызывающего приходит
с вызовом (MCP-клиент передаёт разрешённые заголовки, `get_http_headers()` их видит),
роль читается из `managers` тем же способом, что вa. Но ограничивать в этих трёх
инструментах почти нечего: адрес, цена и комнаты — **публичные** данные, их агентство
рекламирует. Единственное непубличное, что здесь есть, — **заметки менеджеров**: они
выдаются только владельцу, и именно на них написан тест. Заявлять «клиент не видит чужие
данные» шире этого нельзя: инструментов с данными клиентов (брони, телефоны) у агента нет
и не будет — они остаются в ведении моста.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware

from realty_agent.booking.filter import apply_filter
from realty_agent.booking.slots import free_slots
from realty_agent.channels.turn_context import Listing
from realty_agent.settings import settings as default_settings

log = logging.getLogger(__name__)

# Заголовок, которым шлюз передаёт идентичность вызывающего. Имя наше: платформа
# пересылает разрешённые заголовки как есть, а какой именно считать идентичностью —
# решает сервер.
CALLER_HEADER = "x-caller-id"
PLATFORM_HEADER = "x-caller-platform"
DEFAULT_PLATFORM = "tg"

# Поля объекта, которые можно отдавать кому угодно: их агентство и так рекламирует.
PUBLIC_FIELDS = ("id", "address", "price", "rooms")


#: Сказали ли уже в журнал, что удостоверения вызывающего нет (см. `get_object_details`).
#:
#: ❗ Флаг на процесс, а не на вызов: в бою удостоверения нет НИКОГДА — единственный боевой вызывающий
#: идёт через шлюз вендора, чей allowlist заголовок `x-caller-id` не пропускает (`решение 0037`). Без флага
#: строка стояла бы в журнале на каждом вызове и стала бы шумом, который перестают читать.
_caller_gap_reported = False


@dataclass(frozen=True)
class Caller:
    """Кто вызвал инструмент. `role` — из БД, а не из заголовка."""

    chat_id: str
    platform: str
    role: str | None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def _note_caller_gap(caller: Caller) -> None:
    """Назвать в журнале, что заметки не показаны и ПОЧЕМУ — один раз на процесс.

    Молчаливый пропуск неотличим от «заметок нет»: `manager_notes` просто не появляется в ответе, ошибки
    нет, а роль `None` в бою — норма. Поэтому причина одна на процесс, а не на вызов.
    """
    global _caller_gap_reported
    if _caller_gap_reported:
        return
    _caller_gap_reported = True
    reason = ("удостоверения вызывающего в запросе нет" if not caller.chat_id
              else f"роль вызывающего {caller.chat_id!r} — {caller.role!r}, не владелец")
    log.info("mcp: заметки менеджера не показаны — %s (решение 0037)", reason)


async def resolve_caller(db) -> Caller:
    """Определить вызывающего по заголовкам вызова; роль — из `managers` (a).

    Заголовка с ролью не существует намеренно: роль, пришедшая от вызывающего, была бы
    декоративной. Неизвестный вызывающий получает `role=None` — это **не отказ**:
    публичные данные доступны и анонимному читателю каталога, ограничение касается
    только непубличного (заметок менеджеров).
    """
    headers = get_http_headers() or {}
    chat_id = str(headers.get(CALLER_HEADER, "")).strip()
    platform = str(headers.get(PLATFORM_HEADER, "") or DEFAULT_PLATFORM).strip()
    if not chat_id:
        return Caller(chat_id="", platform=platform, role=None)

    role = await db.manager_role(platform=platform, chat_id=chat_id)
    return Caller(chat_id=chat_id, platform=platform, role=role)


def _summarize(arguments: dict | None) -> str:
    """Аргументы вызова — коротко и без значений, которые могут быть длинными."""
    if not arguments:
        return "без аргументов"
    return ", ".join(f"{key}={str(value)[:40]}" for key, value in sorted(arguments.items()))


class CallLogMiddleware(Middleware):
    """След каждого вызова инструмента: кто вызвал, что просил, сколько это заняло.

    Отдельным слоем, а не строкой внутри каждого инструмента, по двум причинам.
    Первая: `fastmcp` сам вызовы не журналирует, и без следа «агент действительно ходит
    в наш сервер» доказывается только косвенно — по числу соединений. Вторая: это
    единственное место, где видно, **каким вызывающим инструменты реально пользуются**,
    то есть настоящий ли у них scope или он остался надписью.

    Роль читается из БД (`resolve_caller`) — тот же источник, что в самих инструментах,
    поэтому вызывающий из журнала и вызывающий, определивший права, не могут разойтись.
    Цена — один дополнительный запрос к `managers` на вызов инструмента.
    """

    def __init__(self, db) -> None:
        self._db = db

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        arguments = context.message.arguments
        caller = await resolve_caller(_live_db(self._db))
        who = f"{caller.chat_id or 'аноним'}/роль={caller.role or 'нет'}"
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            log.warning(
                "mcp: %s(%s) — ошибка за %.0f мс: %s",
                name, who, (time.perf_counter() - started) * 1000, exc,
            )
            raise
        log.info(
            "mcp: %s(%s, %s) — за %.0f мс",
            name, who, _summarize(arguments), (time.perf_counter() - started) * 1000,
        )
        return result


def public_listing(row: dict | Listing) -> dict:
    """Только публичные поля объекта — внутренние ключи в ответ не попадают вовсе."""
    if isinstance(row, Listing):
        return {"id": row.id, "address": row.address, "price": row.price, "rooms": row.rooms}
    return {key: row.get(key) for key in PUBLIC_FIELDS if key in row}


def _live_db(source):
    """Получить живую базу: сервер собирается ДО старта приложения, а база появляется в
    lifespan. Поэтому принимаем и объект, и функцию-поставщик — иначе монтировать MCP
    пришлось бы во время старта, то есть менять маршруты уже запущенного приложения."""
    return source() if callable(source) else source


def build_server(db, *, tz=None):
    """Собрать MCP-сервер с тремя read-only инструментами.

    Сервер собирается функцией, а не модульным синглтоном: в тестах нужен экземпляр
    поверх двойника `Db`, и общее глобальное состояние этому только мешало бы.
    """
    from fastmcp import FastMCP

    tz = tz or getattr(default_settings, "tz", None)
    server = FastMCP(
        name="realty-mcp",
        instructions=(
            "Инструменты агентства недвижимости: каталог активных объектов, свободные слоты "
            "просмотра и карточка объекта. Все инструменты только читают данные."
        ),
    )

    server.add_middleware(CallLogMiddleware(db))

    @server.tool(description="Свободные слоты для просмотра. Возвращает ISO-время в таймзоне агентства.")
    async def get_available_slots(listing_id: str | None = None) -> dict:
        await resolve_caller(_live_db(db))  # identity резолвится на каждом вызове: роль видна в логах
        slots = await free_slots(_live_db(db), tz=tz, listing_id=listing_id)
        return {"slots": slots, "count": len(slots), "listing_id": listing_id}

    @server.tool(description="Найти активные объекты. Фильтр — те же условия, что клиент называет словами.")
    async def search_listings(
        rooms: int | None = None,
        price_max: int | None = None,
        price_min: int | None = None,
        area_min: int | None = None,
        keywords: list[str] | None = None,
    ) -> dict:
        caller = await resolve_caller(_live_db(db))
        spec = {
            "rooms": rooms, "price_max": price_max, "price_min": price_min,
            "area_min": area_min, "keywords": keywords or [],
        }
        rows = await _live_db(db).active_listings(limit=50)
        listings = [
            Listing(
                id=str(row.get("id", "")),
                address=str(row.get("address", "")),
                price=str(row.get("price", "")),
                rooms=str(row.get("rooms", "")),
            )
            for row in rows
        ]
        # Фильтр применяет МОСТ теми же правилами, что в диалоге: отдельной логики поиска
        # для инструмента не заводится, иначе агент и бот находили бы разное.
        matched = apply_filter(listings, spec) if any(value for value in spec.values()) else listings
        return {
            "listings": [public_listing(item) for item in matched],
            "count": len(matched),
            "caller_is_owner": caller.is_owner,
        }

    @server.tool(description="Карточка объекта по идентификатору. Заметки менеджеров видит только владелец.")
    async def get_object_details(listing_id: str) -> dict:
        caller = await resolve_caller(_live_db(db))
        rows = await _live_db(db).active_listings(limit=200)
        row = next((item for item in rows if str(item.get("id", "")) == listing_id), None)
        if row is None:
            return {"found": False, "listing_id": listing_id}

        payload: dict = {"found": True, **public_listing(row)}
        # Единственное непубличное здесь. Не владелец ключа вовсе не видит, а не получает пустым.
        if caller.is_owner:
            payload["manager_notes"] = await _live_db(db).listing_notes(address=str(row.get("address", "")))
        else:
            _note_caller_gap(caller)
        return payload

    return server
