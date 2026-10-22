"""Чекпойнт-база графа: явный DSN и запрет тихого вывода из адреса векторов.

**Почему отдельный файл и почему первая проверка — про ОТКАЗ.** Пустой `REMINDERS_CHECKPOINT_DSN`
нельзя подменять адресом базы векторов: тогда состояние графа и корпус RAG живут в одной базе, а
«отдельное хранилище» существует только на бумаге. Подстановка тихая — процесс поднимается и работает,
и заметить её можно лишь чтением кода, поэтому тест падает, если она вернётся.

Драйвер приводится к `postgresql://` (saver работает через `psycopg`, остальные DSN проекта — с
драйвером SQLAlchemy): это второе свойство, которое легко потерять при правке, поэтому оно проверяется.
"""
from __future__ import annotations

import pytest

from realty_agent.reminders.checkpointer import checkpoint_dsn


@pytest.mark.parametrize("empty", ["", "   ", "\n", None])
def test_empty_setting_is_refused_instead_of_derived(empty):
    """Пустая настройка — отказ с названной причиной, а не адрес базы векторов."""
    with pytest.raises(ValueError) as caught:
        checkpoint_dsn(empty)

    assert "REMINDERS_CHECKPOINT_DSN" in str(caught.value), "в ошибке обязано быть имя настройки"
    assert "vectors" not in str(caught.value), \
        "подсказки про базу векторов быть не должно: именно её подстановка и запрещена"


def test_explicit_dsn_keeps_credentials_and_loses_the_driver():
    """Креды и база остаются как заданы, драйвер SQLAlchemy убирается."""
    assert checkpoint_dsn("postgresql+asyncpg://u:p@graph-db:5432/graph") == \
        "postgresql://u:p@graph-db:5432/graph"
    assert checkpoint_dsn("postgresql+psycopg://u:p@h:5432/g") == "postgresql://u:p@h:5432/g"
    assert checkpoint_dsn("  postgresql://u:p@h:5432/g  ") == "postgresql://u:p@h:5432/g", \
        "пробелы вокруг значения — не повод запускать чекпойнт в чужой базе"


def test_config_has_no_default_so_the_ban_is_visible():
    """Умолчание в настройках — пустое.

    Если бы здесь появилось «разумное» значение (адрес векторов), пункт выглядел бы выполненным, а
    состояние графа снова оказалось бы в базе корпуса. Пустое умолчание делает подмену невозможной
    молча: без явной строки в `.env` напоминания не поднимутся и скажут, чего им не хватает.
    """
    from realty_agent.settings import Settings

    assert Settings.model_fields["reminders_checkpoint_dsn"].default == ""
