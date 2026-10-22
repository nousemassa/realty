#!/usr/bin/env bash
# Вендоры платформы (`PLATFORM_VENDOR`) — ОДИН вход для лончеров и сторожей.
#
# **Вендоры равноправны.** Любой вендор из `infra/vendors.tsv` можно выбрать осью; различаются они не
# «основной/второй», а набором возможностей (`runtime`, `channel`, `http_api`, `telemetry_receiver`,
# `span_form`). Код спрашивает ВОЗМОЖНОСТЬ, а не имя: иначе вендор без возможности окажется «мачехой».
#
# **Зачем один файл, а не проверка в каждом лончере.** Копия разъедется на первой правке, и один из
# лончеров начнёт молча поднимать продукт в режиме, где рантайма нет.
#
# Подключение:  source "$PROJECT_DIR/lib/vendor.sh"
# Проверка:     require_platform_mode "продукт realty" "$BASE_ENV"   # только на «поднять», не на stop/status

#: Таблица вендоров рядом с обвязкой. Путь можно переопределить (тесты, нестандартная раскладка).
#: `INFRA` обычно задан `lib/workspace.sh`; если библиотеку подключили отдельно — считаем от себя.
#: Таблица лежит РЯДОМ с этой библиотекой: путь считается от файла, а не от корня проекта.
VENDORS_FILE="${VENDORS_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vendors.tsv}"

#: Режим из основы окружения. Пусто ⇒ значение по умолчанию из таблицы (первая строка с `runtime=yes`).
platform_vendor() {
  local env_file="${1:-.env}" vendor
  vendor="$(sed -n 's/^PLATFORM_VENDOR=//p' "$env_file" 2>/dev/null | head -1 | tr -d '"'"'"'' | tr -d '[:space:]')"
  if [[ -n "$vendor" ]]; then printf '%s' "$vendor"; return 0; fi
  vendor_capability_default
}

#: Вендор по умолчанию — тот, что объявлен первым как способный исполнять роли (`runtime=yes`).
vendor_capability_default() {
  require_vendors_file
  awk -F'\t' '!/^#/ && NF > 1 && $2 == "yes" { print $1; exit }' "$VENDORS_FILE"
}

#: Таблица обязательна: без неё «вендор неизвестен» — ложь, причина в другом, и её надо назвать.
require_vendors_file() {
  [[ -f "$VENDORS_FILE" ]] || {
    echo "[vendor] ОСТАНОВ: нет таблицы вендоров $VENDORS_FILE" >&2
    echo "          она объявляет вендоров и их возможности; положите файл на место или задайте VENDORS_FILE" >&2
    exit 2
  }
}

#: Возможность вендора из таблицы: `yes`/`no` (или имя проекта стека для `compose_project`).
#: Неизвестный вендор — пусто (и это отказ у вызывающего).
vendor_capability() {
  local vendor="${1:?вендор}" capability="${2:?возможность}" col
  case "$capability" in
    runtime) col=2 ;;
    channel) col=3 ;;
    http_api) col=4 ;;
    telemetry_receiver) col=5 ;;
    span_form) col=6 ;;
    compose_project) col=7 ;;
    *) echo "[vendor] ОСТАНОВ: неизвестная возможность '$capability'" >&2; return 2 ;;
  esac
  require_vendors_file
  awk -F'\t' -v v="$vendor" -v c="$col" '!/^#/ && NF > 1 && $1 == v { print $c; exit }' "$VENDORS_FILE"
}

#: Известен ли вендор таблице.
vendor_known() {
  local vendor="${1:?вендор}"
  require_vendors_file
  [[ -n "$(awk -F'\t' -v v="$vendor" '!/^#/ && NF > 1 && $1 == v { print v; exit }' "$VENDORS_FILE")" ]]
}

#: Перечень известных вендоров — для текста отказа.
vendors_all() {
  [[ -f "$VENDORS_FILE" ]] || { echo "нет таблицы $VENDORS_FILE"; return 0; }
  awk -F'\t' '!/^#/ && NF > 1 { printf "%s%s", sep, $1; sep=", " } END { print "" }' "$VENDORS_FILE"
}

#: Отказ ПОДНИМАТЬ продукт у вендора, который этого не умеет. Код 2 — «условия не те», а не «сломалось».
#: ❗ Не звать на `stop`/`status`: погасить или посмотреть службу в чужом режиме — законное действие.
require_platform_mode() {
  local what="${1:?что поднимаем}" env_file="${2:-.env}" vendor capable
  vendor="$(platform_vendor "$env_file")"
  if ! vendor_known "$vendor"; then
    cat >&2 <<EOF
[$(basename "$0")] ОСТАНОВ: $what не поднимается — PLATFORM_VENDOR=$vendor неизвестен.
  Известные вендоры: $(vendors_all)
  Проверьте строку PLATFORM_VENDOR в $env_file
EOF
    exit 2
  fi
  capable="$(vendor_capability "$vendor" runtime)"
  [[ "$capable" == "yes" ]] && return 0
  cat >&2 <<EOF
[$(basename "$0")] ОСТАНОВ: $what не поднимается — у вендора $vendor нет возможности 'runtime'.
  Возможности вендора описаны данными: $VENDORS_FILE (колонка 'runtime').
  Что делать:
    · выбрать вендора, который умеет исполнять роли (ось PLATFORM_VENDOR в $env_file)
    · либо это не ваша задача: режим меняет владелец, осознанным действием
  Это НЕ поломка платформы: проверьте строку PLATFORM_VENDOR.
EOF
  exit 2
}
