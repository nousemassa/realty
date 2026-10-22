"""Доступ к данным продукта: объекты, слоты, брони, менеджеры, визиты, календарь.

Наследует общий `Db` и добавляет только СВОИ операции: общий слой не знает ни одной таблицы продукта.
Схема — в `realty_agent/db/schema.py`, накат — в `realty_agent/db/migrate.py`.
"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import asyncpg

from agent_core.db.migrate import Outcome, apply_all
from agent_core.db.registry import CORE_COMPONENTS, RUNTIME_COMPONENTS, Component
from agent_core.db.store import Db, parse_slot
from realty_agent.db import schema
from realty_agent.ui.format import nick_bare
from realty_agent.vocab import VISIT_RESULTS, CalendarEvent

REVISION = 2

REALTY_COMPONENT = Component(
    name="realty",
    ddl=schema.DDL,
    revision=REVISION,
    check_sql=schema.SCHEMA_CHECK_SQL,
    expected=schema.SCHEMA_CHECK_EXPECTED,
    settings=schema.DEFAULT_SETTINGS,
    hint=schema.SCHEMA_CHECK_HINT,
    #: Прежнее имя компонента, которое писал старый образ: переносится накатом и зеркалится для него.
    legacy_names=("product",),
)

REALTY_COMPONENTS = (REALTY_COMPONENT,)

#: Таблицы, без которых MCP-листенер не поднимется: схему создаёт мост, листенер только проверяет.
MCP_TABLES = ("bookings", "listing_jobs", "listings", "managers", "settings")


class RealtyDb(Db):
    """Общий `Db` плюс операции продукта. По умолчанию накатывает и требует оба компонента."""

    def __init__(self, *args, components: tuple[Component, ...] | None = None, **kwargs) -> None:
        super().__init__(
            *args, components=components or (CORE_COMPONENTS + RUNTIME_COMPONENTS + REALTY_COMPONENTS),
            **kwargs)

    async def revoke_consent(self, *, platform: str, chat_id: str) -> None:
        """Отзыв согласия: анонимизация броней за последние 30 дней, затем общая чистка."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE bookings SET client_name='', client_phone='', chat_id=''
                   WHERE platform=$1 AND chat_id=$2 AND created_at > now() - interval '30 days'""",
                platform, chat_id)
        await super().revoke_consent(platform=platform, chat_id=chat_id)

    async def jobs_today(self) -> int:
        return await self.pool.fetchval("SELECT count(*) FROM listing_jobs WHERE created_at::date = now()::date")

    async def insert_listing_job(
        self, *, session_id: str, fields: dict[str, Any], result: str, duration_ms: int, cost_usd: float | None
    ) -> int:
        return await self.pool.fetchval(
            """INSERT INTO listing_jobs (session_id, fields, result, duration_ms, cost_usd)
               VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING id""",
            session_id,
            json.dumps(fields, ensure_ascii=False),
            result,
            duration_ms,
            cost_usd,
        )

    async def active_listings(self, limit: int = 6) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT id, address, price, rooms FROM listings WHERE status='active' ORDER BY created_at LIMIT $1""",
            limit)
        return [dict(r) for r in rows]

    async def upsert_listing(self, *, listing_id: str, address: str, price: str, rooms: str) -> None:
        await self.pool.execute(
            """INSERT INTO listings (id, external_id, address, price, rooms) VALUES ($1,$1,$2,NULLIF($3,'')::bigint,$4)
               ON CONFLICT (id) DO UPDATE SET address=$2, price=NULLIF($3,'')::bigint, rooms=$4""",
            listing_id, address, price, rooms)

    async def count_active_listings(self) -> int:
        """Сколько активных объектов ВСЕГО (для приписки «Показаны первые 6 из N», DD-4)."""
        return int(await self.pool.fetchval(
            "SELECT count(*) FROM listings WHERE status='active'") or 0)

    async def listing_exists(self, listing_id: str) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM listings WHERE id=$1 AND status='active'", listing_id))

    async def busy_slots(self, candidates: list[str]) -> set[str]:
        """Занятые времена среди кандидатов — ОДНИМ запросом ().

        Уникальность ГЛОБАЛЬНАЯ: время, занятое на любом объекте, не предлагается нигде.
        `= ANY(...)`, не `BETWEEN`: в окне есть пропуск выходного дня.
        """
        if not candidates:
            return set()
        rows = await self.pool.fetch(
            """SELECT DISTINCT slot FROM bookings
               WHERE status IN ('pending','confirmed') AND slot = ANY($1::timestamptz[])""",
            [parse_slot(item, tz=self._tz) for item in candidates])
        return {row["slot"].astimezone(self._tz).strftime("%Y-%m-%dT%H:%M") for row in rows}

    async def slot_taken(self, *, listing_id: str, slot_iso: str) -> bool:
        return bool(await self.pool.fetchval(
            """SELECT 1 FROM bookings WHERE listing_id=$1 AND slot=$2::timestamptz
               AND status IN ('pending','confirmed')""", listing_id, parse_slot(slot_iso, tz=self._tz)))

    async def create_booking(self, *, listing_id: str, slot_iso: str, name: str, phone: str,
                             platform: str, chat_id: str, raw_block: str) -> dict:
        """Создаёт pending-бронь; на занятый слот бросает ValueError (partial unique index)."""
        try:
            row = await self.pool.fetchrow(
                """INSERT INTO bookings (listing_id, slot, client_name, client_phone, platform, chat_id, raw_block)
                   VALUES ($1,$2::timestamptz,$3,$4,$5,$6,$7)
                   RETURNING id, listing_id, slot, client_name, client_phone, status""",
                listing_id, parse_slot(slot_iso, tz=self._tz), name, phone, platform, chat_id, raw_block)
        except asyncpg.UniqueViolationError as error:
            raise ValueError("slot_taken") from error
        return dict(row)

    async def set_booking_status(
        self, *, booking_id: int, status: str, manager_chat_id: str, role: str = "manager"
    ) -> dict | None:
        column = "confirmed_by" if status == "confirmed" else "rejected_by"
        row = await self.pool.fetchrow(
            f"""UPDATE bookings SET status=$2, {column}=$3
                WHERE id=$1 AND status='pending'
                RETURNING id, listing_id, slot, client_name, client_phone, platform, chat_id, status""",
            booking_id, status, manager_chat_id)
        if row:
            # UPDATE ... WHERE status='pending' гарантирует исходное состояние — фиксируем его в аудите
            await self.pool.execute(
                """INSERT INTO manager_actions
                       (manager_platform, manager_chat_id, role, action, booking_id, from_status, to_status)
                   VALUES ('tg',$1,$4,$2,$3,'pending',$2)""",
                manager_chat_id, status, booking_id, role)
        return dict(row) if row else None

    async def confirmed_bookings(self, *, chat_id: str | None = None, limit: int = 10) -> list[dict]:
        """Подтверждённые заявки: менеджеру — свои (confirmed_by), владельцу — все (DD-6).

        `visit_result` отдаётся вместе со строкой: по нему список показывает, отмечена
        ли явка, а на прошедших визитах без отметки ставятся кнопки отметки.
        """
        if chat_id:
            rows = await self.pool.fetch(
                """SELECT id, listing_id, slot, client_name, client_phone, confirmed_by, visit_result
                   FROM bookings
                   WHERE status='confirmed' AND confirmed_by=$1 ORDER BY slot LIMIT $2""", chat_id, limit)
        else:
            rows = await self.pool.fetch(
                """SELECT id, listing_id, slot, client_name, client_phone, confirmed_by, visit_result
                   FROM bookings
                   WHERE status='confirmed' ORDER BY slot LIMIT $1""", limit)
        return [dict(row) for row in rows]

    async def set_visit_result(
        self, *, booking_id: int, result: str, manager_chat_id: str, role: str = "manager"
    ) -> dict | None:
        """Отметить явку: `attended` («пришёл») или `no_show` («не пришёл»).

        Отметить можно только **подтверждённую** бронь — на `pending` отметка не имеет
        смысла, а на `rejected` визита не было вовсе. Повторная отметка **не ошибка**, а
        исправление: менеджер вправе переставить («обознался»), поэтому значение
        перезаписывается, а не отвергается.

        Аудит — в `manager_actions` (`action='visit_mark'`, `result` = отметка): там
        видно и кто, и когда. Поэтому отдельного поля «когда отмечено» в `bookings` нет —
        второй журнал тех же сведений разошёлся бы с первым.
        """
        if result not in VISIT_RESULTS:
            raise ValueError(f"неизвестная отметка явки: {result!r}")
        row = await self.pool.fetchrow(
            """UPDATE bookings SET visit_result=$2
                WHERE id=$1 AND status='confirmed'
                RETURNING id, listing_id, slot, status, visit_result""",
            booking_id, result)
        if row:
            await self.pool.execute(
                """INSERT INTO manager_actions
                       (manager_platform, manager_chat_id, role, action, booking_id, from_status, to_status, result)
                   VALUES ('tg',$1,$2,'visit_mark',$3,'confirmed','confirmed',$4)""",
                manager_chat_id, role, booking_id, result)
        return dict(row) if row else None

    async def booking_for_visit(self, *, booking_id: int) -> dict | None:
        """Бронь глазами потока напоминаний: что нужно для текста и проверки состояния.

        Граф проверяет по этому ответу, что бронь **всё ещё подтверждена**, перед каждой
        отправкой: иначе клиент получил бы напоминание о визите, который уже отменён.
        """
        row = await self.pool.fetchrow(
            """SELECT b.id, b.listing_id, b.slot, b.platform, b.chat_id, b.status, b.visit_result,
                      b.confirmed_by, COALESCE(l.address, '') AS address
                 FROM bookings b LEFT JOIN listings l ON l.id = b.listing_id
                WHERE b.id = $1""",
            booking_id)
        return dict(row) if row else None

    async def visit_candidates(
        self, *, ahead_h: float, back_h: float = 0.0, limit: int = 50
    ) -> list[dict]:
        """Подтверждённые визиты в окне `[now - back_h; now + ahead_h]`, явка не отмечена.

        Один метод на два разных скана планировщика — иначе получились бы два похожих
        запроса, которые разъедутся:
          * **старт потоков** — `back_h=0`, окно `ahead_h = N` часов: кому пора напомнить;
          * **проверка сроков ожидания** — окно шире, с запасом в прошлое: у потока,
            который ждёт ответа, визит уже мог пройти, и его надо не бросить, а закрыть.

        `limit` нужен, чтобы сбой в базе не превратился в тысячи одновременных потоков.
        """
        rows = await self.pool.fetch(
            """SELECT b.id, b.slot, b.platform, b.chat_id, b.confirmed_by, COALESCE(l.address, '') AS address
                 FROM bookings b LEFT JOIN listings l ON l.id = b.listing_id
                WHERE b.status='confirmed' AND b.visit_result IS NULL
                  AND b.slot > now() - ($1)::interval
                  AND b.slot <= now() + ($2)::interval
                ORDER BY b.slot LIMIT $3""",
            # ВАЖНО: параметр — `timedelta`, а не строка «168 hours». Прежняя редакция передавала
            # строку, и asyncpg отвергал её (`DataError: str object has no attribute 'days'`). Дефект
            # жил незамеченным, потому что этот запрос выполняет ТОЛЬКО планировщик графа напоминаний,
            # а механизм до был выключен — и тесты шли на двойнике базы, который строку
            # принимал. Нашлось это первым же живым тиком после включения.
            dt.timedelta(hours=float(back_h)), dt.timedelta(hours=float(ahead_h)), limit)
        return [dict(row) for row in rows]

    async def visit_buckets(self, *, since_days: int = 90) -> list[dict]:
        """Прошедшие подтверждённые визиты по (менеджер, дата слота) с исходами явки.

        Группу («с напоминанием» / «без») здесь **не считаем**: она выводится чистой
        функцией `realty_agent/booking/visits.py:reminder_group` из этих же двух полей, поэтому
        пересчитывается в любой момент и не хранится в базе.

        `unmarked` возвращается отдельным числом намеренно: неотмеченный визит нельзя
        молча записывать ни в «пришёл», ни в «не пришёл» — иначе знаменатель доли
        окажется придуманным.
        """
        rows = await self.pool.fetch(
            """SELECT to_char(b.slot AT TIME ZONE $1, 'YYYY-MM-DD') AS slot_date,
                      COALESCE(b.confirmed_by, '') AS manager_chat_id,
                      count(*) AS confirmed,
                      count(*) FILTER (WHERE b.visit_result = 'attended') AS attended,
                      count(*) FILTER (WHERE b.visit_result = 'no_show')  AS no_show,
                      count(*) FILTER (WHERE b.visit_result IS NULL)      AS unmarked
                 FROM bookings b
                WHERE b.status='confirmed' AND b.slot < now()
                  AND b.slot >= now() - ($2)::interval
                GROUP BY 1, 2 ORDER BY 1, 2""",
            str(self._tz), f"{since_days} days")
        return [dict(row) for row in rows]

    async def pending_count(self) -> int:
        """Истинная длина очереди: `pending_bookings(limit=…)` для счётчика не годится — лимит врёт."""
        return int(await self.pool.fetchval(
            "SELECT count(*) FROM bookings WHERE status='pending'"))

    async def confirmed_count(self, *, chat_id: str | None = None) -> int:
        """Сколько заявок подтверждено (менеджеру — своих, владельцу — всех).

        Счётчик в кнопке нельзя считать через `confirmed_bookings(limit=…)` — лимит обрезает выборку
        и цифра «залипает» на 0/1 ().
        """
        if chat_id:
            return int(await self.pool.fetchval(
                "SELECT count(*) FROM bookings WHERE status='confirmed' AND confirmed_by=$1", chat_id))
        return int(await self.pool.fetchval(
            "SELECT count(*) FROM bookings WHERE status='confirmed'"))

    async def pending_bookings(self, limit: int = 8) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT id, listing_id, slot, client_name, client_phone FROM bookings
               WHERE status='pending' ORDER BY created_at LIMIT $1""", limit)
        return [dict(r) for r in rows]

    async def is_active_manager(self, chat_id: str) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM managers WHERE platform='tg' AND chat_id=$1 AND active", chat_id))

    async def listing_notes(self, *, address: str) -> list[str]:
        """Заметки менеджеров по объекту — непубличные данные.

        Заметки вводятся при генерации объявления и лежат в `listing_jobs.fields`, а не в
        карточке объекта: связи по `listing_id` в схеме нет, поэтому ищем по адресу. Это
        ограничение названо, а не спрятано — при появлении постоянной таблицы заметок
        запрос меняется здесь же.
        """
        rows = await self.pool.fetch(
            """SELECT DISTINCT fields->>'notes' AS note
                 FROM listing_jobs
                WHERE fields->>'address' = $1
                  AND coalesce(fields->>'notes', '') <> ''""",
            address,
        )
        return [row["note"] for row in rows if row["note"]]

    async def manager_role(self, *, chat_id: str, platform: str = "tg") -> str | None:
        """Роль заявителя для ACL: `owner|manager`, или `None`, если он неизвестен/неактивен.

        Источник роли — БД, а не заголовок запроса (a). Неактивная строка (`active=false`)
        роли не даёт: снятие прав действует сразу, как и в менеджерском меню.
        """
        return await self.pool.fetchval(
            "SELECT role FROM managers WHERE platform=$1 AND chat_id=$2 AND active",
            platform, chat_id)

    async def add_manager(self, *, chat_id: str, name: str, added_by: str, role: str = "manager") -> None:
        """Повторное добавление = реактивация: `active=true`, но `ready=false` (пусть заново даст /start)."""
        await self.pool.execute(
            """INSERT INTO managers (platform, chat_id, name, role, active, added_by, ready)
               VALUES ('tg',$1,$2,$3,true,$4,false)
               ON CONFLICT (platform, chat_id) DO UPDATE
               SET name=$2, role=$3, active=true, added_by=$4, ready=false""",
            chat_id, nick_bare(name), role, added_by)
        await self.log_manager_action(chat_id=chat_id, role=role, action="manager_add", actor=added_by)

    async def deactivate_manager(self, *, chat_id: str, actor: str, role: str = "manager") -> None:
        """Мягкое удаление: аудит и история сохраняются, права снимаются сразу."""
        await self.pool.execute(
            "UPDATE managers SET active=false WHERE platform='tg' AND chat_id=$1", chat_id)
        await self.log_manager_action(chat_id=chat_id, role=role, action="manager_remove", actor=actor)

    async def list_managers(self, limit: int = 20) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT chat_id, name, role, active, ready FROM managers
               WHERE platform='tg' ORDER BY active DESC, created_at LIMIT $1""", limit)
        return [dict(row) for row in rows]

    async def manager_recipients(self) -> list[str]:
        """Адресаты карточек: только активные И давшие /start (иначе отправка падала бы вечно)."""
        rows = await self.pool.fetch(
            "SELECT chat_id FROM managers WHERE platform='tg' AND active AND ready")
        return [str(row["chat_id"]) for row in rows]

    async def set_manager_ready(self, *, chat_id: str, ready: bool = True) -> None:
        await self.pool.execute(
            "UPDATE managers SET ready=$2 WHERE platform='tg' AND chat_id=$1", chat_id, ready)

    async def calendar_seen_keys(self) -> set[tuple[str, datetime]]:
        """Уже отправленные вхождения — (uid, начало). Дедуп держим в БД, а не в памяти процесса."""
        rows = await self.pool.fetch("SELECT uid, starts_at FROM calendar_events_seen")
        return {(str(row["uid"]), row["starts_at"]) for row in rows}

    async def mark_calendar_seen(self, events: Iterable[CalendarEvent]) -> int:
        """Пометить отправленные и подчистить старое (ретенция 30 дней — таблица не растёт бесконечно)."""
        rows = [(item.uid, item.starts_at, item.summary) for item in events]
        if rows:
            await self.pool.executemany(
                """INSERT INTO calendar_events_seen (uid, starts_at, summary) VALUES ($1, $2, $3)
                   ON CONFLICT (uid, starts_at) DO NOTHING""",
                rows,
            )
        await self.pool.execute("DELETE FROM calendar_events_seen WHERE starts_at < now() - interval '30 days'")
        return len(rows)

    async def log_manager_action(
        self, *, chat_id: str, role: str, action: str, actor: str | None = None, result: str | None = None,
    ) -> None:
        """Аудит действий с менеджерами/просмотра ПД (`manager_add|manager_remove|view_pending`)."""
        await self.pool.execute(
            """INSERT INTO manager_actions
                   (manager_platform, manager_chat_id, role, action, result, meta)
               VALUES ('tg',$1,$2,$3,$4,$5::jsonb)""",
            chat_id, role, action, result, json.dumps({"actor": actor}) if actor else None)

    async def set_owner_flow(self, *, platform: str, chat_id: str, flow: str | None) -> None:
        """Отдельные колонки, а не `continuum`: тот перезаписывается целиком при каждом ходе диалога."""
        await self.pool.execute(
            """INSERT INTO conversations (platform, chat_id, session_id, step, owner_flow, owner_flow_at)
               VALUES ($1,$2,$1||'-'||$2,'listing',$3,now())
               ON CONFLICT (platform, chat_id) DO UPDATE
               SET owner_flow=$3, owner_flow_at=now()""",
            platform, chat_id, flow)

    async def manager_row_exists(self, chat_id: str) -> bool:
        """Есть ли строка менеджера (в т.ч. снятая) — нужно, чтобы legacy-чат не обходил `active` (M4)."""
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM managers WHERE platform='tg' AND chat_id=$1", chat_id))

    async def load_owner_flow(self, *, platform: str, chat_id: str) -> tuple[str | None, float | None]:
        row = await self.pool.fetchrow(
            """SELECT owner_flow, extract(epoch from owner_flow_at) AS at
               FROM conversations WHERE platform=$1 AND chat_id=$2""",
            platform, chat_id)
        if not row:
            return None, None
        return row["owner_flow"], (float(row["at"]) if row["at"] is not None else None)

    async def create_manager_request(self, *, chat_id: str, username: str) -> dict | None:
        """UPSERT ожидающей заявки. Предикат в ON CONFLICT обязателен: индекса два (partial)."""
        row = await self.pool.fetchrow(
            """INSERT INTO manager_requests (platform, chat_id, username)
               VALUES ('tg',$1,$2)
               ON CONFLICT (platform, chat_id) WHERE status='pending'
               DO UPDATE SET username = EXCLUDED.username
               RETURNING id, status""", chat_id, username)
        return dict(row) if row else None

    async def list_manager_requests(self, limit: int = 10) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT id, chat_id, username, created_at FROM manager_requests
               WHERE platform='tg' AND status='pending' ORDER BY created_at LIMIT $1""", limit)
        return [dict(row) for row in rows]

    async def pending_requests_count(self) -> int:
        return int(await self.pool.fetchval(
            "SELECT count(*) FROM manager_requests WHERE platform='tg' AND status='pending'") or 0)

    async def decide_manager_request(self, *, request_id: int, status: str, actor: str,
                                     name: str | None = None) -> dict | None:
        """CAS по статусу; при утверждении — строка менеджера (active+ready) и аудит в одной транзакции."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """UPDATE manager_requests SET status=$2, decided_by=$3, decided_at=now()
                   WHERE id=$1 AND status='pending'
                   RETURNING id, chat_id, username, platform""", request_id, status, actor)
            if row is None:
                return None
            if status == "approved":
                await conn.execute(
                    """INSERT INTO managers (platform, chat_id, name, role, active, added_by, ready)
                       VALUES ('tg',$1,$2,'manager',true,$3,true)
                       ON CONFLICT (platform, chat_id) DO UPDATE
                       SET name=$2, role='manager', active=true, added_by=$3, ready=true""",
                    row["chat_id"], nick_bare(name or row["username"]), actor)
            await conn.execute(
                """INSERT INTO manager_actions (manager_platform, manager_chat_id, role, action, result, meta)
                   VALUES ('tg',$1,'client',$2,$3,$4::jsonb)""",
                row["chat_id"], "manager_approve" if status == "approved" else "manager_reject",
                status, json.dumps({"actor": actor, "request_id": request_id}))
            return dict(row)

    async def mark_request_reminded(self, request_id: int) -> None:
        await self.pool.execute(
            "UPDATE manager_requests SET reminded_at=now() WHERE id=$1", request_id)

    async def purge_decided_requests(self, *, days: int = 30) -> int:
        """Ретенция (H6): решённые заявки старше N дней удаляем вместе с остальными ПД."""
        return int(await self.pool.execute(
            """DELETE FROM manager_requests WHERE status <> 'pending'
               AND decided_at < now() - ($1 || ' days')::interval""", str(days)) or 0)

    async def chats_with_pending_offer(self, *, min_age_s: int, max_age_s: int,
                                       limit: int = 20) -> list[dict]:
        """Чаты, у которых есть предложение слотов старше min_age и моложе max_age, без напоминания."""
        rows = await self.pool.fetch(
            """SELECT chat_id, step, continuum FROM conversations
               WHERE step IN ('slot','name','phone','confirm')
                 AND (continuum->>'offered_at') IS NOT NULL
                 AND COALESCE((continuum->>'offer_reminded_at')::double precision, 0) = 0
                 AND now() - to_timestamp((continuum->>'offered_at')::double precision)
                     BETWEEN ($1 || ' seconds')::interval AND ($2 || ' seconds')::interval
               ORDER BY updated_at LIMIT $3""", str(min_age_s), str(max_age_s), limit)
        return [dict(row) for row in rows]

    async def day_report(self, day: date) -> dict:
        """Числа отчёта за день в бизнес-таймзоне (+ предстоящие подтверждённые). ПД нет."""
        row = await self.pool.fetchrow(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE status='confirmed') AS confirmed,
                      count(*) FILTER (WHERE status='rejected')  AS rejected,
                      count(*) FILTER (WHERE status='pending')   AS pending
               FROM bookings WHERE (created_at AT TIME ZONE $2)::date = $1""", day, str(self._tz))
        upcoming = await self.pool.fetchval(
            "SELECT count(*) FROM bookings WHERE status='confirmed' AND slot >= now()")
        return {**dict(row or {}), "upcoming": int(upcoming or 0)}

    async def turn_report(self, *, days: int = 7) -> dict:
        """Агрегаты трасс за окно: ходы, доля без облака, токены, деньги и латентность.

        Знаменатель «$/диалог» — ВСЕ клиентские ходы, включая локально закрытые (решение владельца):
        иначе цена сопровождения клиента считалась бы только по ходам с облаком и была бы завышена.
        Поэтому рядом всегда печатается, сколько ходов прошло без агента.
        """
        row = await self.pool.fetchrow(
            """SELECT count(*)                                            AS turns,
                      count(*) FILTER (WHERE agent_called)                AS with_agent,
                      count(*) FILTER (WHERE NOT agent_called)            AS local_closed,
                      coalesce(sum(tokens_total), 0)                      AS tokens,
                      coalesce(sum(cost_usd), 0)                          AS cost_usd,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                        FILTER (WHERE agent_called)                       AS p50_ms,
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                        FILTER (WHERE agent_called)                       AS p95_ms
               FROM turn_traces WHERE day >= (current_date - $1::int)""", days)
        data = dict(row or {})
        bookings = await self.pool.fetchval(
            "SELECT count(*) FROM bookings WHERE created_at >= now() - ($1::int || ' days')::interval", days)
        turns = int(data.get("turns") or 0)
        cost = float(data.get("cost_usd") or 0)
        return {
            "days": days,
            "turns": turns,
            "with_agent": int(data.get("with_agent") or 0),
            "local_closed": int(data.get("local_closed") or 0),
            "tokens": int(data.get("tokens") or 0),
            "cost_usd": cost,
            "cost_per_turn": (cost / turns) if turns else 0.0,
            "cost_per_dialogue": (cost / turns) if turns else 0.0,
            "bookings": int(bookings or 0),
            "cost_per_booking": (cost / int(bookings)) if bookings else 0.0,
            "p50_ms": int(data["p50_ms"]) if data.get("p50_ms") else None,
            "p95_ms": int(data["p95_ms"]) if data.get("p95_ms") else None,
        }


async def migrate(*, db_url: str, vector_url: str = "") -> list[Outcome]:
    """Накатить схему продукта (общий слой идёт первым: он создаёт общие таблицы)."""
    from realty_agent.db.migrate import COMPONENTS

    return await apply_all(COMPONENTS, db_url=db_url, vector_url=vector_url)
