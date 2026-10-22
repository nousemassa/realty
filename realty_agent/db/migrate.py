"""Накат схемы продукта: общий слой, затем таблицы продукта.

Запуск: `python -m realty_agent.db.migrate --components realty,vectors`
(в общем реестре имена: `core`, `realty`, `vectors`).
"""
from __future__ import annotations

from agent_core.db.migrate import cli
from agent_core.db.registry import CORE_COMPONENTS, RUNTIME_COMPONENTS, Component
from agent_core.db.schema import (
    VECTOR_DDL,
    VECTOR_SCHEMA_CHECK_EXPECTED,
    VECTOR_SCHEMA_CHECK_HINT,
    VECTOR_SCHEMA_CHECK_SQL,
)
from realty_agent.db.repo import REALTY_COMPONENTS

VECTOR_COMPONENT = Component(
    name="vectors",
    ddl=VECTOR_DDL,
    revision=2,
    check_sql=VECTOR_SCHEMA_CHECK_SQL,
    expected=VECTOR_SCHEMA_CHECK_EXPECTED,
    hint=VECTOR_SCHEMA_CHECK_HINT,
    url_key="vectors",
)

COMPONENTS = CORE_COMPONENTS + RUNTIME_COMPONENTS + REALTY_COMPONENTS + (VECTOR_COMPONENT,)


def main(argv: list[str] | None = None) -> int:
    return cli(COMPONENTS, argv)


if __name__ == "__main__":
    raise SystemExit(main())
