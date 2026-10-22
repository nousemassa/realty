"""Корни путей для тестов: сам репозиторий и — если он есть — воркспейс вокруг него."""
import os
import pathlib

#: Корень репозитория: свои файлы (`.env`, `eval/`, `realty_agent/`) лежат здесь.
PROJECT = pathlib.Path(__file__).resolve().parents[1]


def _workspace() -> pathlib.Path:
    """Каталог НАД клоном — в воркспейсе лежит общий `.env` и стоят другие продукты.

    `AGENT_WORKSPACE` главнее догадки: рядом с одиночным клоном может оказаться чужая раскладка, и
    тогда общий `.env` нашёлся бы не там. Признак воркспейса — ОБЩИЙ `.env` в каталоге над клоном
    (тот же признак проверяют скрипты обвязки). Нет его — воркспейса нет, оба корня совпадают.
    """
    from_env = os.environ.get("AGENT_WORKSPACE")
    if from_env:
        return pathlib.Path(from_env).resolve()
    parent = PROJECT.parent
    if parent.resolve() != PROJECT and (parent / ".env").is_file():
        return parent
    return PROJECT


WORKSPACE = _workspace()
