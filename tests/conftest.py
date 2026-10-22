"""Pytest-фикстуры. Сами фейки — в `tests/fakes.py` (их импортируют и тесты).

**`bridge_test_dsn` — своя база для тестов, которые проверяют SQL.**

Инвариант: тест SQL видит ТОЛЬКО свои строки и пересоздаёт ТОЛЬКО свои таблицы. В общей базе он
забирает чужую строку `new` (она самая старая) и пересоздаёт чекпойнт-таблицы чужого контура — адрес
чекпойнтов по умолчанию смотрит на `…/vectors`, а не на тестовую базу.

**Как устроено.** Тестовая база создаётся на том же сервере, что боевая (значит DDL и драйвер ведут себя
одинаково), называется `realty_test` и **пересоздаётся на каждый прогон**: `DROP … WITH (FORCE)` +
`CREATE`. Схему накатывает ТОТ ЖЕ код, что в бою — `Db(dsn, apply_ddl=True)`: копии DDL в тестах нет и
появиться не может. В конце сессии база удаляется.

**Сторож пригодности проверки** — `empty_test_db`: он утверждает, что в тестовой базе нет чужих строк.
Без него «тест зелёный» ничего не доказывал бы: если фикстуру однажды направят на боевую базу, проверки
вида «строка стала `sent`» пойдут по чужим данным — ровно то, что уже случилось. Что сторож **умеет
краснеть**, проверяет отдельный тест (`test_outbox.py::test_guard_reddens_on_a_busy_database`).
"""

from __future__ import annotations

import asyncio
import os

import pytest

#: Адреса сервисов моделей объявляет АГЕНТ: тестам они тоже нужны, и подставляются в ОКРУЖЕНИЕ до
#: импорта настроек — иначе `Settings()` (он собирается при импорте модуля) откажет на пустом адресе.
#: Умолчаний в коде нет намеренно: пустой адрес в бою должен быть виден, а не подменён «удобным».
for _name, _value in {
    "LLAMA_EMBED_URL": "http://embed.test:8082/v1",
    "LLAMA_EMBED_MODEL": "test-embed",
    "SOLVER_URL": "http://solver.test:8083",
}.items():
    os.environ.setdefault(_name, _value)
from fakes import (
    db,  # noqa: F401 — фикстура продукта
    transport,  # noqa: F401 — фикстура ПРОДУКТА (TelegramTransport); pytest подхватывает её отсюда
)
from live_db import assert_no_foreign_rows, env_value, host_dsn

from agent_core.testing import built_client  # noqa: F401 — фикстуры ОБЩЕГО слоя

TEST_DB_NAME = "realty_test"


async def _recreate_database(admin_dsn: str, *, name: str) -> None:
    """Пересоздать тестовую базу: без этого тесты зависят от того, что осталось от прошлого прогона."""
    import asyncpg

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_database(admin_dsn: str, *, name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _apply_schema(dsn: str) -> None:
    """Схему создаёт ТОТ ЖЕ код, что в бою (`apply_ddl=True`) — один источник DDL, никакого дубля."""
    from realty_agent.db.repo import RealtyDb

    repo = RealtyDb(dsn, apply_ddl=True)
    await repo.connect()
    await repo.close()


@pytest.fixture(scope="session")
def bridge_test_dsn() -> str:
    """DSN тестовой базы: пересоздаётся на прогон, схема — как у боевой.

    Синхронная фикстура намеренно: сессионные асинхронные фикстуры требуют своего loop scope, а здесь
    достаточно `asyncio.run` — сам тест живёт в своей петле.
    """
    pytest.importorskip("asyncpg")
    url = env_value("DB_URL")
    if not url:
        pytest.skip("нет DB_URL в .env продукта — живые тесты не запускаются")
    server = host_dsn(url, db_name="postgres")   # служебная база того же сервера
    try:
        asyncio.run(_recreate_database(server, name=TEST_DB_NAME))
    except Exception as error:  # noqa: BLE001 — недоступный сервер это пропуск, а не отказ (как и раньше)
        message = f"сервер базы недоступен ({type(error).__name__}: {error}) — проверьте docker ps"
        # ❗ `make test-live` зовётся ИМЕННО чтобы проверить живой путь: там пропуск был бы ложной
        # зеленью (rc=0 без единой проверки). В `make ci` пропуск остаётся законным — гейт не обязан
        # требовать поднятую базу.
        if os.environ.get("AGENT_LIVE_STRICT"):
            pytest.fail(message)
        pytest.skip(message)

    dsn = host_dsn(url, db_name=TEST_DB_NAME)
    asyncio.run(_apply_schema(dsn))
    yield dsn
    try:
        asyncio.run(_drop_database(server, name=TEST_DB_NAME))
    except Exception as error:  # noqa: BLE001 — ошибка удаления тестовой базы не влияет на исход прогона
        print(f"[conftest] тестовая база не удалена: {type(error).__name__}: {error}")


@pytest.fixture
async def empty_test_db(bridge_test_dsn: str) -> str:
    """Тестовая база пуста — иначе проверки пошли бы по чужим строкам (см. докстроку модуля)."""
    await assert_no_foreign_rows(bridge_test_dsn, where="тестовая база")
    yield bridge_test_dsn
