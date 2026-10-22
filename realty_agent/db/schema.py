"""Схема продукта: объекты, просмотры, менеджеры, календарь и аудит действий.

Общая база у продуктов одна, но владелец таблиц — продукт: здесь только его таблицы и его проверка
наката. Общие таблицы (состояние хода, трассы, очередь, настройки) объявлены в `agent_core`.

Порядок важен: общий слой накатывается первым, потому что `conversations` создаётся там, а здесь
дополняется колонками.
"""
from __future__ import annotations

SCHEMA_CHECK_SQL = """
select
    (select count(*) from information_schema.columns
       where table_schema='public' and table_name='conversations'
         and column_name in ('owner_flow','owner_flow_at'))
  + (select count(*) from information_schema.columns
       where table_schema='public' and table_name='managers' and column_name='ready')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='manager_requests')
  + (select count(*) from pg_indexes
       where schemaname='public' and indexname in ('uq_manager_request_pending','ix_mreq_pending'))
  + (select count(*) from pg_indexes
       where schemaname='public' and indexname = 'uq_slot_global')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='calendar_events_seen')
  + (select count(*) from information_schema.columns
       where table_schema='public' and table_name='bookings' and column_name='visit_result')
"""

SCHEMA_CHECK_EXPECTED = 9

SCHEMA_CHECK_HINT = (
    "owner_flow (2 колонки), managers.ready, manager_requests (+2 индекса), uq_slot_global, "
    "calendar_events_seen, bookings.visit_result"
)

DDL = """
CREATE TABLE IF NOT EXISTS listings (
    id           text PRIMARY KEY,
    external_id  text UNIQUE,
    address      text NOT NULL,
    price        bigint,
    rooms        int,
    status       text NOT NULL DEFAULT 'active',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bookings (
    id           bigserial PRIMARY KEY,
    listing_id   text REFERENCES listings(id) ON DELETE SET NULL,
    slot         timestamptz NOT NULL,
    client_name  text NOT NULL,
    client_phone text NOT NULL,
    platform     text NOT NULL,
    chat_id      text NOT NULL,
    status       text NOT NULL DEFAULT 'pending',
    raw_block    text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    confirmed_by text,
    rejected_by  text
);

-- заявки на роль менеджера (самозаявка)
CREATE TABLE IF NOT EXISTS manager_requests (
    id          bigserial PRIMARY KEY,
    platform    text NOT NULL DEFAULT 'tg',
    chat_id     text NOT NULL,                     -- user_id заявителя (ключ прав)
    username    text NOT NULL,                     -- @ник из ПРОФИЛЯ Telegram
    status      text NOT NULL DEFAULT 'pending',   -- pending|approved|rejected
    created_at  timestamptz NOT NULL DEFAULT now(),
    reminded_at timestamptz,
    decided_by  text,
    decided_at  timestamptz
);
-- одна ожидающая заявка на чат (второй INSERT конфликтует: ON CONFLICT ... WHERE status='pending')
CREATE UNIQUE INDEX IF NOT EXISTS uq_manager_request_pending
    ON manager_requests (platform, chat_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_mreq_pending ON manager_requests (created_at) WHERE status = 'pending';

-- частичный unique: только активные брони держат слот
CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_active ON bookings (listing_id, slot)
    WHERE status IN ('pending','confirmed');
CREATE INDEX IF NOT EXISTS ix_book_pending ON bookings (status, created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_book_client  ON bookings (platform, chat_id);
-- отчёт владельца читает брони по дате создания (иначе Seq Scan)
CREATE INDEX IF NOT EXISTS ix_book_created ON bookings (created_at);
-- уникальность времени ГЛОБАЛЬНАЯ — одно время = один просмотр (на любом объекте)
CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_global ON bookings (slot)
    WHERE status IN ('pending', 'confirmed');
-- «Просмотры» ищет подтверждённые по тому, кто подтвердил
CREATE INDEX IF NOT EXISTS ix_book_confirmed_by ON bookings (confirmed_by) WHERE status = 'confirmed';
-- Признак явки. Три состояния, а не булево: `NULL` — ещё не отмечено, `attended` — пришёл,
-- `no_show` — не пришёл. Булево не отличило бы «не отмечено» от «не пришёл», а метрике «доля
-- дошедших до показа» нужны и числитель, и знаменатель. Отдельного поля «когда отмечено» нет
-- намеренно: кто и когда — уже в `manager_actions` (аудит отметки).
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS visit_result text;

CREATE TABLE IF NOT EXISTS managers (
    platform    text NOT NULL,
    chat_id     text NOT NULL,
    name        text,
    role        text NOT NULL DEFAULT 'manager',   -- owner|manager
    active      boolean NOT NULL DEFAULT true,
    ready       boolean NOT NULL DEFAULT false,  -- дал /start ⇒ карточки доходят
    added_by    text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, chat_id)
);
CREATE INDEX IF NOT EXISTS ix_managers_active ON managers (active) WHERE active;
-- ALTER, а не только CREATE: на ЖИВОЙ базе `CREATE TABLE IF NOT EXISTS` колонок не добавляет.
ALTER TABLE managers ADD COLUMN IF NOT EXISTS ready boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS booking_notifications (
    booking_id       bigint NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    manager_chat_id  text NOT NULL,
    message_id       bigint,
    PRIMARY KEY (booking_id, manager_chat_id)
);

CREATE TABLE IF NOT EXISTS listing_jobs (
    id          bigserial PRIMARY KEY,
    session_id  text,
    fields      jsonb NOT NULL,
    result      text,
    duration_ms int,
    cost_usd    numeric(10,4),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Аудит действий с менеджерами и просмотра ПД.
CREATE TABLE IF NOT EXISTS manager_actions (
    id                bigserial PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    manager_platform  text NOT NULL,
    manager_chat_id   text NOT NULL,
    role              text,
    action            text NOT NULL,
    booking_id        bigint,
    from_status       text,
    to_status         text,
    result            text,
    meta              jsonb
);
-- Календарь: какие вхождения уже отправлены. Ключ (uid, начало) — правило повторения даёт много
-- вхождений у одного uid, повторов быть не должно.
CREATE TABLE IF NOT EXISTS calendar_events_seen (
    uid         text NOT NULL,
    starts_at   timestamptz NOT NULL,
    summary     text NOT NULL DEFAULT '',
    notified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, starts_at)
);
CREATE INDEX IF NOT EXISTS ix_calendar_starts ON calendar_events_seen (starts_at);

-- Овнер-флоу меню: колонки в общей `conversations` (таблица создаётся общим слоем).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS owner_flow text;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS owner_flow_at timestamptz;
"""

DEFAULT_SETTINGS = {
    "bookings_open": True,
    "schedule": {"tz": "Europe/Moscow", "dow": [1, 2, 3, 4, 5, 6],
                 "start": "10:00", "end": "19:00", "horizon_days": 7},
}
