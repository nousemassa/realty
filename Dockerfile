# Образ ПРОДУКТА (`realty-agent`): свой проект, свой пакет (`realty_agent`), общий слой — колёсом.
#
# Multi-stage: колёса собираются отдельно от рантайма (меньше образ, нет build-зависимостей в проде).
FROM python:3.13-slim AS builder
ENV PIP_NO_CACHE_DIR=1
WORKDIR /wheels
COPY pyproject.toml ./
# Колесо ОБЩЕГО СЛОЯ (`agent-core`) лежит в репозитории готовым — `wheels/` — и берётся оттуда: этого
# пакета нет в индексе, и без локального файла разрешение `agent-core==…` ушло бы в сеть за тем, чего
# там нет. Этап сборки колёс сеть использует (pip, build, зависимости); этап рантайма ставит всё
# с `--no-index` — только из `/wheels` и `/wheels-core`.
COPY wheels /wheels-core
# `setuptools` кладём в /wheels рядом с зависимостями: установка ниже идёт с `--no-index`, и бэкенд
# сборки должен лежать локально (иначе `Could not find a version that satisfies the requirement setuptools`).
RUN pip install --upgrade pip build && pip wheel --wheel-dir /wheels --find-links=/wheels-core . setuptools

FROM python:3.13-slim
# libheif НЕ ставим: pillow-heif несёт свои wheels с libheif>=1.23 (апт-версия устарела и не загружается).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

#: ❗ Каталог файлов-сессий НЕ выводится из расположения кода (общий слой ставится колёсом и живёт в
#: `site-packages`): он приходит настройкой `SESSIONS_DIR`, её задаёт композ. Путь от `__file__` в
#: установленном пакете указывает внутрь `site-packages`, и бот молча не подключается.
WORKDIR /srv/realty
COPY --from=builder /wheels /wheels
COPY wheels /wheels-core
COPY pyproject.toml ./
COPY realty_agent ./realty_agent
#: ❗ Тексты агентов входят в ОБРАЗ: свой рантайм (`AGENT_RUNTIME=own`) берёт из них системный промпт,
#: `temperature` и `max_tokens`, и другого источника у него нет. Без этой строки файл существовал бы
#: только в репозитории, а контейнер отказывал бы на старте с названным путём (`DIALOG_PROMPT_PATH`).
#: Промпт при этом остаётся под контролем версий: правка текста = пересборка образа, а не «тихо
#: изменилось поведение живого бота».
COPY onboarding ./onboarding
RUN pip install --no-cache-dir --no-index --find-links=/wheels --find-links=/wheels-core setuptools \
 && pip install --no-cache-dir --no-index --find-links=/wheels --find-links=/wheels-core --no-build-isolation -e . \
 && rm -rf /wheels /wheels-core

EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8081/health', timeout=4).status_code==200 else 1)"

# Один процесс = один event loop: меньше памяти и предсказуемее локи/очереди.
# Конкретную команду задаёт композ (мост, граф или MCP-листенер — они из одного образа).
CMD ["uvicorn", "realty_agent.bridge_main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--no-access-log"]
