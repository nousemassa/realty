"""Живые базы в тестах: где брать адрес и как убедиться, что это НЕ боевые данные.

**Зачем отдельным модулем.** Эти две вещи нужны и фикстурам (`conftest.py`), и самим тестам: адрес —
чтобы не писать литералом хостовый адрес базы в пяти файлах, а «сторож чистоты» — чтобы его можно было
**проверить на красное**. Проверка, которую нельзя показать падающей, ничего не гарантирует.
"""
from __future__ import annotations

import re
import urllib.parse as urlparse

from paths import PROJECT, WORKSPACE

#: Своя база продукта: адрес и креды живут в ЕГО `.env`, а не в общем. В одиночном клоне `WORKSPACE`
#: совпадает с `PROJECT`, поэтому один и тот же файл не читается дважды.
ENV_FILES = tuple(dict.fromkeys((PROJECT / ".env", WORKSPACE / ".env")))

ROOT = WORKSPACE

#: Опубликованные на 127.0.0.1 порты наших контейнеров: в `.env` хост контейнерный (`realty-db`),
#: а тесты идут с хоста. ❗ `postgres` — историческое имя ВЕНДОРСКОГО сервера; после переезда базы
#: `bridge` в наш стек эту роль играет `data-db`, и старая запись ведёт туда же: иначе забытый `.env`
#: увёл бы тесты в чужой postgres.
HOST_PORTS = {"realty-db": 55435, "news-db": 55436, "pgvector": 55433,
              "graph-db": 55434, "reminders-db": 55434}

#: Таблицы, по которым сторож проверяет «нет чужих строк». Список закрытый: это таблицы, в которые
#: пишут именно эти тесты, и по ним же видно, что база не пересоздана.
GUARDED_TABLES = ("outbox", "turn_state", "turn_traces")


def env_value(name: str) -> str:
    """Значение из файлов окружения: сначала `.env` продукта, затем общий.

    Адрес своей базы продукт держит у себя — вместе с логином и паролем этой базы.
    """
    for path in ENV_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        match = re.search(rf"^{re.escape(name)}=(.+)$", text, re.MULTILINE)
        if match:
            return match.group(1).strip().strip("\"'")
    return ""


def host_dsn(url: str, *, db_name: str) -> str:
    """Адрес базы с ХОСТА (в `.env` хост контейнерный): креды сохраняем, хост и порт — хостовые."""
    parsed = urlparse.urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    creds = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
    host = parsed.hostname or ""
    port = HOST_PORTS.get(host)
    if port is None:
        # ❗ Не «parsed.port or 5432»: тихий откат на 5432 давал ту же картину, что мёртвая база, и
        # причину искали не там (а после переезда 5432 не слушает НИКТО). Пусть падает с именем хоста.
        raise ValueError(
            f"хост базы {host!r} из .env неизвестен: опубликованные порты знаю для {sorted(HOST_PORTS)}"
        )
    prefix = f"{creds}@" if creds else ""
    return f"postgresql://{prefix}127.0.0.1:{port}/{db_name}"


def live_bridge_dsn() -> str:
    """Адрес БОЕВОЙ базы продукта с хоста — для проверок, которые обязаны её видеть (только чтение).

    Пустая строка означает «адреса нет»: вызывающий пропускает проверку, а не падает.
    """
    url = env_value("DB_URL")
    return host_dsn(url, db_name="realty") if url else ""


async def foreign_rows(dsn: str) -> dict[str, int]:
    """Сколько строк в «наших» таблицах. Ненулевое число означает чужие данные (тест не изолирован)."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        # Имена таблиц — из константы модуля, не из ввода: подстановка безопасна.
        return {table: await conn.fetchval(f"SELECT count(*) FROM {table}") for table in GUARDED_TABLES}
    finally:
        await conn.close()


async def assert_no_foreign_rows(dsn: str, *, where: str = "тестовая база") -> dict[str, int]:
    """Сторож чистоты: в базе не должно быть чужих строк — иначе проверки идут по чужим данным.

    Краснеет на боевой базе по построению (там строки есть), и это проверяется отдельным тестом:
    сторож, который не может покраснеть, опаснее отсутствующего.
    """
    counts = await foreign_rows(dsn)
    assert all(count == 0 for count in counts.values()), (
        f"{where}: ЧУЖИЕ строки {counts} — база не пересоздана или указывает на боевую"
    )
    return counts
