"""MCP-листенер отдельным процессом — без сети и без запуска служб.

**Что здесь проверяется.** Два свойства, которые нельзя увидеть глазами в коде и которые ломаются
тихо:

1. **У моста нет второго маунта.** Смонтированный в мост MCP (`/mcp` на порту бота) отвечал бы `307`,
   и «забыть снять маунт» — самая вероятная ошибка при выносе: код листенера при этом продолжает
   работать, а посторонний протокол остаётся на порту бота. Проверяется по таблице маршрутов, а не
   по обещанию.
2. **Набор таблиц листенера существует в схеме.** Листенер работает с `apply_ddl=False` (схему
   создаёт мост) и падает на старте, если таблицы нет. Набор вычислен по коду инструментов, и
   разойтись с DDL он может только незаметно — ровно поэтому проверка машинная.
"""
from __future__ import annotations

import pytest


def test_bridge_does_not_serve_mcp_on_its_own_port():
    """Посторонний протокол не живёт на порту бота: у моста нет ни одного маршрута под `/mcp`.

    Проверка «после выноса»: если маунт вернуть, тест покраснеет. Раньше маршрут был
    (`app.mount(MCP_PATH, mcp_app)`), и по нему же отвечал `127.0.0.1:8085/mcp`.
    """
    pytest.importorskip("fastapi")
    from realty_agent.bridge_main import app

    paths = [getattr(route, "path", "") for route in app.routes]
    offenders = [path for path in paths if path == "/mcp" or path.startswith("/mcp/")]
    assert not offenders, (
        f"мост снова отдаёт MCP на своём порту: {offenders}. Листенер обязан быть отдельным "
        f"процессом (`realty/bridge-up-mcp.sh`, юнит `bridge-mcp`)"
    )


def test_listener_tables_exist_in_the_schema():
    """Каждая таблица из `MCP_TABLES` объявлена в DDL: иначе листенер не поднимется на живой базе.

    `apply_ddl=False` + `expected_tables` — это проверка «схему применил мост». Набор здесь не
    «примерный список», а следствие кода инструментов: `managers` (роль вызывающего, `E13a`),
    `listings` (каталог), `listing_jobs` (заметки менеджеров), `settings` (расписание слотов),
    `bookings` (занятость слотов).
    """
    from agent_core.db import schema_core, schema_runtime
    from realty_agent.db import schema
    from realty_agent.db.repo import MCP_TABLES

    DDL = schema_core.DDL + schema_runtime.DDL + schema.DDL

    assert MCP_TABLES, "набор таблиц листенера не может быть пустым"
    missing = [table for table in MCP_TABLES if f"CREATE TABLE IF NOT EXISTS {table}" not in DDL]
    assert not missing, (
        f"таблицы листенера не найдены в DDL: {missing}. Если инструмент начал читать новую "
        f"таблицу, её надо добавить и в `MCP_TABLES`, и в DDL — иначе листенер упадёт на старте"
    )
