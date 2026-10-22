"""Двойники ПРОДУКТА. Общие имена РЕЭКСПОРТИРУЮТСЯ: их зовут и через `fakes.<имя>`.

❗ Реэкспорт обязателен: в продукте есть сайты формы `import fakes` — они обращаются к
`fakes.FakeDb/.make_settings/.make_transport`, и без реэкспорта упадут с AttributeError.
"""
from __future__ import annotations

import datetime as dt

import pytest

from agent_core.testing import (  # noqa: F401 — реэкспорт общих двойников
    OWNER,
    SECRET,
    TEST_SESSIONS_DIR,
    FakeCallback,
    FakeMsg,
    FakeSender,
    ScriptedChannels,
    StrictChannels,
    _trace_cost,
    fake_client,
)
from agent_core.testing import FakeDb as CoreFakeDb
from realty_agent.channels.turn_context import Listing
from realty_agent.settings import Settings
from realty_agent.tg.bot import TelegramTransport

MANAGER = "222"

LISTINGS = [
    Listing(id="L-1", address="ул. Пушкина, 10, кв. 42", price="8900000", rooms="2"),
    Listing(id="L-2", address="пр. Мира, 3, кв. 15", price="6400000", rooms="1"),
]

LISTINGS_FLOORS = [
    Listing(id="L-1", address="ул. Пушкина, 10, кв. 42 (2-комн., 54 м², 7/9 эт., 5 мин до метро)",
            price="8900000", rooms="2"),
    Listing(id="L-2", address="пр. Мира, 3, кв. 15 (1-комн., 38 м², 4/12 эт., с ремонтом)",
            price="6400000", rooms="1"),
    Listing(id="L-3", address="ул. Садовая, 27, кв. 8 (3-комн., 76 м², 2/5 эт., вид на парк)",
            price="12500000", rooms="3"),
    Listing(id="L-4", address="ул. Речная, 5, кв. 3 (студия, 29 м², 14/17 эт.)",
            price="5200000", rooms="1"),
    # без данных об этаже: должен остаться в выдаче с пометкой
    Listing(id="L-5", address="ул. Лесная, 1, кв. 9 (2-комн., 44 м²)", price="7100000", rooms="2"),
]


