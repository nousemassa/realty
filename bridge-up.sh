#!/usr/bin/env bash
# Подъём моста (хостовый процесс uvicorn на 127.0.0.1:8085).
#
# Почему скрипт, а не длинная команда: в `.env` адреса ВНУТРИСЕТЕВЫЕ (`data-db:5432`, `api:8000`) —
# они верны для контейнерного пути (`compose.realty.yaml`), но с хоста не резолвятся. Для хостового
# запуска они перекрываются на `127.0.0.1` (базы опубликованы на 55433–55435, api — на 8680).
#
# То же и для RAG: векторная база опубликована на 127.0.0.1:55433, а сервисы локального инференса живут
# ПРЯМО НА ХОСТЕ — для хостового процесса это `127.0.0.1:8082`, тогда как в контейнерном пути адрес
# `host.docker.internal:8082` (там за это отвечает extra_hosts).
# Лог обязателен: без него причина смерти процесса не восстановима.
#
#   bash bridge-up.sh          # поднять в фоне (setsid+nohup, лог, pid-файл)
#   bash bridge-up.sh run      # запустить в ПЕРЕДНЕМ плане (для systemd-юнита)
#   bash bridge-up.sh status   # только проверка
#   bash bridge-up.sh stop     # остановить
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#: ❗ Своя библиотека подключается ЗДЕСЬ: она вычисляет корни, а `set -u` обрывает запуск на первой же
#: ссылке на неопределённую переменную. Хостовый лончер — путь ОТКАТА, и он обязан запускаться.
source "$PROJECT_DIR/lib/workspace.sh"   #: PROJECT_DIR и WORKSPACE (с учётом `AGENT_WORKSPACE`)
source "$PROJECT_DIR/lib/vendor.sh"      #: режим платформы — один вход
source "$PROJECT_DIR/lib/dsn.sh"         #: хостовые DSN — один вход
ENV_FILE="${REALTY_ENV:-$PROJECT_DIR/.env}"        #: окружение ПРОЕКТА
BASE_ENV="${REALTY_BASE_ENV:-$WORKSPACE/.env}"     #: общая основа (может отсутствовать)
#: Интерпретатор: своё окружение проекта, иначе окружение воркспейса, иначе системный.
PYTHON="${REALTY_PY:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in "$PROJECT_DIR/.venv/bin/python" "$WORKSPACE/.venv/bin/python"; do
    [[ -x "$candidate" ]] && { PYTHON="$candidate"; break; }
  done
fi
PYTHON="${PYTHON:-python3}"
LOGDIR="$PROJECT_DIR/logs"
#: ❗ Файл агента для своего рантайма (`AGENT_RUNTIME=own`): на хосте он лежит в этом репозитории, в
#: контейнере путь задаёт композ (`/srv/realty/onboarding/…`). Путь передаём ЯВНО: угадывание по
#: текущему каталогу даёт молчащий бот (каталог сессий указывал внутрь site-packages).
export DIALOG_PROMPT_PATH="${DIALOG_PROMPT_PATH:-$PROJECT_DIR/onboarding/realtor-bot.md}"
#: Тот же шов для объявлений: путь к файлу агента роли `listing` — на хосте из воркспейса.
export LISTING_PROMPT_PATH="${LISTING_PROMPT_PATH:-$PROJECT_DIR/onboarding/listing-writer.md}"
LOG="$LOGDIR/bridge.log"
PIDFILE="$PROJECT_DIR/bridge.pid"
HEALTH="http://127.0.0.1:8085/health"
ACTION="${1:-up}"

health() { curl -fsS -m 3 "$HEALTH" 2>/dev/null; }

#: Хостовые адреса переводятся ЯВНО: в `.env` они внутри docker-сети (`data-db:5432`, `pgvector:5432`,
#: `reminders-db:5432`), а хостовый процесс таких имён не разрешает. Нерезолвящийся адрес не роняет
#: запуск — половина RAG отключается молча, поэтому перекрываются ВСЕ адреса, а не только адрес базы.

#: Режим читаем из того файла, где он лежит: общая основа, а если её нет — окружение проекта.
#: Без этого режим читался бы из другого файла, чем тот, с которым работает служба.
require_platform_mode "мост (хостовый запуск)" "${REALTY_BASE_ENV:-$BASE_ENV}"

case "$ACTION" in
  status)
    if out="$(health)"; then echo "[bridge] работает: $out"; else echo "[bridge] НЕ отвечает на $HEALTH"; fi
    ;;
  stop)
    pids="$(pgrep -f 'uvicorn realty_agent.bridge_main:app' || true)"
    if [[ -z "$pids" ]]; then echo "[bridge] процесс не найден"; exit 0; fi
    kill $pids 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "[bridge] остановлен (pid: $pids)"
    ;;
  run)
    # Передний план: systemd сам следит за процессом (Restart=always) и пишет лог.
    mkdir -p "$LOGDIR" "$PROJECT_DIR/.sessions"
    exec env PYTHONPATH="$PROJECT_DIR" \
      DB_URL="$(dsn "$BASE_ENV" "$ENV_FILE")" \
      VECTOR_DB_URL="$(vector_dsn "$BASE_ENV" "$ENV_FILE")" \
      LLAMA_EMBED_URL="${LLAMA_EMBED_URL_HOST:-http://127.0.0.1:8082/v1}" \
      SOLVER_URL="${SOLVER_URL_HOST:-http://127.0.0.1:8083}" \
    SESSIONS_DIR="${SESSIONS_DIR_HOST:-$PROJECT_DIR/.sessions}" \
        AGENT_ENV_FILES="$BASE_ENV:$ENV_FILE" \
      PLATFORM_API_URL="${PLATFORM_API_URL_HOST:-http://127.0.0.1:8680/api}" \
      "$PYTHON" -m uvicorn realty_agent.bridge_main:app --host 127.0.0.1 --port 8085
    ;;
  up|*)
    if out="$(health)"; then echo "[bridge] уже поднят: $out"; exit 0; fi
    mkdir -p "$LOGDIR" "$PROJECT_DIR/.sessions"
    setsid nohup env PYTHONPATH="$PROJECT_DIR" \
      DB_URL="$(dsn "$BASE_ENV" "$ENV_FILE")" \
      VECTOR_DB_URL="$(vector_dsn "$BASE_ENV" "$ENV_FILE")" \
      LLAMA_EMBED_URL="${LLAMA_EMBED_URL_HOST:-http://127.0.0.1:8082/v1}" \
      SOLVER_URL="${SOLVER_URL_HOST:-http://127.0.0.1:8083}" \
    SESSIONS_DIR="${SESSIONS_DIR_HOST:-$PROJECT_DIR/.sessions}" \
        AGENT_ENV_FILES="$BASE_ENV:$ENV_FILE" \
      PLATFORM_API_URL="${PLATFORM_API_URL_HOST:-http://127.0.0.1:8680/api}" \
      "$PYTHON" -m uvicorn realty_agent.bridge_main:app --host 127.0.0.1 --port 8085 \
      >> "$LOG" 2>&1 < /dev/null &
    pgrep -f 'uvicorn realty_agent.bridge_main:app' > "$PIDFILE" 2>/dev/null || true
    for i in $(seq 1 20); do
      if out="$(health)"; then echo "[bridge] поднят (попытка $i): $out"; exit 0; fi
      sleep 2
    done
    echo "[bridge] НЕ поднялся за 40 с — хвост $LOG:"
    tail -n 25 "$LOG" || true
    exit 1
    ;;
esac
