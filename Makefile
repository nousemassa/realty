# Проект `realty`: продукт (мост, граф напоминаний, MCP-листенер) и его базы. Свой Makefile — чтобы проект
# можно было поднять и проверить БЕЗ воркспейса. Логика живёт в лончере, цели её не дублируют.
#: Интерпретатор с окружением проекта.
PY ?= python3
.PHONY: up status stop restart images check test ci

up:
	bash realty-up.sh up

status:
	bash realty-up.sh status

stop:
	bash realty-up.sh stop

restart: stop up

#: Образ проекта — `realty-agent:latest`: из него поднимаются мост, граф, MCP-листенер и накат схемы.
images:
	docker compose --env-file .env -f compose.realty.yaml build

# Офлайн-проверка: композ обязан РАЗБИРАТЬСЯ настоящим парсером (демон не нужен).
#: Обязательные ключи (`:?` в композе) подставляет проверка: разбор их не создаёт, а значение нужно
#: только для подстановки. Образец окружения — `.env.example`, настоящий `.env` не требуется.
check:
	@PLATFORM_NETWORK=__check__ VECTOR_DB_PASSWORD=__check__ GRAPH_DB_PASSWORD=__check__ \
	  LLAMA_EMBED_URL=http://embed:8082/v1 SOLVER_URL=http://solver:8083 \
	  docker compose --env-file .env.example -f compose.realty.yaml config -q \
	  && test "$$(PLATFORM_NETWORK=__check__ VECTOR_DB_PASSWORD=__check__ GRAPH_DB_PASSWORD=__check__ \
	  LLAMA_EMBED_URL=http://embed:8082/v1 SOLVER_URL=http://solver:8083 \
	  docker compose --env-file .env.example -f compose.realty.yaml config --format json \
	  | $(PY) -c 'import json,sys; print(json.load(sys.stdin)["name"])')" = "realty" \
	  && echo "[realty] композ разбирается, имя проекта — realty (тома на месте) ✓"

#: Офлайн-тесты. Живые (нужна поднятая база из `.env`) пропускают себя сами; чтобы пропуск стал
#: отказом — `AGENT_LIVE_STRICT=1 make test`.
test:
	$(PY) -m pytest tests -q

#: То же, что гоняет CI: линтер по своим файлам и свои офлайн-тесты.
ci:
	$(PY) -m ruff check .
	$(PY) -m pytest tests -q