class FakeDb(CoreFakeDb):
    """Двойник базы продукта: диалог, заявки, менеджеры, визиты и метрики."""

    def __init__(
        self,
        *,
        listings: list[Listing] | None = None,
        pending: list[dict] | None = None,
        managers: dict[str, dict] | None = None,
        bot_mode: str = "live",
        owner_flow: str | None = None,
        owner_flow_at: float | None = None,
        requests: list[dict] | None = None,
        bookings: list[dict] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.listings = LISTINGS if listings is None else listings
        self.listings_source = self.listings
        self.pending = pending or []
        self.managers = managers or {}
        self.bot_mode = bot_mode
        self.owner_flow = owner_flow
        self.owner_flow_at = owner_flow_at
        self.bookings: list = bookings or []
        self.requests: list[dict] = requests or []
        self.pending_offers: list[dict] = []
        self.listings_total: int | None = None
        self.taken_slots: set[str] = set()      # занятые времена (глобально)
        self.reminder_args: tuple[int, int] | None = None
        self.calendar_seen: set[tuple[str, object]] = set()

    async def turn_report(self, *, days: int = 7) -> dict:
        rows = getattr(self, "turn_traces_rows", [])
        return {"days": days, "turns": len(rows), "with_agent": 0, "local_closed": 0, "tokens": 0,
                "cost_usd": await self.cost_usd(None), "cost_per_turn": 0.0, "cost_per_dialogue": 0.0,
                "bookings": 0, "cost_per_booking": 0.0, "p50_ms": None, "p95_ms": None}

    async def create_manager_request(self, *, chat_id: str, username: str) -> dict | None:
        for row in self.requests:
            if row["chat_id"] == str(chat_id) and row["status"] == "pending":
                row["username"] = username
                return {"id": row["id"], "status": "pending"}
        row = {"id": len(self.requests) + 1, "chat_id": str(chat_id), "username": username,
               "status": "pending", "created_at": dt.datetime(2026, 10, 3, 21, 40, tzinfo=dt.UTC)}
        self.requests.append(row)
        return {"id": row["id"], "status": "pending"}

    async def list_manager_requests(self, limit: int = 10) -> list[dict]:
        return [row for row in self.requests if row["status"] == "pending"][:limit]

    async def pending_requests_count(self) -> int:
        return sum(1 for row in self.requests if row["status"] == "pending")

    async def decide_manager_request(self, *, request_id: int, status: str, actor: str,
                                     name: str | None = None) -> dict | None:
        for row in self.requests:
            if row["id"] == int(request_id) and row["status"] == "pending":
                row["status"] = status
                if status == "approved":
                    self.managers[row["chat_id"]] = {"name": row["username"].lstrip("@"),
                                                     "active": True, "ready": True, "role": "manager"}
                await self.log_manager_action(chat_id=row["chat_id"], role="client",
                                              action=f"manager_{'approve' if status == 'approved' else 'reject'}",
                                              actor=actor)
                return {"chat_id": row["chat_id"], "username": row["username"], "platform": "tg"}
        return None

    async def mark_request_reminded(self, request_id: int) -> None:
        return None

    async def chats_with_pending_offer(self, *, min_age_s: int, max_age_s: int, limit: int = 20) -> list[dict]:
        self.reminder_args = (min_age_s, max_age_s)
        return self.pending_offers[:limit]

    async def day_report(self, day) -> dict:
        confirmed = sum(1 for b in self.bookings if b.get("status") == "confirmed")
        return {"total": len(self.bookings), "confirmed": confirmed, "rejected": 0,
                "pending": len(self.pending), "upcoming": confirmed}

    async def active_listings(self, limit: int = 6) -> list[dict]:
        return [{"id": item.id, "address": item.address, "price": item.price, "rooms": item.rooms}
                for item in self.listings[:limit]]

    async def count_active_listings(self) -> int:
        return self.listings_total if self.listings_total is not None else len(self.listings)

    async def listing_exists(self, listing_id: str) -> bool:
        return any(item.id == listing_id for item in self.listings)

    async def busy_slots(self, candidates: list[str]) -> set[str]:
        return {slot for slot in candidates if slot in self.taken_slots}

    async def slot_taken(self, *, listing_id: str = "", slot_iso: str = "", **kwargs) -> bool:
        return slot_iso in self.taken_slots

    async def create_booking(self, **kwargs) -> dict:
        self.bookings.append(kwargs)
        return {"id": len(self.bookings)}

    async def confirmed_bookings(self, *, chat_id: str | None = None, limit: int = 10) -> list[dict]:
        rows = [b for b in self.bookings if b.get("status") == "confirmed"]
        if chat_id:
            rows = [b for b in rows if str(b.get("confirmed_by")) == str(chat_id)]
        return rows[:limit]

    async def pending_count(self) -> int:
        return len(self.pending)

    async def confirmed_count(self, *, chat_id: str | None = None) -> int:
        rows = [b for b in self.bookings if b.get("status") == "confirmed"]
        if chat_id:
            rows = [b for b in rows if str(b.get("confirmed_by")) == str(chat_id)]
        return len(rows)

    async def pending_bookings(self, limit: int = 8) -> list[dict]:
        return self.pending[:limit]

    async def set_booking_status(self, **kwargs):
        return None

    async def manager_row_exists(self, chat_id: str) -> bool:
        return str(chat_id) in self.managers

    async def is_active_manager(self, chat_id: str) -> bool:
        row = self.managers.get(str(chat_id))
        return bool(row and row.get("active"))

    async def manager_recipients(self) -> list[str]:
        return [cid for cid, row in self.managers.items() if row.get("active") and row.get("ready")]

    async def calendar_seen_keys(self) -> set[tuple[str, object]]:
        return set(self.calendar_seen)

    async def mark_calendar_seen(self, events) -> int:
        """Дедуп календаря: запоминаем (uid, начало), как таблица в pg.py."""
        rows = list(events)
        self.calendar_seen.update((item.uid, item.starts_at) for item in rows)
        return len(rows)

    async def add_manager(self, **kwargs) -> None:
        """Повторное добавление = реактивация с `ready=false` (как UPSERT в `pg.py`)."""
        self.managers[str(kwargs["chat_id"])] = {
            "name": kwargs.get("name"), "active": True, "ready": False,
            "role": kwargs.get("role", "manager"),
        }
        await self.log_manager_action(chat_id=str(kwargs["chat_id"]), role=kwargs.get("role", "manager"),
                                      action="manager_add", actor=kwargs.get("added_by"))

    async def deactivate_manager(self, **kwargs) -> None:
        row = self.managers.get(str(kwargs["chat_id"]))
        if row:
            row["active"] = False
        await self.log_manager_action(chat_id=str(kwargs["chat_id"]), role=kwargs.get("role", "manager"),
                                      action="manager_remove", actor=kwargs.get("actor"))

    async def list_managers(self, limit: int = 20) -> list[dict]:
        return [{"chat_id": cid, **row} for cid, row in self.managers.items()][:limit]

    async def set_manager_ready(self, *, chat_id: str, ready: bool = True) -> None:
        row = self.managers.get(str(chat_id))
        if row:
            row["ready"] = ready

    async def log_manager_action(self, **kwargs) -> None:
        self.actions.append((kwargs.get("chat_id"), kwargs.get("action"), kwargs.get("role")))

    async def set_owner_flow(self, *, platform: str, chat_id: str, flow: str | None) -> None:
        self.owner_flow = flow
        self.owner_flow_at = self.now if flow else None

    async def load_owner_flow(self, *, platform: str, chat_id: str):
        return self.owner_flow, self.owner_flow_at


def make_settings(**overrides) -> Settings:
    base = {
        "sessions_dir": str(TEST_SESSIONS_DIR),
        "ui_secret": SECRET,
        "bridge_api_key": "k" * 32,
        "telegram_owner_chat_id": OWNER,
        "telegram_manager_chat_id": MANAGER,
        "manager_contact": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


def queued_notices(transport) -> list[tuple[str, str]]:
    """Исходящие, ПРИНЯТЫЕ к доставке: пары (chat_id, text) из очереди.

    Уведомления и карточки не уходят прямым вызовом, а ставятся в очередь, и проверять их содержимое
    надо здесь: `FakeClient.sent` остаётся сторожем того, что прямой отправки НЕ было.
    """
    return [(row["payload"]["chat_id"], row["payload"]["text"]) for row in transport._db.outbox_rows]


def make_transport(db: FakeDb | None = None, *, settings: Settings | None = None,
                   channels=None) -> TelegramTransport:
    fake = db or FakeDb()
    transport = TelegramTransport(
        settings=settings or make_settings(), db=fake, channels=channels or StrictChannels()
    )
    # Часы флоу берём у фейка: иначе реальный `time.time()` сделал бы метку из БД «просроченной».
    transport._owner_flows._clock = lambda: getattr(fake, "now", 0.0)
    return transport


@pytest.fixture
def transport(db) -> TelegramTransport:
    t = make_transport(db)
    t._limiter._now = lambda: 1000.0        # заморозка часов лимитера: иначе кейс флакует
    return t
