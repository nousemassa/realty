"""Telegram-транспорт (Telethon, MTProto) поверх DialogService.

Почему MTProto, а не Bot API: с dev-хоста `api.telegram.org` заблокирован (HTTPS 000), а
MTProto-прокси из t.me-ссылки работает — см. `realty_agent/tg/mtproto.py`. Тот же токен бота, тот же
функционал (сообщения, inline-кнопки, callback, файлы без лимита 20 МБ Bot API).

Поток: update → лимит/дедуп → гейт согласия → DialogService (агент через канал) → ответ клиенту.
Менеджер: /pending → inline ✅/❌ (callback_data с HMAC) → статус в БД → уведомление клиента.
Апдейты приходят push'ем по MTProto — long polling не нужен, `start()` не блокирует.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import defaultdict

from telethon import Button, TelegramClient, events, functions, types
from telethon.errors import (
    FloodWaitError,
    PeerIdInvalidError,
    RPCError,
    UserDeactivatedError,
    UserIsBlockedError,
)

from agent_core import telemetry
from agent_core.decisions.client import make_ask
from agent_core.decisions.ladder import threshold_from
from agent_core.ids import chat_hash
from agent_core.tg.connection import TelegramConnection
from agent_core.ui import tokens
from agent_core.ui.reply import INLINE, REPLY, Reply, rows_from_payload, rows_to_payload
from realty_agent import outbox as outbox_queue
from realty_agent.booking.slots import free_slots
from realty_agent.booking.visits import classify_answer
from realty_agent.channels.turn_context import Listing
from realty_agent.dialog import (
    OFFER_REMIND_AFTER_S,
    OFFER_TTL_S,
    AgentReply,
    Booking,
    ChatState,
    DialogService,
)
from realty_agent.integrations import ical as calendar_connector
from realty_agent.local_close import gate

#: продолжения графа и адресат ожидания. Импортируются на уровне модуля: сам
# LangGraph подтягивается лениво (внутри функций), чтобы мост не зависел от него при старте.
from realty_agent.reminders.pipeline import (
    AWAIT_CLIENT,
    as_datetime,
    resume_client,
    resume_manager,
)
from realty_agent.tg import render
from realty_agent.tg.callbacks import sign_callback, verify_callback
from realty_agent.tg.handlers import (
    BOOKING_CONFIRMED_MSG,
    BOOKING_REJECTED_MSG,
    CLIENT_GREETING,
    CLIENT_HOWTO,
    CLIENT_PATH_NOTICE,
    COMMANDS,
    CONFIRM_ONLY,
    CONSENT_TEXT,
    EMPTY_QUEUE_TEXT,
    FAILOVER_TEXT,
    GREETING,
    MANAGER_INSTRUCTION,
    MANAGER_STARTED_TEXT,
    MANAGER_STOP_NOTE,
    MANAGERS_ONLY_OWNER,
    NOAI_TEXT,
    OFFER_REMINDER_TEXT,
    OWNER_ONLY,
    OWNER_START_NOTE,
    PAUSED_TEXT,
    PRIVACY_TEXT,
    SECTION_LATER_TEXT,
    STOP_TEXT,
    TOO_OFTEN_TEXT,
    VIEW_ONLY,
    VIEWS_EMPTY,
    VIEWS_OWN_SUFFIX,
    VIEWS_TITLE,
    is_consent_command,
    parse_command,
)
from realty_agent.tg.limits import (
    VISIT_ABSENT,
    VISIT_ACTIONS,
    VISIT_ANSWER_ACK,
    VISIT_BUSY,
    VISIT_BUSY_STATUS,
    VISIT_CLIENT_ACTIONS,
    VISIT_MANAGER_ACTIONS,
    VISIT_MARK_ACTIONS,
    VISIT_MARK_ONLY,
    VISIT_MARK_PROMPT,
    VISIT_MARKED,
    VISIT_NO_FLOW,
    VISIT_RESUMED,
    VISIT_REWRITE_HINT,
    VISIT_SENT_TO_CLIENT,
    RateLimiter,
    client_notification_text,
    format_slot,
    format_slot_iso,
    status_for_action,
)
from realty_agent.tg.manager_requests import ManagerRequests
from realty_agent.tg.menu_router import CLIENT_CODES, MenuRouter
from realty_agent.tg.owner_flows import OwnerFlows
from realty_agent.tg.roles import CLIENT, MANAGER, OWNER, may, resolve_role
from realty_agent.ui import format as fmt
from realty_agent.ui import menu as ui_menu
from realty_agent.ui.keyboards import (
    client_menu_kb,
    consent_kb,
    edit_menu_kb,
    manager_menu_kb,
    owner_menu_kb,
)
from realty_agent.ui.kinds import (
    KIND_APPLY,
    KIND_APPLY_NO,
    KIND_APPLY_OK,
    KIND_APPLY_PICK,
    KIND_BACK,
    KIND_CANCEL,
    KIND_CONFIRM,
    KIND_DAY,
    KIND_EDIT,
    KIND_FILTER_RESET,
    KIND_LISTING,
    KIND_OWNER,
    KIND_OWNER_OFF,
    KIND_OWNER_PICK,
    KIND_TIME,
)

log = logging.getLogger("realty_agent.tg")

# Какое право нужно для овнер-метки (права ролей — в realty_agent/tg/roles.py).
OWNER_CODE_RIGHTS = {
    ui_menu.MENU_OWNER_BOOKINGS: "view_pending",
    ui_menu.MENU_OWNER_REQUESTS: "manage_managers",
    ui_menu.MENU_OWNER_VIEWS: "view_views",
    ui_menu.MENU_OWNER_MANAGERS: "manage_managers",
    ui_menu.MENU_OWNER_PAUSE: "pause",
    ui_menu.MENU_OWNER_RESUME: "resume",
    ui_menu.MENU_OWNER_REPORT: "report",
    ui_menu.MENU_OWNER_STATUS: "status",
}

# Виды inline-кнопок овнер-меню (подписанные; тапы принимает `_on_callback`, не MenuRouter).
OWNER_KINDS = (KIND_OWNER, KIND_OWNER_PICK, KIND_OWNER_OFF)
APPLY_KINDS = (KIND_APPLY, KIND_APPLY_PICK, KIND_APPLY_OK, KIND_APPLY_NO)
# Клиентские кнопки: их обрабатывает транспорт через DialogService (без LLM).
CLIENT_KINDS = (KIND_LISTING, KIND_DAY, KIND_TIME, KIND_BACK, KIND_CONFIRM, KIND_EDIT, KIND_CANCEL,
                KIND_FILTER_RESET)

#: Сколько слотов предлагаем за раз: 12 ≈ 1,3 рабочих дня при 10:00–18:00.
OFFER_SLOTS = 12
RATE_PER_MIN = 10
#: Сколько ждём формулировку от агента и какой ответ считаем годным. 45 с, а не 30: первый вызов
#: укладывается в 16 с, а параллельный ход к тому же агенту — в больше 30 с. При таймауте уходит
#: шаблон, и он хуже по форме.
CALENDAR_TEXT_WAIT_S = 45.0
CALENDAR_TEXT_MAX_CHARS = 300
# Дебаунс служебных уведомлений (пауза, запрос звонка): не чаще раза в минуту на получателя.
NOTIFY_COOLDOWN_S = 60
# Периодичность проверки «зависших» офферов для напоминания ()
REMINDER_TICK_S = 60
# Тик sweeper: ходы с просроченным lease разбираются раз в 2 минуты — чаще
# незачем, срок lease по умолчанию 120 с.
TURN_SWEEP_TICK_S = 120
# Как часто сторож соединения проверяет, жив ли Telegram. Проверка дешёвая (`is_connected()` —
# локальное состояние), поэтому тик короткий: цена ошибки — бот, молчащий лишние полчаса.
# Файл-сессия моста: авторизация переживает рестарты и НЕ тратит bot-логины, на которые Telegram
# накладывает FloodWait. У новостного бота такая сессия была (`tg/news_bot.py`), у моста — нет;
# с переподключением это стало опасным: каждый повтор иначе означал бы новый bot-логин.
# Бюджет одной попытки переподключения. Больше бюджета старта: во время обрыва сети ждать дольше
# полезнее, чем чаще дёргать прокси.
TYPING_REFRESH_S = 4
# Сетевые ошибки MTProto: RPCError (ответ Telegram) + транспорт (proxy/DC/таймаут).
NETWORK_ERRORS = (RPCError, ConnectionError, OSError, asyncio.TimeoutError)
# Адресат недостижим навсегда: это не «сеть моргнула», а «карточки к нему больше не ходят».
UNREACHABLE_ERRORS = (UserIsBlockedError, UserDeactivatedError, PeerIdInvalidError)
# Сколько ждать повтора, если Telegram не назвал число (`FloodWaitError.seconds` пусто) —.
# По умолчанию берём нижнюю границу лимита Telegram: повтор раньше всё равно упрётся в отказ.
OUTBOX_DEFAULT_RETRY_S = 5.0
# Сколько мост ждёт процесс графа. Граф отвечает быстро (чтение состояния и продолжение), но
# он отдельная служба: если он тормозит или лежит, ответ клиенту не должен ждать его бесконечно.
VISIT_PROCESS_TIMEOUT_S = 10.0


def _is_private(event) -> bool:
    """Личный ли чат. Fail-closed: неизвестно/группа ⇒ прав роли не даём."""
    return getattr(event, "is_private", None) is True


class TelegramTransport:
    def __init__(self, *, settings, db, channels, retriever=None, turns=None, tracer=None) -> None:
        self._settings = settings
        # Трассировка. Необязательный шов, как ретривер и durable-состояние: нет трейсера —
        # спаны не создаются, и код хода об этом не думает (`with tracer.start_as_current_span`
        # у пустышки работает как ничего не делающий контекст).
        self._tracer = tracer
        self._db = db
        self._channels = channels
        # Поиск по базе знаний. Необязателен: без него (или с пустым корпусом)
        # диалог работает как раньше — блок «База знаний» в ход не добавляется.
        self._retriever = retriever
        # Durable-состояние хода: необязательный шов, как и ретривер.
        self._turns = turns
        # Подключение к Telegram — ОДИН общий механизм (`realty_agent/tg/connection.py`), он же у новостного
        # бота. Создаётся в конструкторе, а не в `start()`: транспортные свойства и тесты читают
        # `_client` и до старта, и механизм не должен зависеть от того, состоялся ли старт.
        # На каком клиенте уже стоят обработчики: повторная регистрация на том же объекте дала бы
        # двойные ответы клиенту.
        self._handlers_client = None
        self._connection = TelegramConnection(
            settings=settings, name="бот", session_name="bridge",
            token=getattr(settings, "telegram_bot_token", "") or "",
        )
        self._limiter = RateLimiter(per_minute=RATE_PER_MIN)
        self._clock = time.monotonic
        self._menu = MenuRouter(settings=settings, listings=self._listings, load_state=self._load_state,
                                count_listings=getattr(self._db, "count_active_listings", None))
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._notified_at: dict[tuple[str, str], float] = {}
        # Тайм-аут флоу считается по стенным часам: метка приходит из БД (epoch), не из monotonic.
        self._manager_requests = ManagerRequests(
            db=db, settings=settings, notify_owner=self._notify_owner,
            notify_applicant=self._send_to, clock=time.time,
        )
        self._owner_flows = OwnerFlows(
            db=db, settings=settings, client_getter=lambda: self._client, clock=time.time
        )
        self._updates_processed = 0
        self._last_update_ts: float | None = None
        #: граф напоминаний и его чекпойнтер. `None` до старта и при выключенном
        # механизме — по этим полям видно, работает ли граф, без чтения журнала.
        # Кто из менеджеров сейчас пишет новый текст напоминания: `booking_id → chat_id`.
        # Состояние потока живёт в чекпойнте и переживает перезапуск, а эта подсказка — нет:
        # она означает «менеджер начал печатать», и потерять её безвредно (он нажмёт кнопку
        # снова). Обещать её живучесть было бы неправдой.
        self._rewrite_pending: dict[int, str] = {}
        # Подпись кнопок — отдельным секретом: утечка X-API-Key не должна позволять подделывать кнопки.
        self._ui_secret = settings.ui_secret if settings.ui_buttons_enabled() else ""
        if not self._ui_secret:
            log.warning("UI_SECRET не задан — кнопки менеджера отключены (текстовый путь работает)")
        self._dialog = DialogService(
            ask_agent=self._ask_agent,
            listings=self._listings,
            free_slots=self._free_slots,
            listing_exists=self._db.listing_exists,
            slot_free=self._slot_free,
            busy_slots=self._db.busy_slots,
            save_booking=self._save_booking,
            notify_manager=self._notify_manager,
            save_state=self._save_state,
            load_state=self._load_state,
            #: необязательная зависимость (как в MenuRouter) — приписка про лимит не критична
            count_listings=getattr(self._db, "count_active_listings", None),
            #: поиск по базе знаний. Шов необязательный, поэтому передаётся только если
            # ретривер собран (база знаний поднята) — иначе DialogService пойдёт без него.
            search_knowledge=self._search_knowledge if self._retriever is not None else None,
            knowledge_topics=self._knowledge_topics if self._retriever is not None else None,
            report_coverage=self._report_coverage,
            report_contract=self._report_contract,
            report_ladder=self._report_ladder,
            knowledge_limit=getattr(settings, "retrieval_top_k", 4),
            turns=self._turns,
            record_trace=self._record_trace,
            record_message=self._record_message,
            #, шаг 2: гейт «сообщение — ТОЛЬКО условие?» строится на том же клиенте решателя,
            # что и всё остальное (один вход обращения), и передаётся ОДНОЙ функцией: диалог не
            # знает ни про адрес решателя, ни про порог. Флаг по умолчанию выключен.
            local_gate=gate.gate(ask=make_ask(settings.solver_url, api_key=settings.solver_api_key,
                                       timeout_s=settings.solver_timeout_s),
                          threshold=threshold_from(settings)),
            ladder_enabled=getattr(settings, "dialog_ladder_enabled", False),
        )

    async def _record_message(self, *, platform: str, chat_id: str, turn_key: str, role: str,
                              text: str, step: str) -> None:
        """Записать реплику хода в историю диалога.

        Пишет МОСТ, а не диалог и не рантайм: у моста есть пул базы, а диалог не должен знать, как
        хранятся реплики (тот же принцип, что у трассы: диалог не знает про хеширование клиента).
        Текст приходит уже маскированным (`dialog.py`: телефон закрывается ДО промпта).
        """
        await self._db.append_dialog_message(
            platform=platform, chat_id=chat_id, turn_key=turn_key, role=role, text=text, step=step)

    async def _record_trace(self, trace: dict) -> None:
        """Записать трассу хода — единственный писатель этих строк.

        Идентификатор клиента хешируется здесь, а не в диалоге: диалог не должен знать, как именно
        мы обезличиваем данные, а таблица обязана их не содержать.
        """
        today = dt.datetime.now(self._settings.tz).date()
        await self._db.save_turn_trace(
            day=today,
            platform=trace.get("platform", "tg"),
            chat_hash=chat_hash(str(trace.get("chat_id", ""))),
            step=str(trace.get("step", "")),
            prompt_hash=str(trace.get("prompt_hash", "")),
            context_items=int(trace.get("context_items") or 0),
            agent_called=bool(trace.get("agent_called")),
            outcome=str(trace.get("outcome", "")),
            parser_verdict=str(trace.get("parser_verdict", "")),
            turn_id=trace.get("turn_id"),
            session_id=str(trace.get("session_id", "")),
            usage=trace.get("usage"),
            latency_ms=trace.get("latency_ms"),
            trace_id=str(trace.get("trace_id", "")),
        )

    async def _report_ladder(self, kind: str) -> None:
        """Замер лестницы в диалоге (шаг 0): полоса, совпадение спеков, общее число ходов.

        Метрики: `dialog_turn` (знаменатель), `dialog_ladder_band` (ходы, которые закрылись бы
        локально), `dialog_ladder_same` / `dialog_ladder_differs` (совпал ли спек агента с локальным
        разбором — ЭТО измерение риска: расхождение означает, что локальное закрытие применило бы
        другой фильтр). Шаг 0 ничего не меняет в поведении — только считает.
        """
        metric = f"dialog_{'ladder_' + kind if kind != 'turn' else 'turn'}"
        log.info("ladder chat=- kind=%s metric=%s", kind, metric)
        try:
            await self._db.bump_metric(metric=metric)
        except Exception as error:  # noqa: BLE001 — наблюдение не должно ронять ход
            log.warning("ladder: метрика %s не записана (%s)", metric, type(error).__name__)

    async def _report_contract(self, kind: str) -> None:
        """Записать наблюдение за контрактом ответа: `kind` — имя метрики.

        Две метрики: `promise_without_action` — бот пообещал то, чего не делает (сейчас таких
        механизмов нет, поэтому это дефект, а не фича); `escalation_offered` — предложил
        настоящую кнопку вызова менеджера. Отношение этих чисел и есть ответ на вопрос, нужна
        ли автоматическая эскалация.
        """
        log.info("contract chat=- kind=%s", kind)
        try:
            await self._db.bump_metric(metric=kind)
        except Exception as error:  # noqa: BLE001 — наблюдение не должно ронять ход
            log.warning("contract: метрика %s не записана (%s)", kind, type(error).__name__)

    async def _report_coverage(self, answered: bool) -> None:
        """Записать выбор агента «ответил или уточнил».

        Зачем метрика, а не только журнал: владелец поставил вопрос «где мера между уточняющим
        вопросом и прямым ответом», и дешёвой меры не существует (лексическая ошибается в обе
        стороны). Тогда мера — это **наблюдаемая доля**: сколько ходов ответили против скольких
        уточнили. Число накапливается само, и по нему видно, не начал ли бот уточнять там, где
        отвечать уже есть чем.
        """
        metric = "coverage_answered" if answered else "coverage_asked"
        log.info("coverage chat=%s answered=%s", "-", answered)
        try:
            await self._db.bump_metric(metric=metric)
        except Exception as error:  # noqa: BLE001 — наблюдение не должно ронять ход
            log.warning("coverage: метрика не записана (%s)", type(error).__name__)

    async def _knowledge_topics(self) -> list[str]:
        """Механизмы, по которым есть данные: заголовки статей корпуса.

        Агент предлагает их клиенту, когда фрагменты не отвечают на вопрос: без этого списка
        он не знает, что ему есть что предложить, и отвечает общим «в базе нет». Список
        считает ретривер — из БАЗЫ, а не из файла манифеста: он обязан совпадать с тем, что
        реально залито.
        """
        if self._retriever is None:
            return []
        return list(await self._retriever.topics())

    async def _search_knowledge(self, query: str, limit: int) -> list[str]:
        """Тексты найденных фрагментов для хода. Пустой список — нормальное «нет знаний»."""
        if self._retriever is None:
            return []
        chunks = await self._retriever.search(query, limit=limit)
        return [chunk.text for chunk in chunks]

    @property
    def running(self) -> bool:
        return self._connection.running

    # ---------- lifecycle ----------

    async def start(self) -> None:
        token = self._settings.telegram_bot_token
        if not token:
            log.warning("TELEGRAM_BOT_TOKEN не задан — транспорт не запущен")
            return
        # Механизм ждёт готовности и держит соединение; нам остаётся сказать, что делать после.
        await self._connection.start(self._on_telegram_connected)

    # ---------- подключение и переподключение ----------

    # Механизм подключения (массив прокси, файл-сессия, ожидание готовности, сторож) живёт в
    # `realty_agent/tg/connection.py` и ОДИН на все процессы проекта. Здесь остаётся только то, что делается
    # ПОСЛЕ подключения: команды, приветствие и фоновые петли.

    @property
    def _client(self):
        """Клиент подключается механизмом; свойство сохраняет прежние чтения и записи в тестах."""
        return self._connection.client

    @_client.setter
    def _client(self, value) -> None:
        self._connection.client = value

    async def _on_telegram_connected(self, first: bool) -> None:
        """Что делаем после (пере)подключения.

        Приветствие менеджеру — ТОЛЬКО при старте: при переподключении оно превратилось бы в «бот
        запущен» после каждого обрыва, то есть в шум ровно тогда, когда связь и так нестабильна.
        Команды обновляем всегда — операция дешёвая и идемпотентная.
        """
        # ОБРАБОТЧИКИ СТАВИМ НА КАЖДОМ ПОДКЛЮЧЕНИИ — и это не мелочь. Обработчики Telethon живут на
        # объекте клиента, а переподключение создаёт НОВЫЙ объект; без регистрации получается живое
        # соединение без приёма сообщений: `/health` отвечает `telegram: true`, `updates_processed`
        # стоит на нуле, клиент пишет и не получает ответа. Регистрация идёт на клиента, отличного от
        # того, на который уже ставили: повтор на ТОМ ЖЕ объекте дал бы дубли обработчиков (двойные
        # ответы).
        if self._handlers_client is not self._connection.client:
            self._register_handlers(self._connection.client)
            self._handlers_client = self._connection.client
        await self._set_commands()
        if not first:
            self._start_loops()
            return
        me = self._connection.me
        proxy = self._connection.proxy
        log.info(
            "telegram: бот @%s (id=%s) запущен через MTProto (прокси %s:%s)",
            getattr(me, "username", "?"), getattr(me, "id", "?"),
            proxy[0] if proxy else "-", proxy[1] if proxy else "-",
        )
        await self._greet_manager()
        self._start_loops()

    def _start_loops(self) -> None:
        """Поднять фоновые петли — один раз, сколько бы раз ни переподключались."""
        if getattr(self, "_reminder_task", None) is None:
            self._reminder_task = self._watch("reminders", asyncio.create_task(self._offer_reminder_loop()))
        if getattr(self, "_calendar_task", None) is None:
            self._calendar_task = self._watch("calendar", asyncio.create_task(self._calendar_loop()))
        if getattr(self, "_turn_sweep_task", None) is None and self._turns is not None:
            self._turn_sweep_task = self._watch("turn_sweep", asyncio.create_task(self._turn_sweep_loop()))
        # Очередь исходящих живёт рядом с остальными петлями, а не в `main.py`: здесь место
        # петель и единственный, кто умеет отправлять. Пока очередь пуста (производители переедут
        # отдельным срезом), проход стоит одного запроса к базе.
        if getattr(self, "_outbox_task", None) is None and self._db is not None:
            # Событие создаётся ЗДЕСЬ (внутри работающей петли): оно привязано к ней, и создавать его
            # раньше — значит получить объект из чужой петли.
            self._outbox_wake = asyncio.Event()
            self._outbox_task = self._watch("outbox", asyncio.create_task(
                outbox_queue.run_forever(
                    repo=self._db,
                    sender=self._outbox_sender,
                    on_tick=self._outbox_tick,
                    on_result=self._outbox_result,
                    wake=self._outbox_wake,
                ),
                name="outbox",
            ))

    def _watch(self, name: str, task: asyncio.Task) -> asyncio.Task:
        """Заметить смерть петли в журнале: до этого `task.exception()` не читал НИКТО.

        Перезапуска здесь нет НАМЕРЕННО. Во-первых, реально умереть может только календарь (у остальных
        `except Exception` внутри цикла), а его причина — чтение настройки, и она теперь внутри `try`.
        Во-вторых, самоперезапуск через `os._exit`/backoff складывается с `guard_heal` и
        `StartLimitBurst=5/60 с`: пять быстрых выходов уводят юнит в `failed`, а каждый цикл — это новый
        логин MTProto. Состояние видно в `/health.loops`, а не только в журнале.
        """
        def _done(finished: asyncio.Task) -> None:
            if finished.cancelled() or finished.exception() is None:
                return
            log.error("петля %s умерла: %s (см. /health.loops)", name,
                      type(finished.exception()).__name__)

        task.add_done_callback(_done)
        return task

    def loops(self) -> dict[str, str]:
        """Состояние петель для `/health`: `ok` / `dead` / `not_started` / `not_configured`.

        ❗ Четыре состояния, а не два: свопер не поднимается без `turns`, outbox — без базы, и это
        ЗАКОННО (`_start_loops`). Двузначное поле заставило бы сторожа краснеть навсегда. Сторож
        смотрит только на `dead`.
        """
        tasks = {
            "reminders": getattr(self, "_reminder_task", None),
            "calendar": getattr(self, "_calendar_task", None),
            "turn_sweep": getattr(self, "_turn_sweep_task", None),
            "outbox": getattr(self, "_outbox_task", None),
        }
        optional = {"turn_sweep", "outbox"}
        state: dict[str, str] = {}
        for name, task in tasks.items():
            if task is None:
                state[name] = "not_configured" if name in optional else "not_started"
            elif task.done():
                state[name] = "dead"
            else:
                state[name] = "ok"
        return state

    # Граф напоминаний о визите вынесен СВОИМ процессом: здесь его больше нет — ни задачи,
    # ни чекпойнтера, ни пула, ни зависимостей доставки. Мост продолжает потоки HTTP-ручкой процесса
    # (`_visit_resume`), а исходящие графа доставляет воркер очереди. Причина выноса не «мода на
    # сервисы»: у напоминаний своя жизнь (дни), свой пул и свой темп, и их рестарт не должен быть
    # рестартом бота.

    async def stop(self) -> None:
        task, self._reminder_task = getattr(self, "_reminder_task", None), None
        if task is not None:
            task.cancel()
        calendar_task, self._calendar_task = getattr(self, "_calendar_task", None), None
        if calendar_task is not None:
            calendar_task.cancel()
        sweep_task, self._turn_sweep_task = getattr(self, "_turn_sweep_task", None), None
        if sweep_task is not None:
            sweep_task.cancel()
        outbox_task, self._outbox_task = getattr(self, "_outbox_task", None), None
        if outbox_task is not None:
            outbox_task.cancel()

        await self._connection.stop()

    def _register_handlers(self, client: TelegramClient) -> None:
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        client.add_event_handler(self._on_callback, events.CallbackQuery())

    async def _set_commands(self) -> None:
        try:
            await self._client(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code="",
                commands=[types.BotCommand(name, description) for name, description in COMMANDS],
            ))
        except NETWORK_ERRORS as error:
            log.warning("telegram: список команд не установлен (%s)", type(error).__name__)

    async def _greet_manager(self) -> None:
        chat = self._settings.telegram_manager_chat_id
        if not chat:
            return
        try:
            await self._client.send_message(int(chat), MANAGER_STARTED_TEXT)
        except NETWORK_ERRORS as error:
            log.warning(
                "не смог отправить приветствие менеджеру: %s "
                "(бот не может писать первым — менеджеру нужно нажать /start у бота)",
                error,
            )

    # ---------- handlers ----------

    async def _role(self, chat_id: str, *, private: bool) -> str:
        """Роль — ОДИН раз на вход. Групповой чат прав не даёт (иначе участник стал бы владельцем)."""
        role = await resolve_role(chat_id, settings=self._settings, db=self._db)
        if role != CLIENT and not private:
            log.warning("чат %s не личный — права роли %s отключены (fail-closed)", chat_id, role)
            return CLIENT
        return role

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        self._note_update()
        contact = getattr(getattr(event, "message", None), "contact", None)
        if contact is not None:
            return await self._on_contact(event, contact, chat_id=str(event.chat_id))
        text = (event.raw_text or "").strip()
        if not text:
            return
        chat_id = str(event.chat_id)
        role = await self._role(chat_id, private=_is_private(event))
        # Флоу управления менеджерами (только владелец): команда или тап меню его снимают.
        flow = await self._owner_flows.active(chat_id) if role == OWNER else None

        # Антифлуд/стоимость: списывается ровно один раз на вход для ВСЕХ ролей и веток (, C-2).
        if not self._allow(chat_id):
            return await event.respond(TOO_OFTEN_TEXT)

        command = parse_command(text)
        if command is not None and flow:
            await self._owner_flows.clear(chat_id)
            flow = None
        if command == "start":
            return await self._start_dialog(event, role=role, chat_id=chat_id)
        # «согласен/согласна» текстом фиксирует согласие, пока его нет (); иначе — обычная реплика.
        if (command is None and role != OWNER and is_consent_command(text)
                and not await self._db.has_consent(platform="tg", chat_id=chat_id)):
            await self._db.save_consent(platform="tg", chat_id=chat_id, version="v1", allow_ai=True)
            return await self._start_dialog(event, role=role, chat_id=chat_id)
        if command == "stop":
            return await self._revoke_consent(event, role=role)
        if command == "privacy":
            return await self._reply_privacy(event)
        if command == "noai":
            return await self._disable_ai(event)
        if command == "pending":
            return await self._pending_command(event, chat_id=chat_id, role=role)
        if command is not None:
            return  # неизвестная команда — молча (агент её не видит)

        # Овнер-метки разбираем РАНЬШЕ клиентских: тап меню важнее текста.
        code = ui_menu.resolve_menu(text)
        if code:
            log.info("ui.tap code=%s chat=%s path=menu", code, chat_id)
            try:
                await self._db.bump_metric(metric="taps_total")
            except Exception as error:  # noqa: BLE001 — счётчик не должен ломать ответ на тап
                log.warning("метрика taps_total не записана: %s", type(error).__name__)
            if flow:
                # Владелец выбрал другой раздел — флоу снимаем, иначе текст съедался бы парсером ника.
                await self._owner_flows.clear(chat_id)
                flow = None
        if code in OWNER_CODE_RIGHTS:
            return await self._owner_menu(event, code=code, chat_id=chat_id, role=role)

        if text.strip() == ui_menu.REPEAT_LABEL:   # только «повтор»; прочие алиасы — через resolve_menu ()
            state = await self._load_state("tg", chat_id) or ChatState(platform="tg", chat_id=chat_id)
            log.info("ui.repeat chat=%s step=%s listing=%s slot=%s has_name=%s has_phone=%s offered=%d filt=%s",
                     chat_id, state.step, state.listing_id, state.slot,
                     bool(state.name), bool(state.phone), len(state.offered_slots), bool(state.client_filter))
            return await self._send(event, await self._dialog.repeat_catalog(state))

        if flow:
            # Шаг ввода ника: до клиентских кодов и до агента (иначе ник ушёл бы как реплика клиента).
            return await self._send(event, await self._owner_flows.handle_text(chat_id=chat_id, text=text))

        if code == ui_menu.MENU_APPLY:
            #: заявитель — клиентский путь ⇒ согласие/пауза те же (`_client_gate`).
            if not await self._client_gate(event, chat_id):
                return
            sender, profile_error = await self._sender_profile(event)
            return await self._send(event, await self._manager_requests.apply(
                chat_id=chat_id, sender=sender, profile_error=profile_error, role=role))

        if code in CLIENT_CODES:
            if not await self._client_gate(event, chat_id):
                return
            if code == ui_menu.MENU_MANAGER:
                # C3/: уведомление адресатам с идентификацией клиента (иначе «какой именно клиент?»)
                await self._notify_people(await self._call_request_text(event, chat_id=chat_id), actor=chat_id)
            reply = await self._menu.route(code, chat_id=chat_id, role=role)
            if reply is not None:
                if role != CLIENT:
                    reply.text = f"{CLIENT_PATH_NOTICE}\n{reply.text}"
                return await self._send(event, reply)

        #: ответ на напоминание о визите — до хода к агенту, потому что это ответ на
        # конкретный вопрос графа, а не свободный текст. Стоит и здесь, а не только ниже:
        # клиент может ответить и кодовым пунктом меню.
        if await self._visit_rewrite(text, chat_id=chat_id):
            return
        if await self._visit_answer(event, text, chat_id=chat_id):
            return
        return await self._turn(event, text, chat_id=chat_id)

    async def _start_dialog(self, event: events.NewMessage.Event, *, role: str, chat_id: str) -> None:
        pending = await self._pending_count()
        if role == MANAGER:
            # Менеджеру — инструкция и его меню, ДО клиентского согласия (он не клиент).
            await self._db.set_manager_ready(chat_id=chat_id, ready=True)
            log.info("manager.ready chat=%s path=start (карточки заявок теперь доходят)", chat_id)
            reply = Reply(text=MANAGER_INSTRUCTION, kind=REPLY,
                          rows=manager_menu_kb(pending, confirmed=await self._confirmed_count(chat_id)))
            return await self._send(event, reply)
        if role == OWNER:
            paused = (await self._db.setting("bot_mode", "live")) != "live"
            reply = Reply(
                text=f"{GREETING}\n\n{OWNER_START_NOTE}",
                rows=owner_menu_kb(pending=pending, paused=paused,
                                   confirmed=await self._confirmed_count()),
                kind=REPLY,
            )
            return await self._send(event, reply)
        if not await self._db.has_consent(platform="tg", chat_id=chat_id):
            return await self._ask_consent(event)
        if (await self._db.setting("bot_mode", "live")) != "live":
            return await event.respond(PAUSED_TEXT)
        return await self._client_entry(event, chat_id)

    async def _call_request_text(self, event, *, chat_id: str) -> str:
        """Текст уведомления о запросе звонка (): ВИДНО, кто именно просит, и как с ним связаться.

        Профиль и состояние читаются мягко: их сбой не должен отменять уведомление. Телефон попадает
        только в личный чат адресатам (как в карточке заявки), в лог — лишь факт наличия.
        """
        who = None
        try:
            sender, _ = await self._sender_profile(event)
            username = getattr(sender, "username", None)
            full = " ".join(
                part for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if part
            )
            who = f"@{username}" if username else (full or None)
        except Exception as error:  # noqa: BLE001 — без профиля уведомление всё равно полезно
            log.warning("профиль клиента для уведомления недоступен (%s)", type(error).__name__)
        state = None
        try:
            state = await self._load_state("tg", chat_id)
        except Exception as error:  # noqa: BLE001 — состояние тоже не критично
            log.warning("состояние клиента для уведомления недоступно (%s)", type(error).__name__)
        lines = ["📞 Запрос звонка от клиента", f"Кто: {who or 'профиль недоступен'} · чат {chat_id}"]
        if state and state.name:
            lines.append(f"Имя: {state.name}")       #: имя, которым клиент просит обращаться
        if state and state.phone:
            lines.append(f"Телефон: {state.phone}")
        if state and state.listing_id:
            try:
                address = fmt.address_for(state.listing_id, await self._listings())
            except Exception:  # noqa: BLE001 — адрес не обязателен
                address = None
            if address:
                lines.append(f"Объект: {address}")
        if state and state.slot:
            lines.append(f"Время: {format_slot_iso(state.slot, self._settings.tz)}")
        lines.append("Свяжитесь в рабочее время.")
        log.info("ui.call_request chat=%s has_phone=%s offer=%s",
                 chat_id, bool(state and state.phone), bool(state and state.slot))
        return "\n".join(lines)

    async def _confirmed_count(self, chat_id: str | None = None) -> int:
        """Подтверждённые заявки для кнопки «👁 Просмотры (N)»: менеджеру — свои, владельцу — все ()."""
        try:
            return await self._db.confirmed_count(chat_id=str(chat_id) if chat_id else None)
        except Exception as error:  # noqa: BLE001 — счётчик не должен ронять меню
            log.warning("счётчик подтверждённых недоступен: %s", type(error).__name__)
            return 0

    async def _pending_count(self) -> int:
        try:
            return await self._db.pending_count()
        except Exception as error:  # noqa: BLE001 — счётчик не должен ронять старт диалога
            log.warning("счётчик заявок недоступен: %s", type(error).__name__)
            return 0

    async def _client_gate(self, event, chat_id: str) -> bool:
        """Клиентский гейт (согласие + пауза) — по ВХОДУ, а не по роли: ПД не пишем без согласия."""
        if not await self._db.has_consent(platform="tg", chat_id=chat_id):
            await self._ask_consent(event)
            return False
        if (await self._db.setting("bot_mode", "live")) != "live":
            await event.respond(PAUSED_TEXT)
            return False
        if not await self._db.ai_allowed(platform="tg", chat_id=chat_id):
            # /noai: клиент запретил передачу данных ИИ — хода к агенту не будет (M7).
            await event.respond(NOAI_TEXT)
            return False
        return True

    async def _owner_menu(self, event, *, code: str, chat_id: str, role: str) -> None:
        capability = OWNER_CODE_RIGHTS[code]
        if not may(role, capability):
            return await event.respond(VIEW_ONLY if capability == "view_pending" else OWNER_ONLY)
        if code == ui_menu.MENU_OWNER_BOOKINGS:
            return await self._send_pending(event, chat_id=chat_id, role=role)
        if code == ui_menu.MENU_OWNER_MANAGERS:
            return await self._send(event, self._owner_flows.submenu(
                pending_requests=await self._db.pending_requests_count()))
        if code == ui_menu.MENU_OWNER_REQUESTS:
            return await self._send(event, await self._manager_requests.list_pending())
        if code == ui_menu.MENU_OWNER_VIEWS:
            await self._send(event, await self._views_reply(chat_id=chat_id, role=role))
            #: отметку явки нельзя повесить на reply-клавиатуру списка — она смешала бы
            # два механизма кнопок. Поэтому отметка идёт ОТДЕЛЬНЫМ сообщением со своими
            # подписанными кнопками, и только по прошедшим визитам без отметки.
            return await self._send_visit_marking(chat_id=chat_id, role=role)
        if code in (ui_menu.MENU_OWNER_PAUSE, ui_menu.MENU_OWNER_RESUME):
            paused = code == ui_menu.MENU_OWNER_PAUSE
            await self._db.set_setting("bot_mode", "paused" if paused else "live")
            log.info("owner.pause mode=%s chat=%s", "paused" if paused else "live", chat_id)
            await self._notify_people(
                ("⏸ Владелец поставил сервис на паузу: новые заявки не принимаются." if paused
                 else "▶️ Владелец вернул сервис в работу — заявки снова принимаются."),
                actor=chat_id,
            )
            reply = Reply(
                text=("⏸ Сервис на паузе: клиенты получают вежливый отказ, заявки не принимаются."
                      if paused else "▶️ Сервис снова принимает заявки."),
                rows=owner_menu_kb(pending=await self._pending_count(), paused=paused,
                                   confirmed=await self._confirmed_count()),
                kind=REPLY,
            )
            return await self._send(event, reply)
        if code == ui_menu.MENU_OWNER_REPORT:
            return await self._send(event, await self._report_reply())
        if code == ui_menu.MENU_OWNER_STATUS:
            return await self._send(event, await self._status_reply())
        return await event.respond(SECTION_LATER_TEXT)   # вне объёма: экспорт

    async def _pending_command(self, event, *, chat_id: str, role: str) -> None:
        if not may(role, "view_pending"):
            return await event.respond(VIEW_ONLY)
        return await self._send_pending(event, chat_id=chat_id, role=role)

    async def _client_entry(self, event, chat_id: str) -> None:
        """Вход клиента (согласие/`/start`/«согласен»): три сообщения и НИ ОДНОГО хода к агенту.

        Почему так: раньше здесь звался LLM (до ~17 с тишины, клиент не понимал, что происходит).
        Порядок по: приветствие с меню → короткая инструкция → каталог объектов кнопками.
        Состояние сбрасываем и СОХРАНЯЕМ (иначе старое `offered_*` давало бы ложные напоминания).
        """
        state = await self._load_state("tg", chat_id) or ChatState(platform="tg", chat_id=chat_id)
        state.step, state.listing_id, state.slot = "listing", None, None
        state.name, state.phone = None, None
        state.offered_slots, state.offered_at, state.offered_listing = [], 0.0, None
        state.offer_reminded_at, state.client_filter = 0.0, None
        listings = await self._listings()
        state.offered_listings = [item.id for item in listings]      # DD-1: порядок показа
        await self._save_state(state)

        note = await self._menu.limit_note(len(listings))
        messages = (
            Reply(text=CLIENT_GREETING, rows=client_menu_kb(), kind=REPLY),
            Reply(text=CLIENT_HOWTO, kind=REPLY),
            await self._dialog.offer_catalog(listings, note=note),
        )
        for message in messages:
            try:
                await self._send(event, message)
            except NETWORK_ERRORS as error:
                # Частичный флоу недопустим: пишем в лог и продолжаем остальными сообщениями (DD-5).
                log.warning("сообщение входа клиента не отправлено (%s)", type(error).__name__)

    async def _ask_consent(self, event: events.NewMessage.Event) -> None:
        reply = Reply(text=CONSENT_TEXT, rows=consent_kb(), kind=INLINE)
        await render.send(event, reply, secret=self._ui_secret, chat_id=str(event.chat_id))

    async def _turn(self, event: events.NewMessage.Event, text: str, *, chat_id: str) -> None:
        if not await self._client_gate(event, chat_id):
            return

        async with self._locks[chat_id]:
            typing = asyncio.create_task(self._keep_typing(event.chat_id))
            attributes = telemetry.span_attributes(
                workflow_id=self._settings.otel_workflow_id,
                workflow_slug=self._settings.otel_workflow_slug,
                chat_id=chat_id,
                step="message",
            ) if self._tracer else {}
            try:
                with (telemetry.span(self._tracer, "turn", attributes),
                      telemetry.span(self._tracer, "dialog.handle")):
                    reply = await self._dialog.handle_message(platform="tg", chat_id=chat_id, text=text)
            # Внешний контур: падение хода не должно оставлять клиента без ответа.
            except Exception as error:
                log.exception("ход не обработан (%s)", type(error).__name__)
                reply = Reply(text=FAILOVER_TEXT)
            finally:
                typing.cancel()
            try:
                await self._send(event, reply)
            except NETWORK_ERRORS as error:
                log.warning("ответ не отправлен: %s", error)
            # Сборка клавиатуры/рендер (Telegram тоже может отказать).
            except Exception as error:
                log.exception("рендер ответа упал (%s)", type(error).__name__)
                await event.respond(FAILOVER_TEXT)

    async def _send(self, event, reply) -> None:
        """Единственная точка отправки ответа хода (слой привязки к Telegram — `render`)."""
        with telemetry.span(self._tracer, "send"):
            await render.send(event, reply, secret=self._ui_secret, chat_id=str(event.chat_id))

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        self._note_update()
        data = (event.data or b"").decode()
        chat_id = str(event.chat_id)

        if data == render.POLICY_CALLBACK:
            # Без подписи и без состояния: показ статичного текста (кнопка живёт и без UI_SECRET).
            await event.answer()
            return await self._reply_privacy(event)

        unsigned = (render.CONSENT_CALLBACK, render.POLICY_CALLBACK)
        if not self._ui_secret and data not in unsigned:
            # Кнопок без секрета не бывает: подписанный callback принимать нечем.
            return await event.answer()

        if data == render.CONSENT_CALLBACK:
            await event.answer()
            async with self._locks[chat_id]:
                await self._db.save_consent(platform="tg", chat_id=chat_id, version="v1", allow_ai=True)
                if (await self._db.setting("bot_mode", "live")) != "live":
                    # M8: на паузе ход не начинаем, согласие уже зафиксировано
                    return await event.respond(PAUSED_TEXT)
                await self._drop_buttons(event)
                # DC-1/DC-2: согласие больше не зовёт агента — сразу три сообщения (0 LLM, без тишины)
                return await self._client_entry(event, chat_id)

        if data.startswith(f"{tokens.PREFIX}:"):
            return await self._owner_callback(event, data=data, chat_id=chat_id)

        parsed = verify_callback(self._ui_secret, data, chat_id)
        if parsed is None:
            return await event.answer()
        role = await self._role(chat_id, private=_is_private(event))
        booking_id, action = parsed
        #: кнопки визита разбираются до решений по броне — у них свои права и свой путь
        # (менеджер одобряет текст, клиент отвечает, менеджер отмечает явку).
        if action in VISIT_ACTIONS:
            return await self._visit_callback(
                event, booking_id=booking_id, action=action, chat_id=chat_id)
        if not may(role, "confirm"):
            return await event.answer(CONFIRM_ONLY, alert=True)
        status = status_for_action(action)
        if status is None:
            return await event.answer()
        async with self._locks[chat_id]:
            booking = await self._db.set_booking_status(
                booking_id=booking_id, status=status, manager_chat_id=chat_id, role=role
            )
        if booking is None:
            return await event.answer("Заявка уже обработана", alert=True)
        word = "подтверждена" if status == "confirmed" else "отклонена"
        await event.answer(f"Заявка #{booking_id} {word}")          #: номер заявки виден явно
        await self._drop_buttons(event)
        # DD-6: менеджер должен ВИДЕТЬ в чате, какую заявку он подтвердил (тоста недостаточно)
        try:
            #: номер заявки подставляем в шаблон, объект показываем АДРЕСОМ (id — только резерв)
            template = BOOKING_CONFIRMED_MSG if status == "confirmed" else BOOKING_REJECTED_MSG
            address = fmt.address_label(booking.get("listing_id"), await self._listings())
            await event.respond(
                f"{template.format(id=booking['id'])}"
                f"\n{format_slot(booking['slot'], self._settings.tz)} · {address}"
                f" · {booking.get('client_name') or '—'} ({booking.get('client_phone') or '—'})"
            )
        except NETWORK_ERRORS as error:
            log.warning("сообщение о решении не отправлено: %s", error)
        log.info("ui.booking_decided chat=%s booking=%s status=%s", chat_id, booking_id, status)
        await self._refresh_menu(chat_id, reason="decide")
        client_chat = booking.get("chat_id")
        if client_chat and booking.get("platform") == "tg":
            text = client_notification_text(
                status=status,
                slot=format_slot(booking["slot"], self._settings.tz),
                address=fmt.address_label(booking.get("listing_id"), await self._listings()),
            )
            # Через очередь: клиент должен узнать решение даже если мост в этот момент моргнул.
            if not await self._enqueue_notice(chat_id=str(client_chat), text=text):
                log.warning("клиенту не отправлено (нет ни очереди, ни транспорта): заявка #%s", booking_id)

    async def _owner_callback(self, event, *, data: str, chat_id: str) -> None:
        """Тапы овнер-меню (подписаны UI-секретом). Роль — из личного чата, согласие не требуется."""
        parsed = tokens.verify(self._ui_secret, data, chat_id=chat_id, token="")
        if parsed is None:
            return await event.answer()
        kind, arg = parsed
        role = await self._role(chat_id, private=_is_private(event))
        if kind == KIND_CANCEL and arg == "flow":
            await event.answer()
            return await self._send(event, await self._owner_flows.cancel_reply(chat_id))
        if kind in CLIENT_KINDS:
            return await self._client_callback(event, kind=kind, arg=arg, chat_id=chat_id)
        if kind in APPLY_KINDS:
            return await self._apply_callback(event, kind=kind, arg=arg, chat_id=chat_id, role=role)
        if kind not in OWNER_KINDS:
            return await event.answer()          # клиентские виды —
        if not may(role, "manage_managers"):
            log.warning("owner-меню: отказ (chat=%s role=%s kind=%s)", chat_id, role, kind)
            return await event.answer(MANAGERS_ONLY_OWNER, alert=True)
        await event.answer()
        if kind == KIND_OWNER:
            if arg == "requests":               # «📥 Заявки в менеджеры (N)» из подменю
                return await self._send(event, await self._manager_requests.list_pending())
            if arg == "add":
                return await self._send(event, await self._owner_flows.add_ask(chat_id))
            if arg == "list":
                return await self._send(event, await self._owner_flows.list_reply())
            return await self._send(event, await self._owner_flows.pick_reply())
        if kind == KIND_OWNER_PICK:
            return await self._send(event, await self._owner_flows.confirm_reply(arg))
        reply = await self._owner_flows.remove(target=arg, actor=chat_id)
        await self._drop_buttons(event)
        return await self._send(event, reply)

    async def _client_callback(self, event, *, kind: str, arg: str, chat_id: str) -> None:
        """Кнопочный клиентский путь: LLM не вызывается ни разу ().

        Под per-chat lock: двойной тап по одной кнопке не должен создавать две брони ( п.3).
        """
        async with self._locks[chat_id]:
            await self._client_callback_locked(event, kind=kind, arg=arg, chat_id=chat_id)

    async def _client_callback_locked(self, event, *, kind: str, arg: str, chat_id: str) -> None:
        if not await self._client_gate(event, chat_id):      # согласие + пауза — как у текстового пути
            return
        await event.answer()                                 # спиннер гасим ДО тяжёлых операций ()
        state = await self._load_state("tg", chat_id) or ChatState(platform="tg", chat_id=chat_id)
        index = int(arg) if str(arg).isdigit() else -1
        log.info("ui.tap code=%s chat=%s path=client_button", kind, chat_id)
        if kind == KIND_LISTING:
            reply = await self._dialog.pick_listing(state, index)
        elif kind == KIND_DAY:
            reply = await self._dialog.pick_day(state, index)
        elif kind == KIND_BACK:
            reply = await self._dialog.pick_day(state, 0, back=True)
        elif kind == KIND_TIME:
            reply = await self._dialog.pick_time(state, index)
        elif kind == KIND_CONFIRM:
            reply = await self._dialog.confirm_booking(state)
        elif kind == KIND_FILTER_RESET:
            reply = await self._dialog.filter_reset(state)
        elif kind == KIND_CANCEL:
            reply = await self._dialog.cancel_offer(state)
        elif kind == KIND_EDIT and not arg:
            #: сначала подменю «что меняем», затем правка выбранного поля
            reply = Reply(text="Что изменить?", rows=edit_menu_kb(), kind=INLINE)
        else:                                                # KIND_EDIT с полем: правка одного поля
            reply = await self._dialog.edit_field(state, arg or "phone")
        return await self._send(event, reply)

    async def _apply_callback(self, event, *, kind: str, arg: str, chat_id: str, role: str) -> None:
        """Тапы потока заявок: перепроверка профиля (заявитель) и решение (владелец)."""
        if kind == KIND_APPLY:                      # «🔄 Проверить снова» — тот же заявительский путь
            if not self._allow(chat_id):
                return await event.answer(TOO_OFTEN_TEXT, alert=True)
            sender, profile_error = await self._sender_profile(event)
            await event.answer()
            return await self._send(event, await self._manager_requests.apply(
                chat_id=chat_id, sender=sender, profile_error=profile_error, role=role))
        if not may(role, "manage_managers"):        # H4: у веток своя проверка прав (они выше OWNER_KINDS)
            log.warning("заявка: отказ по правам (chat=%s role=%s kind=%s)", chat_id, role, kind)
            return await event.answer(MANAGERS_ONLY_OWNER, alert=True)
        if kind == KIND_APPLY_PICK:                 # строка списка → карточка заявки с ✅/❌
            card = await self._manager_requests.request_card(arg)
            await event.answer()
            return await self._send(event, card)
        status = "approved" if kind == KIND_APPLY_OK else "rejected"
        await event.answer(await self._manager_requests.decide(request_id=arg, status=status, actor=chat_id))
        if kind == KIND_APPLY_NO:
            await self._drop_buttons(event)

    async def _reply_privacy(self, event) -> None:
        """Единственный путь к тексту политики: команда `/privacy` и кнопка «📄 Политика»."""
        await event.respond(PRIVACY_TEXT)

    async def _drop_buttons(self, event: events.CallbackQuery.Event) -> None:
        """Убираем клавиатуру проигравшим копиям заявки и защищаемся от повторных тапов."""
        try:
            await event.edit(buttons=None)
        except NETWORK_ERRORS as error:
            log.debug("кнопки не убраны: %s", error)

    async def _revoke_consent(self, event: events.NewMessage.Event, *, role: str) -> None:
        await self._db.revoke_consent(platform="tg", chat_id=str(event.chat_id))
        text = STOP_TEXT if role == CLIENT else f"{STOP_TEXT}\n{MANAGER_STOP_NOTE}"
        await event.respond(text)

    async def _disable_ai(self, event: events.NewMessage.Event) -> None:
        await self._db.save_consent(
            platform="tg", chat_id=str(event.chat_id), version="v1-noai", allow_ai=False
        )
        await event.respond(NOAI_TEXT)

    async def _send_pending(self, event, *, chat_id: str, role: str) -> None:
        """Очередь заявок: и `/pending`, и метка «📋 Заявки (N)» — один путь, без LLM."""
        try:
            await self._db.log_manager_action(chat_id=chat_id, role=role, action="view_pending")
        except Exception as error:  # noqa: BLE001 — журнал ПД не должен закрывать саму очередь
            log.warning("аудит просмотра очереди не записан: %s", type(error).__name__)
        bookings = await self._db.pending_bookings()
        if not bookings:
            return await event.respond(EMPTY_QUEUE_TEXT)
        listings = await self._listings()
        for booking in bookings:
            await event.respond(
                f"Заявка #{booking['id']}: {format_slot(booking['slot'], self._settings.tz)}, "
                f"объект {fmt.address_label(booking.get('listing_id'), listings)}, "   #: адрес, не id
                f"{booking['client_name']}, {booking['client_phone']}",
                buttons=self._booking_buttons(booking["id"], chat_id),
            )

    def _callback_buttons(self, *, booking_id, chat_id: str,
                          actions: list[tuple[str, str]]) -> list | None:
        """ЕДИНСТВЕННЫЙ построитель подписанных кнопок.

        Раньше подпись собиралась в двух почти одинаковых местах (`_booking_buttons`, `_visit_buttons`),
        а очередь добавила бы третье — и формат разошёлся бы с тем, что разбирает `_on_callback`. Один
        построитель делает расхождение невозможным: и прямой путь, и отправка из очереди зовут его же.
        Кнопка подписана ЧАТОМ (`sign_callback`), поэтому кнопка менеджера не сработает в чате клиента и
        наоборот — это и есть проверка адресата.
        """
        if not self._ui_secret:
            return None
        return [
            Button.inline(label, sign_callback(self._ui_secret, int(booking_id), action, chat_id))
            for action, label in actions
        ]

    def _booking_buttons(self, booking_id, chat_id: str) -> list | None:
        """Без UI-секрета кнопки не подписываем: карточка уходит без ✅/❌."""
        return self._callback_buttons(booking_id=booking_id, chat_id=chat_id,
                                      actions=[("c", "✅ Подтвердить"), ("r", "❌ Отклонить")])

    # ---------- напоминания о визите ---------- #

    def _visit_buttons(self, booking_id: int, chat_id: str, actions: list[tuple[str, str]]) -> list | None:
        """Кнопки визита — той же подписью, что решение по броне (`sign_callback`).

        Один построитель на все действия: подпись обязана совпадать по формату с той,
        которую разбирает `_on_callback`. Кнопка подписана ЧАТОМ, поэтому кнопка менеджера
        не сработает в чате клиента и наоборот — это и есть проверка адресата.
        """
        return self._callback_buttons(booking_id=booking_id, chat_id=chat_id, actions=actions)

    async def _visit_resume(self, booking_id: int, payload: dict) -> str:
        """Продолжить поток графа — в ЕГО процессе. Возвращает `"resumed"`, `"absent"` или `"busy"`.

        Граф живёт своей службой (свой пул, свой чекпойнтер, свой рестарт), а сессия Telegram остаётся
        у моста, поэтому продолжение идёт HTTP-ручкой. Проверка «есть ли поток» тоже на той стороне:
        нажатие кнопки из истории чата не должно создавать поток, которого нет, иначе клиент получил бы
        напоминание «из ниоткуда». Здесь только транспорт — решения принимает владелец состояния.

        ❗ Три исхода, а не два. «Занято» (поток исполняет планировщик) раньше было неотличимо от
        «потока нет», и человек получал ложное «напоминание не ведётся», а фраза клиента исчезала молча.
        """
        answer = await self._visit_process_post("/api/ops/visit/resume",
                                               {"booking_id": int(booking_id), "payload": payload})
        if not answer:
            return VISIT_ABSENT
        if answer.get("status") == "busy":
            return VISIT_BUSY_STATUS
        return VISIT_RESUMED if answer.get("resumed") else VISIT_ABSENT

    async def _visit_process_post(self, path: str, body: dict) -> dict | None:
        """Запрос к процессу графа. `None` — процесс недоступен (это НЕ ошибка пути клиента).

        Недоступность графа логируется и возвращает `None`: напоминания — отдельная функция, и её
        падение не должно превращаться в ошибку ответа клиенту.
        """
        import httpx

        url = f"http://{self._settings.bridge_graph_host}:{self._settings.bridge_graph_port}{path}"
        try:
            async with httpx.AsyncClient(timeout=VISIT_PROCESS_TIMEOUT_S) as client:
                response = await client.post(
                    url, json=body,
                    headers={"X-API-Key": self._settings.bridge_api_key or ""})
        except Exception as error:  # noqa: BLE001 — недоступный граф не должен ронять мост
            log.warning("граф: процесс недоступен (%s: %s)", type(error).__name__, error)
            return None
        if response.status_code == 401:
            log.error("граф: процесс отверг ключ моста (401) — проверьте BRIDGE_API_KEY")
            return None
        if response.status_code >= 300:
            log.warning("граф: ручка %s ответила %s", path, response.status_code)
            return None
        return response.json()

    async def _visit_process_get(self, path: str, params: dict) -> dict | None:
        """GET к процессу графа — тем же ключом и той же деградацией, что и POST."""
        import httpx

        url = f"http://{self._settings.bridge_graph_host}:{self._settings.bridge_graph_port}{path}"
        try:
            async with httpx.AsyncClient(timeout=VISIT_PROCESS_TIMEOUT_S) as client:
                response = await client.get(
                    url, params=params,
                    headers={"X-API-Key": self._settings.bridge_api_key or ""})
        except Exception as error:  # noqa: BLE001 — недоступный граф не должен ронять мост
            log.warning("граф: процесс недоступен (%s: %s)", type(error).__name__, error)
            return None
        if response.status_code >= 300:
            log.warning("граф: ручка %s ответила %s", path, response.status_code)
            return None
        return response.json()

    async def _visit_callback(self, event, *, booking_id: int, action: str, chat_id: str) -> None:
        """Кнопки визита: одобрение и правка у менеджера, ответ и отметка явки у клиента."""
        role = await self._role(chat_id, private=_is_private(event))
        needs_manager = action in VISIT_MANAGER_ACTIONS or action in VISIT_MARK_ACTIONS
        if needs_manager and not may(role, "confirm"):
            return await event.answer(CONFIRM_ONLY, alert=True)

        if action in VISIT_MARK_ACTIONS:
            marked = await self._db.set_visit_result(
                booking_id=booking_id, result=VISIT_MARK_ACTIONS[action], manager_chat_id=chat_id, role=role)
            if marked is None:
                return await event.answer(VISIT_MARK_ONLY, alert=True)
            await event.answer(VISIT_MARKED[VISIT_MARK_ACTIONS[action]])
            await self._drop_buttons(event)
            log.info("ui.visit_marked chat=%s booking=%s result=%s", chat_id, booking_id,
                     VISIT_MARK_ACTIONS[action])
            return

        if action == "vr":
            # Подсказка живёт в памяти и может пропасть при перезапуске — это названо в
            # описании поля: состояние ПОТОКА переживает перезапуск, «менеджер печатает» нет.
            self._rewrite_pending[booking_id] = chat_id
            return await event.answer(VISIT_REWRITE_HINT, alert=True)

        if action == "va":
            payload, ack = resume_manager(decision="approve"), VISIT_SENT_TO_CLIENT
        else:
            payload, ack = resume_client(answer=VISIT_CLIENT_ACTIONS[action]), VISIT_ANSWER_ACK[VISIT_CLIENT_ACTIONS[action]]
        status = await self._visit_resume(booking_id, payload)
        if status == VISIT_BUSY_STATUS:
            # Поток исполняет планировщик: повтор через минуту, а не «напоминание не ведётся».
            return await event.answer(VISIT_BUSY, alert=True)
        if status != VISIT_RESUMED:
            return await event.answer(VISIT_NO_FLOW, alert=True)
        await event.answer(ack)
        await self._drop_buttons(event)

    async def _visit_answer(self, event, text: str, *, chat_id: str) -> bool:
        """Ответ клиента короткой фразой — вместо хода диалога.

        Перехватываем ТОЛЬКО когда поток этого чата ждёт ответа и фраза входит в закрытый
        список (`realty_agent/booking/visits.py`). В остальных случаях возвращаем `False`, и
        сообщение уходит агенту как обычно: второго FSM рядом с диалогом быть не должно.

        Порядок проверок важен: сначала дешёвые (фраза, поток), и лишь потом гейт согласия.
        Гейт отправляет сообщения (согласие/пауза/`noai`), поэтому вызывать его на каждое
        сообщение-кандидата нельзя — только когда граф действительно забирает ответ.
        """
        kind = classify_answer(text)
        # Механизм проверяем по настройке, а НЕ по наличию графа в этом процессе: граф вынесен своей
        # службой, и прежняя проверка `self._visit_graph` здесь навсегда отключила бы приём
        # фраз клиента (локального графа больше нет по построению).
        if kind is None or not self._settings.visit_reminder_enabled:
            return False
        booking_id = await self._visit_waiting_booking(chat_id=chat_id, awaiting=AWAIT_CLIENT)
        if booking_id is None:
            return False
        if not await self._client_gate(event, chat_id):
            return True   # гейт уже ответил клиенту; второй раз звать ход нельзя
        status = await self._visit_resume(booking_id, resume_client(answer=kind))
        if status != VISIT_RESUMED:
            # ❗ Раньше результат здесь НЕ проверялся: фраза клиента исчезала молча, и ни он, ни
            # менеджер не узнавали, что ответ не дошёл. Теперь клиенту сказано, а случай назван в журнале.
            log.warning("граф: ответ клиента %s не доставлен (поток %s)", chat_id, status)
            await self._send_to(chat_id, VISIT_BUSY if status == VISIT_BUSY_STATUS else VISIT_NO_FLOW)
            return True
        log.info("граф: ответ клиента %s принят фразой (%s)", chat_id, kind)
        return True

    async def _visit_rewrite(self, text: str, *, chat_id: str) -> bool:
        """Менеджер прислал новый текст напоминания после кнопки «Переписать»."""
        pending = [bid for bid, who in self._rewrite_pending.items() if who == chat_id]
        if not pending or not self._settings.visit_reminder_enabled:
            return False
        booking_id = pending[-1]
        self._rewrite_pending.pop(booking_id, None)
        if await self._visit_resume(booking_id, resume_manager(decision="revise", text=text)) != VISIT_RESUMED:
            return False
        log.info("граф: менеджер %s переписал текст по броне %s", chat_id, booking_id)
        return True

    async def _visit_waiting_booking(self, *, chat_id: str, awaiting: str) -> int | None:
        """Поток этого чата, который сейчас ждёт ответа (`awaiting`) — иначе `None`.

        Ожидание определяется по ПРЕРЫВАНИЮ (`pending_expect`), а не по полю состояния:
        узел, вставший в прерывание, своего `awaiting` записать не успевает. Ошибка была
        бы не косметической — фраза клиента просто не нашла бы свой поток.

        Ищем от кандидатов базы, а не по всем потокам: у чекпойнтера нет «списка потоков»,
        а у нас есть точный список подтверждённых визитов без отметки.
        """
        # Состояние живёт у процесса графа: спрашиваем владельца, а не читаем чужой чекпойнтер
        # (иначе ресурсы графа «протекли» бы обратно в мост, и вынос был бы только на бумаге).
        answer = await self._visit_process_get("/api/ops/visit/waiting",
                                              {"chat_id": str(chat_id)})
        if not answer:
            return None
        booking_id = answer.get("booking_id")
        if booking_id is None or str(answer.get("awaiting") or "") != str(awaiting):
            return None
        return int(booking_id)

    # ---------- DialogService wiring ----------

    async def _ask_agent(self, user: str, text: str) -> AgentReply:
        log.info("llm.turn user=%s", user)
        await self._db.bump_metric(metric="llm_turns")
        with telemetry.span(self._tracer, "channel.ask"):
            reply = await self._channels.ask(user=f"tg-{user}", text=text, wait_s=60.0)
        # Токены и стоимость платформа присылает вместе с ответом (запись `usage`) — раньше они
        # выбрасывались здесь, и в отчёте владельцу стояло «нет данных».
        return AgentReply(text=reply.text, done=reply.done, usage=reply.usage,
                          session_id=getattr(reply, "session_id", ""))

    async def _listings(self) -> list[Listing]:
        rows = await self._db.active_listings(limit=6)
        return [Listing(id=str(r["id"]), address=r["address"], price=str(r.get("price") or ""), rooms=str(r.get("rooms") or "")) for r in rows]

    async def _free_slots(self, listing_id: str | None) -> list[str]:
        """Свободные слоты.

        Расчёт вынесен в `realty_agent/booking/slots.py`: тот же результат нужен MCP-инструменту
        `get_available_slots`, и вторая копия расписания неизбежно разошлась бы с первой —
        клиент через бота и клиент через инструмент увидели бы разное время.
        """
        return await free_slots(self._db, tz=self._settings.tz, listing_id=listing_id)

    async def _slot_free(self, listing_id: str, slot: str) -> bool:
        """slot из BOOKING-блока проверяется на занятость (формат ISO нормализует DB-слой)."""
        return not await self._db.slot_taken(listing_id=listing_id, slot_iso=slot)

    async def _save_booking(self, booking: Booking) -> None:
        row = await self._db.create_booking(
            listing_id=booking.listing_id,
            slot_iso=booking.slot,
            name=booking.client_name,
            phone=booking.client_phone,
            platform=booking.platform,
            chat_id=booking.chat_id,
            raw_block=booking.listing_id,
        )
        booking.id = str(row["id"])
        try:
            await self._db.bump_metric(metric="bookings_created")
        except Exception as error:  # noqa: BLE001 — метрика — не повод терять бронь
            log.warning("метрика bookings_created не записана: %s", type(error).__name__)

    async def _card_recipients(self) -> set[str]:
        """Овнер ∪ активные менеджеры ∪ legacy-чат, БЕЗ дублей: в живом конфиге owner == manager."""
        ids = {
            str(self._settings.telegram_owner_chat_id or ""),
            str(self._settings.telegram_manager_chat_id or ""),
        }
        try:
            ids.update(await self._db.manager_recipients())
        except Exception as error:  # noqa: BLE001 — БД недоступна: шлём хотя бы на настроенные чаты
            log.warning("адресаты менеджеров недоступны: %s", type(error).__name__)
        return {item for item in ids if item}

    async def alert_ops(self, text: str) -> int:
        """Служебный алерт владельцу и активным менеджерам (зовёт внешний сторож). Число ПРИНЯТЫХ.

        **Почему через очередь.** Сторож зовёт эту ручку, когда уже что-то не так. Раньше алерт
        уходил прямым вызовом: если в этот момент мост умирал или Telegram отвечал лимитом, сообщение
        терялось — а история проекта знает эпизоды, когда падение моста как раз и не замечали. Очередь
        меняет поведение: строка записана и будет доставлена воркером, в том числе после перезапуска.

        **Фолбэк остаётся, и он не формальность.** Алерт нужен как раз тогда, когда база может быть
        недоступна (сторож проверяет в том числе её). Не удалось поставить в очередь — отправляем
        напрямую, как раньше: «доставить сейчас без гарантии» лучше, чем «не доставить вовсе».
        """
        sent = 0
        for recipient in sorted(await self._card_recipients()):
            if await self._enqueue_notice(chat_id=recipient, text=f"⚠️ Мост: {text}"):
                sent += 1
        return sent

    async def _enqueue_notice(self, *, chat_id: str, text: str, rows=None, buttons=None,
                              booking_id: int | None = None, kind: str = REPLY) -> bool:
        """Поставить исходящее в очередь; если очередь недоступна — отправить напрямую.

        Возвращает `True`, если исходящее ПРИНЯТО (в очередь или к прямой отправке). Это единственное
        место, где решается «очередь или прямой путь»: разложи это по семнадцати швам — и разъедутся
        правила, а не поведение доставки.
        """
        payload = {"chat_id": str(chat_id), "text": text, "kind": kind,
                   "rows": rows_to_payload(rows)}
        if buttons:
            # Второй вид кнопок: ДЕСКРИПТОРЫ, а не готовые объекты Telethon. Готовые `Button.inline` в
            # payload не положить — они не сериализуются, и карточка уехала бы БЕЗ кнопок (регресс
            # работающей функции). Подпись восстанавливается тем же `sign_callback`.
            payload["buttons"] = [{"label": label, "action": action} for action, label in buttons]
            payload["booking_id"] = int(booking_id or 0)
        if self._db is not None:
            try:
                await self._db.enqueue_outbox(kind=kind, payload=payload)
                self._wake_outbox()
                return True
            except Exception as error:  # noqa: BLE001 — недоступная очередь не теряет сообщение
                log.warning("outbox: постановка не удалась (%s: %s) — отправляю напрямую",
                            type(error).__name__, error)
        return await self._send_to(chat_id, text, rows=rows, kind=kind)

    async def _event_message(self, item: calendar_connector.CalendarEvent) -> str:
        """Текст уведомления о событии: формулирует агент, факты — из шаблона.

        Агент получает ГОТОВЫЕ факты и возвращает одну короткую строку. Если ответ пуст, длиннее лимита
        или потерял время события — отдаём шаблон: выдумка или сбой до человека не доходят.
        """
        template = calendar_connector.format_event(item, tz=self._settings.tz)
        if not self._settings.calendar_agent_text:
            return template
        prompt = (
            "Сформулируй ОДНУ короткую строку-напоминание о событии для Telegram (до 200 знаков, можно с эмодзи). "
            "Используй только эти факты, ничего не добавляй и не меняй:\n"
            f"{template}\n"
            "Верни только текст напоминания — без пояснений и кавычек."
        )
        try:
            reply = await self._channels.ask(
                user=f"calendar-notify-{item.uid}",
                text=prompt,
                wait_s=CALENDAR_TEXT_WAIT_S,
                strict_user=True,  # чужую сессию читать нельзя: уведомление не должно взять реплику клиента
            )
        except Exception as error:  # noqa: BLE001 — сбой формулировки не должен терять уведомление
            log.warning("calendar: агент не сформулировал текст (%s) — уходим на шаблон", type(error).__name__)
            return template
        text = (reply.text or "").strip()
        if not reply.done or not text:
            log.info("calendar: агент не ответил за %.0f с — уходим на шаблон", CALENDAR_TEXT_WAIT_S)
            return template
        if not self._agent_text_ok(text, item, tz=self._settings.tz):
            log.info("calendar: ответ агента не прошёл проверку фактов — уходим на шаблон")
            return template
        return text

    @staticmethod
    def _agent_text_ok(text: str, item: calendar_connector.CalendarEvent, *, tz) -> bool:
        """Ответ агента годен только если он не пуст, не длиннее лимита и сохранил время события."""
        if not text or len(text) > CALENDAR_TEXT_MAX_CHARS:
            return False
        local = item.starts_at.astimezone(tz)
        stamp = "весь день" if item.all_day else local.strftime("%H:%M")
        return stamp in text

    async def preview_calendar_message(self, *, summary: str, in_hours: float, location: str = "") -> dict:
        """Собрать текст уведомления на синтетических фактах — проверка/демо, ничего не отправляется."""
        start = dt.datetime.now(dt.UTC) + dt.timedelta(hours=in_hours)
        item = calendar_connector.CalendarEvent(
            uid=f"preview-{int(start.timestamp())}", summary=summary, location=location, starts_at=start
        )
        return {
            "template": calendar_connector.format_event(item, tz=self._settings.tz),
            "message": await self._event_message(item),
        }

    async def sync_calendar(self) -> dict:
        """Один проход «календарь → события ближайших часов → уведомление новых».

        Дедуп по (uid, начало) живёт в БД, поэтому проход раз в `calendar_poll_s` секунд
        не отправляет одно событие дважды. Пустой ICS-адрес ⇒ механизм выключен.
        """
        url = self._settings.calendar_ics_url
        if not url:
            return {"enabled": False, "events": 0, "notified": 0, "delivered": 0}
        raw = await calendar_connector.fetch_ics(url)
        events = calendar_connector.upcoming(
            raw, ahead_h=self._settings.calendar_notify_ahead_h, tz=self._settings.tz
        )
        seen = await self._db.calendar_seen_keys()
        fresh = [item for item in events if (item.uid, item.starts_at) not in seen]
        delivered = 0
        for item in fresh:
            text = await self._event_message(item)
            for recipient in sorted(await self._card_recipients()):
                delivered += 1 if await self._send_to(recipient, text) else 0
        await self._db.mark_calendar_seen(fresh)
        if fresh:
            log.info("calendar: новых событий %d, доставок %d", len(fresh), delivered)
        return {"enabled": True, "events": len(events), "notified": len(fresh), "delivered": delivered}

    async def _calendar_loop(self) -> None:
        """Периодический проход по календарю: ошибка одного прохода не убивает цикл."""
        while True:
            try:
                await self.sync_calendar()
            except Exception as error:  # noqa: BLE001 — проход не должен убивать цикл
                # Текст ошибки httpx содержит URL календаря (а он — секрет) ⇒ пишем только тип.
                log.warning("calendar: проход не удался (%s)", type(error).__name__)
            # ❗ Чтение настройки — ВНУТРИ try. Снаружи любое исключение при её вычислении (поля нет,
            # значение не число) убивало петлю МОЛЧА и навсегда: перезапуска у петли нет, а
            # `task.exception()` до не читал никто, поэтому синхронизация просто исчезала.
            try:
                await asyncio.sleep(max(60, self._settings.calendar_poll_s))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — непригодная настройка не должна убивать петлю
                log.error("calendar: пауза непригодна (%s: %s) — беру 60 с", type(error).__name__, error)
                await asyncio.sleep(60)

    async def _turn_sweep_loop(self) -> None:
        """Периодически разбирает ходы с просроченным lease.

        **Залипший ход НЕ переигрывается** — этим и отличается честная durable-механика от
        «повторим на всякий случай»: если ответ мог уйти клиенту, повтор создал бы второй
        ответ. Ход помечается `failed` на разбор; повтор безопасен только для `created`.
        """
        while True:
            try:
                await asyncio.sleep(TURN_SWEEP_TICK_S)
                resolved = await self._turns.sweep()
                if resolved:
                    log.warning("turns: разобрано залипших ходов: %d", len(resolved))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — sweeper не должен ронять транспорт
                log.warning("turns: сбой прохода sweeper (%s: %s)", type(error).__name__, error)

    async def _offer_reminder_loop(self) -> None:
        """Раз в минуту напоминает клиентам, выбравшим время, но не подтвердившим ()."""
        while True:
            try:
                await asyncio.sleep(REMINDER_TICK_S)
                await self._remind_pending_offers()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — цикл напоминаний не должен умирать от сбоя
                log.warning("напоминания: сбой итерации (%s: %s)", type(error).__name__, error)

    async def _remind_pending_offers(self) -> None:
        """Одно напоминание на оффер: «Ещё актуально?» + [Показать время] [Отменить].

        **Через очередь — и вот честная смена семантики.** Раньше флаг «напомнили»
        (`mark_offer_reminded`) ставился ПОСЛЕ успешной отправки, а сбой отправки оставлял его снятым,
        и напоминание повторялось на следующем проходе. Теперь флаг ставится при ПРИЁМЕ в очередь, а
        временные сбои доставки повторяет воркер — у него для этого есть задержка и предел попыток. При
        постоянном отказе (клиент заблокировал бота) напоминание теряется: оффер всё равно истекает по
        `OFFER_TTL_S`, и гоняться за недостижимым адресатом смысла нет.
        """
        if self._client is None and self._db is None:
            return
        rows = await self._db.chats_with_pending_offer(
            min_age_s=OFFER_REMIND_AFTER_S, max_age_s=OFFER_TTL_S)
        for row in rows:
            chat_id = str(row["chat_id"])
            state = await self._load_state("tg", chat_id)
            if state is None or not state.offered_slots:
                continue
            reply = self._dialog.reminder_reply()
            # Строки едут ДАННЫМИ (`rows_to_payload`): `Action` не сериализуется, и «положить как
            # есть» означало бы отказ очереди и тихий уход прямым путём.
            accepted = await self._enqueue_notice(
                chat_id=chat_id, text=OFFER_REMINDER_TEXT, rows=reply.rows, kind=reply.kind)
            if not accepted:
                log.warning("напоминание не принято к доставке в %s (нет ни очереди, ни транспорта)", chat_id)
                continue
            await self._dialog.mark_offer_reminded(state)
            log.info("ui.remind chat=%s step=%s path=reminder", chat_id, state.step)

    async def _on_contact(self, event, contact, *, chat_id: str) -> None:
        """Контакт (телефон) от reply-кнопки: принимаем только на шаге телефона кнопочного пути."""
        if not self._allow(chat_id):
            return await event.respond(TOO_OFTEN_TEXT)
        if not await self._client_gate(event, chat_id):
            return
        state = await self._load_state("tg", chat_id) or ChatState(platform="tg", chat_id=chat_id)
        phone = str(getattr(contact, "phone_number", "") or "")
        sender_id = getattr(contact, "user_id", None)
        if sender_id and str(sender_id) != chat_id:
            # Телефон чужого человека: не запрещаем (клиент вправе оставить контакт родственника),
            # но фиксируем в логе — это важно при разборе спорных заявок.
            log.info("контакт от чужого пользователя: контакт=%s чат=%s", sender_id, chat_id)
        if state.step != "phone" or not state.offered_slots:
            return await event.respond("Телефон сейчас не нужен — продолжим по шагам. Напишите /start, если хотите начать заново.")
        log.info("ui.tap code=contact chat=%s path=client_button", chat_id)
        await self._send(event, await self._dialog.accept_contact(state, phone))

    async def _sender_profile(self, event) -> tuple[object | None, str | None]:
        """Профиль отправителя для самозаявки: только личный чат, `None` = сбой чтения."""
        if _is_private(event) is not True:
            return None, "not_private"
        if not self.running:
            return None, "transport_down"
        sender = None
        try:
            sender = await event.get_sender()
        except Exception as error:  # noqa: BLE001 — сеть/кэш: трактуем как сбой профиля (fail-closed)
            log.warning("профиль отправителя не получен (%s)", type(error).__name__)
        if sender is None or getattr(sender, "min", False):
            try:
                sender = await self._client.get_entity(int(event.chat_id))
            except (*NETWORK_ERRORS, ValueError) as error:
                log.warning("профиль через get_entity не получен (%s)", type(error).__name__)
                return None, "profile_failed"
        return sender, None

    async def _send_to(self, chat_id: str, text: str, rows=None, kind: str = REPLY) -> bool:
        """Сообщение конкретному адресату (ответ заявителю). True — доставлено."""
        if not self._client:
            log.warning("сообщение в %s не отправлено (нет транспорта)", chat_id)
            return False
        buttons = render.to_buttons(Reply(text=text, rows=rows or [], kind=kind),
                                    secret=self._ui_secret, chat_id=str(chat_id), token="") if rows else None
        try:
            await self._client.send_message(int(chat_id), text, buttons=buttons)
            return True
        except NETWORK_ERRORS as error:
            log.warning("сообщение в %s не ушло: %s", chat_id, error)
            return False

    async def _outbox_sender(self, item: dict) -> None:
        """Доставить элемент очереди. Ошибки НЕ глотаем: очередь обязана знать исход.

        Отличие от прямых швов отправки (`_send_to`, `_notify_owner` и соседи) намеренное: те
        возвращают `False` при сетевой ошибке, и для прямого вызова это честно («сообщение потеряно,
        но процесс жив»). Очереди такого ответа мало — ей надо решить «повторить или разбирать»,
        поэтому исключение поднимается наружу, а лимит Telegram превращается в `RetryAfter`
        (`FloodWaitError.seconds` — то самое число, которое Telegram просит подождать).
        """
        payload = item.get("payload") or {}
        chat_id = str(payload.get("chat_id") or "")
        text = str(payload.get("text") or "")
        if not chat_id or not text:
            raise ValueError(f"в очереди строка без адресата или текста: {item.get('id')}")
        if self._client is None:
            raise RuntimeError("нет транспорта: отправка из очереди невозможна")

        rows = rows_from_payload(payload.get("rows"))
        kind = str(payload.get("kind") or REPLY)
        if payload.get("buttons"):
            # Кнопки-дескрипторы собираются ТЕМ ЖЕ построителем, что и на прямом пути: иначе формат
            # подписи мог бы разойтись, и кнопка из очереди не сработала бы.
            buttons = self._callback_buttons(
                booking_id=int(payload.get("booking_id") or 0), chat_id=chat_id,
                actions=[(item["action"], item["label"]) for item in payload["buttons"]],
            )
        else:
            buttons = (render.to_buttons(Reply(text=text, rows=rows, kind=kind),
                                        secret=self._ui_secret, chat_id=chat_id, token="")
                       if rows else None)
        if (payload.get("buttons") or rows) and not buttons:
            # Кнопки не собраны молча быть не должны: получатель увидел бы текст без клавиатуры,
            # а разбираться пришлось бы по чужому сообщению «пришло без кнопок».
            log.warning("outbox: строка %s просила кнопки, но они не собраны "
                        "(нет UI-секрета или клавиатура пуста) — уйдёт текстом", item.get("id"))
        log.info("outbox: строка %s → чат %s, кнопок %s",
                 item.get("id"), chat_id, len(buttons or []))
        try:
            await self._client.send_message(int(chat_id), text, buttons=buttons)
        except FloodWaitError as error:
            raise outbox_queue.RetryAfter(
                float(getattr(error, "seconds", 0) or OUTBOX_DEFAULT_RETRY_S),
                reason=f"Telegram просит подождать {getattr(error, 'seconds', '—')} с",
            ) from error

    def _wake_outbox(self) -> None:
        """Разбудить воркер: работа появилась. Без этого сообщение ждало бы конца паузы простоя."""
        wake = getattr(self, "_outbox_wake", None)
        if wake is not None:
            wake.set()

    async def _outbox_result(self, outcome: str, *, item: dict | None = None,
                             error: Exception | None = None) -> None:
        """Исход доставки: счётчики и реакция на НЕДОСТИЖИМОГО адресата.

        Реакция на блокировку/удаление аккаунта живёт здесь, а не в момент постановки в очередь:
        «адресат недостижим» — это вывод из РЕАЛЬНОЙ попытки доставки, и раньше он делался по
        синхронному вызову. Теперь карточка уезжает в очередь, а вывод делается по её результату —
        иначе снятие готовности менеджера потерялось бы вместе с прямой отправкой.
        """
        if outcome == "failed" and isinstance(error, UNREACHABLE_ERRORS) and item:
            recipient = str((item.get("payload") or {}).get("chat_id") or "")
            if recipient:
                log.warning("outbox: адресат %s недостижим (%s)", recipient, type(error).__name__)
                try:
                    await self._db.set_manager_ready(chat_id=recipient, ready=False)
                except Exception as db_error:  # noqa: BLE001 — фан-аут не должен падать из-за БД
                    log.warning("готовность %s не снята: %s", recipient, type(db_error).__name__)
                # C-4: владелец должен знать, что менеджер недостижим.
                await self._notify_owner(
                    f"Адресат {recipient} недостижим ({type(error).__name__}): "
                    f"карточки заявок до него не доходят."
                )
        if self._db is None:
            return
        metric = {"sent": "outbox_sent", "failed": "outbox_failed",
                  "retry": "outbox_retries"}.get(outcome)
        if metric is None:
            return
        try:
            await self._db.bump_metric(metric=metric)
        except Exception as metric_error:  # noqa: BLE001 — метрика не ломает доставку
            log.warning("outbox: счётчик %s не записан (%s)", metric, type(metric_error).__name__)

    async def _outbox_tick(self) -> None:
        """Пик лага очереди за день: `bump_metric` складывает, а лаг складывать нельзя."""
        if self._db is None:
            return
        try:
            lag = await self._db.outbox_lag_s()
            await self._db.set_max_metric(metric="outbox_lag_s", value=float(lag))
        except Exception as error:  # noqa: BLE001 — метрика не ломает очередь
            log.warning("outbox: лаг не записан (%s)", type(error).__name__)

    async def _send_visit_marking(self, *, chat_id: str, role: str) -> None:
        """Кнопки отметки явки по прошедшим визитам без отметки.

        Показываем только те, где отметка уместна: визит **уже прошёл**, а решения по нему
        ещё нет. Иначе менеджер мог бы отметить явку заранее, и метрика считала бы
        намерения вместо фактов.
        """
        own = role != OWNER
        rows = await self._db.confirmed_bookings(chat_id=chat_id if own else None, limit=10)
        now = dt.datetime.now(dt.UTC)
        pending = [
            row for row in (rows or [])
            if not row.get("visit_result") and (slot := as_datetime(row.get("slot"))) and slot < now
        ]
        if not pending or not self._client:
            return
        # По одному визиту в строке: подпись кнопки привязана к номеру брони, поэтому
        # «пакетная» кнопка на несколько заявок была бы неразборчивой.
        marking: list[list] = []
        for row in pending[:5]:
            booking_id = int(row["id"])
            buttons = self._visit_buttons(
                booking_id, chat_id,
                [("wa", f"✅ #{booking_id} пришёл"), ("wn", f"🚫 #{booking_id} не пришёл")])
            if buttons:
                marking.append(buttons)
        if not marking:
            return
        try:
            await self._client.send_message(int(chat_id), VISIT_MARK_PROMPT, buttons=marking)
        except NETWORK_ERRORS as error:
            log.warning("кнопки отметки явки не ушли (%s)", error)

    async def _views_reply(self, *, chat_id: str, role: str) -> Reply:
        """«👁 Просмотры (N)»: менеджеру — свои подтверждённые, владельцу — все (DD-6)."""
        own = role != OWNER                      # овнеру показываем все заявки агентства
        rows = await self._db.confirmed_bookings(chat_id=chat_id if own else None, limit=10)
        log.info("ui.views chat=%s role=%s scope=%s n=%s", chat_id, role, "own" if own else "all", len(rows))
        if not rows:
            return Reply(text=f"{VIEWS_TITLE}\n{VIEWS_EMPTY}", kind=REPLY)
        listings = await self._listings()
        lines = []
        for row in rows:
            slot = format_slot(row["slot"], self._settings.tz)
            address = fmt.address_for(row.get("listing_id"), listings) or row.get("listing_id")
            #: имя И телефон клиента; пустые значения показываем честно, а не пустой строкой
            name = (row.get("client_name") or "").strip() or "имя не указано"
            phone = (row.get("client_phone") or "").strip() or "телефон не указан"
            lines.append(f"• #{row['id']} · {slot} · {address}")
            lines.append(f"     {name} · {phone}")
        title = VIEWS_TITLE + (" " + VIEWS_OWN_SUFFIX if own else "")
        return Reply(text=f"{title}\n" + "\n".join(lines), kind=REPLY)

    async def _refresh_menu(self, recipient: str, *, reason: str) -> None:
        """Переслать адресату его меню с АКТУАЛЬНЫМИ счётчиками (DD-2/DD-3).

        Почему отдельный путь, а не `_notify_people`: тот дедупит по (адресат, текст) с окном 60 с, а текст
        со счётчиком повторяется (подтвердили одну — пришла новая, N совпало) ⇒ обновление клавиатуры
        подавлялось бы и кнопка «залипала». Здесь дедупа нет; роль адресата определяет вид меню.
        """
        if not self._client:
            return
        role = await self._role(str(recipient), private=True)
        pending = await self._pending_count()
        try:
            if role == OWNER:
                paused = (await self._db.setting("bot_mode", "live")) != "live"
                reply = Reply(text=f"📋 Заявки: {pending} ждут решения",
                              rows=owner_menu_kb(pending=pending, paused=paused,
                                                 confirmed=await self._confirmed_count()),
                              kind=REPLY)
            else:
                reply = Reply(text=f"📋 Заявки: {pending} ждут решения",
                              rows=manager_menu_kb(pending, confirmed=await self._confirmed_count(recipient)),
                              kind=REPLY)
            buttons = render.to_buttons(reply, secret=self._ui_secret, chat_id=str(recipient), token="")
            await self._client.send_message(int(recipient), reply.text, buttons=buttons)
            log.info("ui.menu_refresh chat=%s pending=%s reason=%s", recipient, pending, reason)
        except NETWORK_ERRORS as error:
            log.warning("меню не обновлено для %s (%s)", recipient, type(error).__name__)

    async def _report_reply(self) -> Reply:
        """📊 Отчёт овнера: числа из bookings + meta_metrics, без ПД (, C2)."""
        today = dt.datetime.now(self._settings.tz).date()
        data = await self._db.day_report(today)
        metrics = await self._db.day_metrics(today, ("dialogues", "llm_turns"))
        return Reply(text=fmt.report_text(
            day=f"{today:%d.%m}", total=data.get("total", 0), confirmed=data.get("confirmed", 0),
            rejected=data.get("rejected", 0), pending=data.get("pending", 0),
            upcoming=data.get("upcoming", 0), dialogues=int(metrics.get("dialogues", 0)),
            llm_turns=int(metrics.get("llm_turns", 0)),
            # Стоимость дня из трасс ходов: «нет данных» больше не единственный честный ответ
            cost_usd=await self._db.cost_usd(today),
        ))

    async def _status_reply(self) -> Reply:
        """ℹ️ Статус: режим, очередь, менеджеры, схема, апдейты (без ПД)."""
        managers = await self._db.list_managers()
        schema_ok = True
        try:
            await self._db.load_owner_flow(platform="tg", chat_id="schema-probe")
        except Exception as error:  # noqa: BLE001 — схема 2b не применена ⇒ честно показываем
            log.warning("проверка схемы не прошла: %s", type(error).__name__)
            schema_ok = False
        stats = self.stats()
        return Reply(text=fmt.status_text(
            mode=await self._db.setting("bot_mode", "live"), pending=await self._pending_count(),
            managers_active=sum(1 for row in managers if row.get("active")),
            managers_ready=sum(1 for row in managers if row.get("active") and row.get("ready")),
            updates_processed=stats["updates_processed"], last_update_age_s=stats["last_update_age_s"],
            schema_ok=schema_ok,
        ))

    async def _notify_people(self, text: str, *, actor: str | None = None) -> None:
        """Уведомить владельца и готовых менеджеров (дедуп + дебаунс; актор не получает своё же сообщение)."""
        recipients = await self._card_recipients()
        now = time.time()
        for recipient in sorted(recipients - {str(actor or "")}):
            key = (recipient, text)
            if now - self._notified_at.get(key, 0.0) < NOTIFY_COOLDOWN_S:
                continue          # дебаунс: не чаще одного такого уведомления в минуту ()
            self._notified_at[key] = now
            # Дебаунс и выбор адресатов остаются ЗДЕСЬ (это решение производителя), отправка — очередь.
            if not await self._enqueue_notice(chat_id=recipient, text=text):
                log.warning("уведомление не ушло в %s (нет ни очереди, ни транспорта): %s", recipient, text)

    async def _notify_owner(self, text: str) -> None:
        """Короткое служебное уведомление владельцу (без ПД). Ошибки не роняют вызывающий путь."""
        owner = str(self._settings.telegram_owner_chat_id or "")
        if not owner:
            log.warning("уведомление владельцу не отправлено (owner не задан): %s", text)
            return
        # Через очередь: доставку гарантирует она, а не момент вызова. Логика «кому и что»
        # осталась здесь — поменялась только последняя миля.
        if not await self._enqueue_notice(chat_id=owner, text=text):
            log.warning("уведомление владельцу не ушло (нет ни очереди, ни транспорта): %s", text)

    async def _notify_manager(self, booking: Booking) -> None:
        """Карточка заявки — каждому адресату со СВОЕЙ подписью (HMAC включает chat_id)."""
        if self._client is None and self._db is None:
            log.warning("карточка не отправлена (нет ни транспорта, ни очереди): заявка #%s", booking.id)
            return
        recipients = await self._card_recipients()
        if not recipients:
            log.warning("карточка не отправлена (нет адресатов): заявка #%s", booking.id)
            return
        try:
            listings = await self._listings()
        except Exception as error:  # noqa: BLE001 — карточка важнее адреса: без списка объектов показываем «—»
            log.warning("список объектов недоступен для карточки: %s", type(error).__name__)
            listings = []
        listing = next((item for item in listings if item.id == booking.listing_id), None)
        card = fmt.manager_card(
            booking_id=booking.id, listing=listing, slot_iso=booking.slot,
            name=booking.client_name, phone=booking.client_phone,
        )
        sent = 0
        for recipient in sorted(recipients):
            # Кнопки едут ДЕСКРИПТОРАМИ: готовые объекты Telethon в очередь не положить, а без кнопок
            # менеджер не смог бы подтвердить заявку (регресс работающей функции). Подпись собирается
            # тем же `_callback_buttons` и включает chat_id — чужая кнопка не сработает.
            if await self._enqueue_notice(
                chat_id=recipient, text=card, booking_id=booking.id,
                buttons=[("c", "✅ Подтвердить"), ("r", "❌ Отклонить")],
            ):
                sent += 1
        if sent < len(recipients):
            log.warning("карточка заявки #%s: принято %d из %d", booking.id, sent, len(recipients))
        log.info("заявка #%s принята к доставке (%d/%d адресатам)", booking.id, sent, len(recipients))
        for recipient in sorted(recipients):                    # DD-4: счётчик в меню не должен залипать
            await self._refresh_menu(recipient, reason="booking")

    async def _save_state(self, state: ChatState) -> None:
        await self._db.save_conversation(
            platform=state.platform, chat_id=state.chat_id, step=state.step,
            continuum={
                "listing_id": state.listing_id, "slot": state.slot, "name": state.name, "phone": state.phone,
                #: что предложено кнопками (whitelist для тапов) и когда
                "offered_slots": state.offered_slots, "offered_at": state.offered_at,
                "offered_listing": state.offered_listing, "offer_reminded_at": state.offer_reminded_at,
                "offered_listings": state.offered_listings, "client_filter": state.client_filter,
            },
        )

    async def _load_state(self, platform: str, chat_id: str) -> ChatState | None:
        row = await self._db.load_conversation(platform=platform, chat_id=chat_id)
        if not row:
            return None
        continuum = row.get("continuum") or {}
        return ChatState(
            platform=platform, chat_id=chat_id, step=row.get("step") or "listing",
            listing_id=continuum.get("listing_id"), slot=continuum.get("slot"),
            name=continuum.get("name"), phone=continuum.get("phone"),
            offered_slots=list(continuum.get("offered_slots") or []),
            offered_at=float(continuum.get("offered_at") or 0.0),
            offered_listing=continuum.get("offered_listing"),
            offer_reminded_at=float(continuum.get("offer_reminded_at") or 0.0),
            offered_listings=list(continuum.get("offered_listings") or []),
            client_filter=continuum.get("client_filter") or None,
        )

    async def _keep_typing(self, chat_id: int) -> None:
        """«печатает…» до отмены задачи (Telethon: action() — только через async with)."""
        if not self._client:
            return
        while True:
            try:
                async with self._client.action(chat_id, "typing"):
                    await asyncio.sleep(TYPING_REFRESH_S)
            except asyncio.CancelledError:
                raise
            except NETWORK_ERRORS as error:
                log.debug("typing недоступен: %s", error)
                return

    def _note_update(self) -> None:
        """След живого трафика: по нему /health отличает «бот молчит» от «просто тихо»."""
        self._updates_processed += 1
        self._last_update_ts = time.time()

    def stats(self) -> dict:
        return {
            "updates_processed": self._updates_processed,
            "last_update_ts": int(self._last_update_ts) if self._last_update_ts else None,
            "last_update_age_s": (
                int(time.time() - self._last_update_ts) if self._last_update_ts else None
            ),
            #: Живы ли фоновые петли. Мост разворачивает `stats()` в `/health` целиком, поэтому поле
            #: доезжает до сторожа без правки моста; сторож краснеет только на `dead`.
            "loops": self.loops(),
        }

    def _allow(self, chat_id: str) -> bool:
        return self._limiter.allow(chat_id)
