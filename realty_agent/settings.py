"""Настройки продукта: боты, календарь, напоминания, роли и лимиты объявлений.

Общая часть (транспорт Telegram, БД, платформа, LLM, эмбеддинги, MCP, решатель, телеметрия) живёт в
`agent_core.config.CoreSettings`.
"""
from __future__ import annotations

from pydantic import Field

from agent_core.config import CoreSettings


class Settings(CoreSettings):
    # --- Адреса сервисов моделей (объявляет АГЕНТ, а не ядро) ---
    #: Службы моделей локальные, но адреса к ним — свойство развёртывания: в бою они могут стоять на
    #: разных хостах, поэтому адрес приходит ТОЛЬКО из `.env` этого агента. Пустое значение — отказ на
    #: старте с именем ключа: «удобное умолчание» означало бы запрос не туда и невидимую причину.
    llama_embed_url: str = Field(..., min_length=1)
    llama_embed_api_key: str = ""
    llama_embed_model: str = Field(..., min_length=1)
    llama_embed_timeout_s: float = 15.0
    solver_url: str = Field(..., min_length=1)
    solver_api_key: str = ""
    solver_timeout_s: float = 60.0
    # --- Telegram (MTProto-транспорт; Bot API с dev-хоста заблокирован) ---
    telegram_bot_token: str = ""
    telegram_manager_chat_id: str = ""
    # Контакт для тапа «📞 Запрос звонка от менеджера» (телефон/почта/юзернейм). Вписывает владелец в .env;
    # пусто ⇒ клиент получает честный фолбэк без обещания передачи.
    manager_contact: str = ""
    bridge_bot_mode: str = "live"     # live|paused|off
    # --- Календарь: приватный iCal-адрес, пусто ⇒ механизм выключен ---
    calendar_ics_url: str = ""
    calendar_notify_ahead_h: int = 24   # окно «скоро» — сколько часов вперёд смотрим
    calendar_poll_s: int = 300          # период опроса календаря (дедуп — в БД, повторов не будет)
    #: текст уведомления формулирует агент (факты — из шаблона; при сбое/невалидном ответе — шаблон).
    calendar_agent_text: bool = True
    # --- Напоминания о подтверждённом визите ---
    # ВЫКЛЮЧЕНО по умолчанию: механизм пишет клиентам напрямую, включать его осознанно.
    # Пока выключен, петля планировщика не запускается вовсе (как календарь без URL).
    visit_reminder_enabled: bool = False
    visit_reminder_hours_before: float = 20.0   # N: за сколько часов до визита начинаем
    visit_reminder_repeat_h: float = 4.0        # M: пауза перед повтором при молчании
    visit_reminder_max_repeats: int = 2         # K: сколько повторов допустимо (0 ⇒ без повторов)
    visit_reminder_tick_s: int = 300            # период петли планировщика
    # Доля визитов, которым напоминание уходит (остальные — контрольная группа замера).
    # Деление — по менеджеру и дате слота, см. `realty_agent/booking/visits.py:reminder_group`.
    visit_reminder_treatment_share: int = 50
    # Чекпойнты графа — в базе векторного контейнера из (платформенный postgres не
    # трогаем). Пусто ⇒ DSN выводится из `VECTOR_DB_URL` заменой драйвера: чекпойнтер
    # ходит через psycopg, а не asyncpg, поэтому `+asyncpg` ему мешает.
    # Своих таблиц граф не заводит: их создаёт `AsyncPostgresSaver.setup()`, и в наш DDL
    # они не входят — иначе «один источник схемы» стало бы неправдой.
    #: Адрес чекпойнтов напоминаний. Имя по РОЛИ: служба `reminders`, база `reminders`.
    reminders_checkpoint_dsn: str = ""
    # Локальное закрытие хода диалога (, шаг 2): сообщение-условие разбирает наш код, агент не
    # вызывается. ВЫКЛЮЧЕНО по умолчанию осознанно: замер по хранимой истории (81 ход) даёт
    # совпадение выдачи с агентской на 11 ходах из 12, но на трёх поведение всё же меняется
    # (1 расхождение + 2 сужения) — это решение продукта, а не следствие флага.
    dialog_ladder_enabled: bool = False
    #: Путь к файлу агента для СВОЕГО рантайма (`AGENT_RUNTIME=own`): из него берутся системный промпт,
    #: `temperature` и `max_tokens` (см. `vendors/runtime/prompt_spec.py`). ❗ Без дефолта: путь не
    #: угадывается ни из расположения кода, ни из текущего каталога — в контейнере файл лежит в образе
    #: (`/srv/realty/onboarding/…`), на хосте — в воркспейсе. Пусто при `own` = громкий отказ на старте.
    dialog_prompt_path: str = ""
    #: Цены диалога для пересчёта токенов в деньги — по той же семантике, что `team_price_*`: ноль
    #: означает «цена не задана», и тогда `cost` в `usage` уходит как `None` (НЕ ноль: ноль в отчёте
    #: читался бы как «ход бесплатный», а это неправда — `cost_usd` в базе считает `coalesce(sum,0)`).
    dialog_price_in_per_1k_usd: float = 0.0
    dialog_price_out_per_1k_usd: float = 0.0
    #: То же для роли ОБЪЯВЛЕНИЙ (`listing`). Отдельные поля, а не словарь: env и `.env.example`
    #: живут ключами, и «одно поле — один ключ» — правило проекта; словарь в env нечитаем.
    #: Цены могут отличаться от диалоговых (иная модель/тариф), поэтому общий аксессор — ниже,
    #: а не «взять диалоговые, сойдёт».
    listing_prompt_path: str = ""
    listing_price_in_per_1k_usd: float = 0.0
    listing_price_out_per_1k_usd: float = 0.0
    # Порт и ХОСТ процесса графа напоминаний: свой процесс, слушает только loopback. Мост
    # ходит к нему за продолжением потока (ответ менеджера) — сессия Telegram остаётся у моста.
    #
    # Хост — настройкой, а не константой: мост переезжает в контейнер, а граф пока остаётся
    # хостовым процессом, и из контейнера `127.0.0.1` — это сам контейнер. Зашей хост литералом, и
    # продолжение потока ушло бы «в себя»: кнопка менеджера молча перестала бы работать.
    bridge_graph_host: str = "127.0.0.1"
    bridge_graph_port: int = 8089
    # канал диалога клиента (Telegram → агент записи на просмотры)
    agent_bot_dialog: str = "realtor-bot"
    dialog_connection_slug: str = "realtor"
    dialog_workflow_id: str = ""
    dialog_revision_id: str = ""
    # канал генерации объявлений (фото → текст)
    agent_bot_listing: str = "listing-writer"
    listing_connection_slug: str = "listing-writer-conn"
    listing_workflow_id: str = ""
    listing_revision_id: str = ""
    listings_max_per_day: int = 20
    listings_max_photo_mb: int = 10


settings = Settings()
