#!/usr/bin/env bash
# Хостовые DSN для процессов, запускаемых НЕ в контейнере.
#
# **Зачем отдельным файлом.** `.env` хранит адреса ВНУТРИ docker-сети (`data-db:5432`, `pgvector:5432`,
# `reminders-db:5432`), а хостовый процесс таких имён не разрешает. Перекрытие на опубликованные порты
# нужно трём базам и трём процессам, поэтому живёт в одном месте: копия разойдётся, и отказ будет тихим —
# процесс поднимется и просто скажет, что «механизм не поднят».
#
# Подключение:  source "$PROJECT_DIR/lib/dsn.sh"

#: Опубликованные на 127.0.0.1 порты наших контейнеров — ОДНА карта на все функции этого файла.
#: ❗ Порт берётся по имени хоста ИЗ DSN, а не литералом: «единый источник» не должен врать первым.
#: Неизвестный хост — ОТКАЗ с причиной: тихое «5432» уже давало молчаливый отказ подключения.
_dsn_ports() {
  cat <<'PORTS'
data-db:55435
pgvector:55433
reminders-db:55434
PORTS
}

#: Перевести адрес базы из контейнерного в хостовый: (`<файлы окружения>…`), печатает DSN.
#: Файлы читаются ПО ПОРЯДКУ: побеждает последний, где ключ определён.
_dsn_host() {
  local key="${1:?ключ}"; shift
  python3 - "$key" "$@" <<'DSN_PY'
import os, sys, urllib.parse as u

key = sys.argv[1]
raw = {}
for path in sys.argv[2:] or [".env"]:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    raw[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        continue

url = raw.get(key, "")
if not url:
    raise SystemExit(f"[dsn] нет {key} в файлах окружения — адрес задаётся ЯВНО")
p = u.urlsplit(url)
cred = p.netloc.rsplit("@", 1)[0] + "@" if "@" in p.netloc else ""
db = p.path.lstrip("/") or "postgres"
ports = {"data-db": 55435, "pgvector": 55433, "reminders-db": 55434}
override = os.environ.get("BRIDGE_DB_PORT_HOST") or os.environ.get("GRAPH_CHECKPOINT_PORT_HOST")
port = override or ports.get(p.hostname or "")
if port is None:
    raise SystemExit(f"[dsn] хост базы {p.hostname!r} из {key} неизвестен: "
                     f"опубликованные порты знаю для {sorted(ports)} (BRIDGE_DB_PORT_HOST — переопределение)")
print(f"postgresql+asyncpg://{cred}127.0.0.1:{port}/{db}")
DSN_PY
}

#: База продукта: ключ `DB_URL` (прежнее имя — `BRIDGE_DB_URL`).
dsn() {
  python3 - "$@" <<'DSN_PY'
import os, sys, urllib.parse as u

raw = {}
for path in sys.argv[1:] or [".env"]:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    raw[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        continue

url = raw.get("DB_URL") or raw.get("BRIDGE_DB_URL")
if not url:
    raise SystemExit("[dsn] нет DB_URL ни в одном из файлов окружения — база продукта не задана")
p = u.urlsplit(url)
cred = p.netloc.rsplit("@", 1)[0] + "@" if "@" in p.netloc else ""
db = p.path.lstrip("/")
if not db:
    raise SystemExit(f"[dsn] в DB_URL ({p.hostname}) не названа база — имя базы задаётся адресом")
ports = {"data-db": 55435, "pgvector": 55433, "reminders-db": 55434}
port = os.environ.get("BRIDGE_DB_PORT_HOST") or ports.get(p.hostname or "")
if port is None:
    raise SystemExit(f"[dsn] хост базы {p.hostname!r} из DB_URL неизвестен: "
                     f"опубликованные порты знаю для {sorted(ports)} (BRIDGE_DB_PORT_HOST — переопределение)")
print(f"postgresql+asyncpg://{cred}127.0.0.1:{port}/{db}")
DSN_PY
}

#: Векторная база RAG (ключ `VECTOR_DB_URL`).
vector_dsn() { _dsn_host VECTOR_DB_URL "$@"; }

#: Чекпойнт-база графа напоминаний (ключ `REMINDERS_CHECKPOINT_DSN`).
reminders_dsn() { _dsn_host REMINDERS_CHECKPOINT_DSN "$@"; }
