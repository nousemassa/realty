#!/usr/bin/env bash
# Подъём ПРОЦЕССА ГРАФА НАПОМИНАНИЙ: хостовый процесс uvicorn на 127.0.0.1:${BRIDGE_GRAPH_PORT:-8089}.
#
# Почему отдельный скрипт, а не флаг у основного: в `bridge-up.sh` жёстко зашиты порт 8085, точка входа
# `realty_agent.bridge_main:app`, pid-файл и health-URL основного моста — переиспользовать нельзя.
# Лог обязателен: без него причина смерти процесса не восстановима.
#
# Почему процесс отдельный, а не корутина моста: у напоминаний своя жизнь (потоки живут днями), свой пул
# чекпойнтера и свой темп опроса. Внутри моста рестарт напоминаний означал рестарт бота, а падение бота
# уносило незавершённые потоки. Слушаем ТОЛЬКО
# loopback: продолжение потока дёргает мост на этом же хосте, наружу ручка не нужна.
#
#   bash bridge-up-mcp.sh          # поднять в фоне (setsid+nohup, лог, pid-файл)
#   bash bridge-up-mcp.sh run      # запустить в ПЕРЕДНЕМ плане (для systemd-юнита)
#   bash bridge-up-mcp.sh status   # только проверка
#   bash bridge-up-mcp.sh stop     # остановить
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
LOG="$LOGDIR/reminders.log"
PIDFILE="$PROJECT_DIR/reminders.pid"
# Хостовые адреса БД (общий модуль: у трёх процессов одна и та же ловушка с docker-именами).
PORT="${BRIDGE_GRAPH_PORT:-8089}"
HOST="127.0.0.1"
# Проверка живости — по loopback: слушатель отвечает и на нём, а ходить на 0.0.0.0 незачем.
HEALTH="http://127.0.0.1:${PORT}/health"
ENTRY="realty_agent.reminders_main:app"
PROC="uvicorn ${ENTRY}"
ACTION="${1:-up}"

health() { curl -fsS -m 3 "$HEALTH" 2>/dev/null; }

#: Хостовые адреса переводятся ЯВНО: в `.env` они внутри docker-сети (`data-db:5432`, `pgvector:5432`,
#: `reminders-db:5432`), а хостовый процесс таких имён не разрешает. Нерезолвящийся адрес не роняет
#: запуск — половина RAG отключается молча, поэтому перекрываются ВСЕ адреса, а не только адрес базы.

#: Режим читаем из того файла, где он лежит: общая основа, а если её нет — окружение проекта.
#: Без этого режим читался бы из другого файла, чем тот, с которым работает служба.
require_platform_mode "граф напоминаний (хостовый запуск)" "${REALTY_BASE_ENV:-$BASE_ENV}"

case "$ACTION" in
  status)
    if out="$(health)"; then echo "[graph] работает: $out"; else echo "[graph] НЕ отвечает на $HEALTH"; fi
    ;;
  stop)
    pids="$(pgrep -f "$PROC" || true)"
    if [[ -z "$pids" ]]; then echo "[graph] процесс не найден"; exit 0; fi
    kill $pids 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "[graph] остановлен (pid: $pids)"
    ;;
  run)
    # Передний план: systemd сам следит за процессом (Restart=always) и пишет лог.
    mkdir -p "$LOGDIR"
    exec env PYTHONPATH="$PROJECT_DIR" \
      DB_URL="$(dsn "$BASE_ENV" "$ENV_FILE")" \
      VECTOR_DB_URL="$(vector_dsn "$BASE_ENV" "$ENV_FILE")" \
      REMINDERS_CHECKPOINT_DSN="$(reminders_dsn "$BASE_ENV" "$ENV_FILE")" \
      AGENT_ENV_FILES="$BASE_ENV:$ENV_FILE" \
      "$PYTHON" -m uvicorn "$ENTRY" --host "$HOST" --port "$PORT"
    ;;
  up|*)
    if out="$(health)"; then echo "[graph] уже поднят: $out"; exit 0; fi
    mkdir -p "$LOGDIR"
    setsid nohup env PYTHONPATH="$PROJECT_DIR" \
      DB_URL="$(dsn "$BASE_ENV" "$ENV_FILE")" \
      VECTOR_DB_URL="$(vector_dsn "$BASE_ENV" "$ENV_FILE")" \
      REMINDERS_CHECKPOINT_DSN="$(reminders_dsn "$BASE_ENV" "$ENV_FILE")" \
      AGENT_ENV_FILES="$BASE_ENV:$ENV_FILE" \
      "$PYTHON" -m uvicorn "$ENTRY" --host "$HOST" --port "$PORT" \
      >> "$LOG" 2>&1 < /dev/null &
    pgrep -f "$PROC" > "$PIDFILE" 2>/dev/null || true
    for i in $(seq 1 20); do
      if out="$(health)"; then echo "[graph] поднят (попытка $i): $out"; exit 0; fi
      sleep 2
    done
    echo "[graph] НЕ поднялся за 40 с — хвост $LOG:"
    tail -n 25 "$LOG" || true
    exit 1
    ;;
esac
