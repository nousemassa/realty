#!/usr/bin/env bash
# Проект `realty`: продукт целиком — мост, граф напоминаний, MCP-листенер и две его базы. Своя папка,
# свой композ, своё имя проекта compose (`realty` — намеренно: по нему названы тома), свой каталог сессий.
#
#   bash realty-up.sh            # поднять службы и дождаться готовности
#   bash realty-up.sh status     # состояние служб и подключения Telegram
#   bash realty-up.sh stop       # остановить (данные и тома НЕ трогаются)
#
# Хостовый (неконтейнерный) путь запуска — лончеры в этой же папке: `bridge-up.sh`,
# `bridge-up-reminders.sh`, `bridge-up-mcp.sh` (они же — то, что зовут юниты; это ОТКАТ).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#: Корни даёт своя библиотека: она же разрешает `AGENT_WORKSPACE`, когда проект стоит отдельно.
source "$PROJECT_DIR/lib/workspace.sh"   #: PROJECT_DIR и WORKSPACE
#: Режим платформы — ОДИН вход: в чужом режиме продукт поднимать не на чем, и лончер обязан сказать это
#: ПРИЧИНОЙ, а не падением миграции.
source "$PROJECT_DIR/lib/vendor.sh"
COMPOSE="$PROJECT_DIR/compose.realty.yaml"
ENV_FILE="${REALTY_ENV:-$PROJECT_DIR/.env}"                       #: окружение ПРОЕКТА (обязательно)
BASE_ENV="${REALTY_BASE_ENV:-$WORKSPACE/.env}"                   #: общая основа (необязательна)

[[ -f "$ENV_FILE" ]] || { echo "[realty] ОСТАНОВ: нет окружения проекта $ENV_FILE" >&2; exit 2; }
#: Каталог сессий нужен ДО старта: файл-сессия — ключ доступа к Telegram, без каталога мост молчит.
mkdir -p "$PROJECT_DIR/.sessions"

#: Аргументы композа — СПИСКОМ: необязательная общая основа попадает в них, только если она есть.
#: Порядок важен: побеждает последний файл, поэтому окружение проекта идёт после основы.
COMPOSE_ENV_ARGS=(--env-file "$ENV_FILE")
if [[ -f "$BASE_ENV" ]]; then
  COMPOSE_ENV_ARGS=(--env-file "$BASE_ENV" "${COMPOSE_ENV_ARGS[@]}")
fi
compose() { docker compose "${COMPOSE_ENV_ARGS[@]}" -f "$COMPOSE" "$@"; }

ACTION="${1:-up}"

#: Печатает одно поле из /health. Отдельным `python3 -c`, а не подстановкой в строку shell: экранирование
#: кавычек внутри f-строки в однокавычных аргументах даёт SyntaxError, и проверка молча говорит
#: «НЕ отвечает» при живых здоровых контейнерах.
field() {
  local url="$1" expr="$2"
  curl -fsS -m 6 "$url" 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
print($expr)
" 2>/dev/null
}

status() {
  local bridge graph
  bridge="$(field http://127.0.0.1:8085/health "f'telegram={d[\"telegram\"]} режим={d[\"bot_mode\"]}'")"
  graph="$(field http://127.0.0.1:8089/health "f'enabled={d[\"enabled\"]} awaiting={d[\"awaiting\"]}'")"
  printf '  %-24s %s\n' "мост (8085)" "${bridge:-НЕ отвечает ✗}"
  printf '  %-24s %s\n' "граф (8089)" "${graph:-НЕ отвечает ✗}"
  printf '  %-24s %s\n' "MCP-листенер (8087)" "$(curl -fsS -m 6 -o /dev/null -w '%{http_code}' http://127.0.0.1:8087/health 2>/dev/null || echo '—')"
  compose ps --format 'table {{.Service}}\t{{.Status}}' | tail -n +2 | sed 's/^/  /' || true
  [[ -n "$bridge" ]] || return 1
}

case "$ACTION" in
  status) status ;;
  stop)
    compose stop
    echo "[realty] службы остановлены. Тома и данные не тронуты; хостовый путь — лончеры bridge-up*.sh"
    ;;
  up|*)
    require_platform_mode "продукт realty" "$BASE_ENV"
    compose up -d
    for i in $(seq 1 40); do status >/dev/null 2>&1 && { sleep 2; status; exit 0; }; sleep 3; done
    echo "[realty] ОСТАНОВ: мост не ответил за 120 с — смотрите docker logs realty-bridge-1" >&2
    exit 1
    ;;
esac
