"""Оркестратор диалога bridge: agenda + вызов агента через канал + BOOKING → pending.

Ключевые инварианты (план v14/v15):
  * **шаги ведёт bridge** (`listing → slot → name → phone → confirm`), а не LLM: ответ клиента
    разбирается детерминированно (`advance_state`), слот принимается ТОЛЬКО из предложенных;
  * агент получает данные хода (листинги/слоты/step/лексема) — единственный источник правды;
  * бронь создаёт ТОЛЬКО bridge, разобрав BOOKING-блок и сверив его с собственным состоянием
    (агент не может подменить объект или слот);
  * телефон в тексте клиенту маскируется, в LLM уходит только служебной строкой блока;
  * клиентский эхо-блок нейтрализуется до отправки агенту.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agent_core import telemetry
from agent_core.retrieval.coverage import coverage_hint
from agent_core.ui.reply import INLINE, REPLY, Action, Reply
from realty_agent.booking.filter import (
    ORDINALS,
    apply_filter,
    describe,
    floor_info,
    is_pure_constraint,
    looks_like_constraint,
    normalize_spec,
    parse_constraints,
)
from realty_agent.booking.parser import (
    ParsedBooking,
    find_unfulfilled_promise,
    neutralize_client_blocks,
    offers_escalation,
    parse_booking_block,
    parse_coverage_block,
    parse_filter_block,
    strip_coverage_block,
    strip_filter_block,
)
from realty_agent.channels.turn_context import Listing, TurnContext, build_turn_text
from realty_agent.local_close.spec import local_spec
from realty_agent.phones import canonical_phone, contains_unmasked_phone, mask_phone, mask_phones
from realty_agent.tg.handlers import (
    BUSY_TEXT,
    CHANNEL_TIMEOUT_TEXT,
    DONE_TEXT,
    FILTER_ACTIVE,
    FILTER_FOUND,
    FILTER_NONE,
    FILTER_RESET_BTN,
    FILTER_RESET_NOTICE,
    FILTER_UNPARSED,
    FILTER_UNPARSED_NOTE,
    MENU_RETURN_NOTE,
    NAME_ASK_TEXT,
    OBJECTS_EMPTY,
    OBJECTS_HINT,
    OBJECTS_TITLE,
    OFFER_EXPIRED_TEXT,
    OFFER_REMINDER_SHOW,
    OFFER_REMINDER_TEXT,
    PHONE_ASK_TEXT,
    SLOT_UNAVAILABLE_TEXT,
    SLOTS_DAY_HEADER,
    SLOTS_EMPTY_TEXT,
    SLOTS_PICK_DAY_TEXT,
    STALE_OFFER_TEXT,
)
from realty_agent.turns import new_turn_key
from realty_agent.ui import format as fmt
from realty_agent.ui import menu
from realty_agent.ui.format import client_card
from realty_agent.ui.keyboards import (
    client_menu_kb,
    confirm_kb,
    day_grid,
    done_menu_kb,
    listings_kb,
    phone_request_kb,
    time_grid,
)
from realty_agent.ui.kinds import KIND_BACK, KIND_CANCEL

log = logging.getLogger("realty_agent.dialog")

#: сколько живёт предложение слотов, после чего его обязательно обновляем
OFFER_TTL_S = 20 * 60
# Напоминание «Ещё актуально?» — один раз в этом окне возраста оффера
OFFER_REMIND_AFTER_S = 10 * 60

def _log_turn_io(where: str, *, chat_id: str, step: str, text: str) -> None:
    """Отладочный лог ввода-вывода хода: `in` (в канал), `out` (от агента), `sent` (клиенту).

    Гейт — уровень DEBUG у нашего логгера, а он поднимается **только** по флагу
    `TURN_IO_DEBUG` (`main.py`). Поэтому «логировать всегда» не случается: в отладочном
    режиме в локальный файл попадает текст клиента, и это осознанный выбор для разбора,
    а не рабочий режим. Телефон маскируется до сборки текста, поэтому и в отладке он маской.

    Появилось это после случая, когда клиенту ушёл запасной текст, а агент **ответил**:
    ответ лежал в его же сессии, но мост смотрел не туда. Без явного входа-выхода приёмка
    опиралась на косвенные признаки и этого не увидела — поэтому «что ушло» и «что вернулось»
    теперь видно строками журнала, а не выводится из баз и сессий.
    """
    if not log.isEnabledFor(logging.DEBUG):
        return
    log.debug("turn.%s chat=%s step=%s len=%s\n%s", where, chat_id, step, len(text), text)


def _reset_action():
    """Кнопка снятия активного фильтра (DD-8)."""
    from realty_agent.ui.kinds import KIND_FILTER_RESET

    return Action(FILTER_RESET_BTN, KIND_FILTER_RESET)


def _object_lines(listings: list[Listing], *, floor_mark: bool = False) -> str:
    """Строки каталога; при активном ЭТАЖНОМ условии объекты без данных об этаже помечаются (.1)."""
    lines = []
    for index, item in enumerate(listings, start=1):
        line = fmt.object_line(index, item)
        if floor_mark and floor_info(item.address) is None:
            line = f"{line} · этаж не указан"
        lines.append(line)
    return "\n".join(lines)


def _objects_text_for(listings: list[Listing], *, note: str = "", floor_mark: bool = False) -> str:
    """Текст каталога: шапка, полные строки объектов, подсказка и (при необходимости) приписка."""
    if not listings:
        return f"{OBJECTS_TITLE}\n{OBJECTS_EMPTY}"
    body = f"{OBJECTS_TITLE}\n{_object_lines(listings, floor_mark=floor_mark)}\n\n{OBJECTS_HINT}"
    return f"{body}\n\n{note}" if note else body


def _day_slot_of(offered: list[str], day_index: int) -> str:
    """Первый слот дня по его индексу (тот же порядок, что в `day_grid`) — для шапки."""
    days: list[str] = []
    for slot in offered:
        day = fmt.canonical(slot).split("T")[0]
        if day and day not in days:
            days.append(day)
    if not (0 <= day_index < len(days)):
        return ""
    return next(slot for slot in offered if fmt.canonical(slot).split("T")[0] == days[day_index])


# --- разбор ответов клиента (чистые функции: тестируются офлайн) ---

LISTING_ID_RE = re.compile(r"\b([a-zа-яё]{1,3}-\d{1,4})\b", re.IGNORECASE)
# Порядковые — ТОЛЬКО с границы слова: иначе «посмоТРЕТь» читается как «третий» и бот
# молча выбирает третий объект за клиента.
ORDINAL_RE = re.compile(r"\b(перв|втор|трет|четв|пят|шест)")
ORDINAL_INDEX = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5, "шест": 6}
MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTH_STEMS = "январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октябр|ноябр|декабр"
DATE_LIKE_RE = re.compile(
    rf"\d{{4}}-\d{{2}}-\d{{2}}|\b\d{{1,2}}[.\-/]\d{{1,2}}\b|\b\d{{1,2}}\s+(?:{MONTH_STEMS})|(?:{MONTH_STEMS})\w*\s+\d{{1,2}}"
)
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})\b")
MAX_NAME_LEN = 40


def canonical_slot(slot: str) -> str:
    """Обе законные формы слота («2026-10-02 10:00» и с `T`) → одна каноническая."""
    return re.sub(r"\s+", "T", (slot or "").strip())


def slot_time(slot: str) -> str:
    """«10:00» слота или пустая строка: мусор не должен ронять ход (см. критику A)."""
    _, _, time_part = canonical_slot(slot).partition("T")
    return time_part


# Стоп-слова, которые НИКОГДА не считаются «именем объекта»: удобства и характеристики ().
ADDRESS_STOP = ("ремонт", "метро", "парк", "балкон", "лоджи", "центр", "мин", "вид", "студи",
                "этаж", "комнат", "корп", "жк", "улиц", "переул", "проезд", "проспект", "шоссе", "квартал")
# Вежливые/менюшные слова: снимаются перед проверкой «сообщение целиком = ссылка» ().
# ВАЖНО: сюда НЕ входят «комнат/этаж/млн/м²» — иначе «2 комнаты» превратилось бы в выбор объекта.
POLITE_RE = re.compile(r"\b(?:возьми|возьму|беру|выбираю|выбери|пожалуйста|квартир\w*|вариант\w*|"
                       r"покажи|покажите|это|мне|хочу|нужна|нужен)\b", re.IGNORECASE)
UNIT_RE = re.compile(r"м²|м2|кв\.?\s*м|млн|млрд|тыс|этаж|эт\.|комнат|мин\b", re.IGNORECASE)
HOUSE_RE = re.compile(r"\b(?:дом|кв\.?|квартир\w*)\s*(\d{1,4})\b", re.IGNORECASE)


def _street_index(listings: list[Listing]) -> dict[str, str]:
    """Улица → id, только для УНИКАЛЬНЫХ слов (аббревиатуры и характеристики адреса отброшены)."""
    by_word: dict[str, set[str]] = {}
    for item in listings:
        head = re.sub(r"\(.*?\)", " ", (item.address or "").split(",")[0])
        for word in re.findall(r"[а-яёa-z]{4,}", head.lower()):
            if not any(word.startswith(stop) for stop in ADDRESS_STOP):
                by_word.setdefault(word, set()).add(item.id)
    return {word: next(iter(ids)) for word, ids in by_word.items() if len(ids) == 1}


def explicit_object_ref(text: str, listings: list[Listing]) -> str | None:
    """Явное указание КОНКРЕТНОЙ квартиры (): id, часть улицы или отдельно стоящий номер.

    Выбор объекта выполняет пользователь: кнопкой (индекс из показанного списка) или явной ссылкой в тексте.
    Никаких «догадок»: «выше второго этажа», «с ремонтом», «2 комнаты» — это условия (фильтр), а не выбор.
    """
    if not text or not listings:
        return None
    low = text.lower().strip()

    # 1) точный идентификатор каталога
    ids = {item.id.lower(): item.id for item in listings}
    for candidate in LISTING_ID_RE.findall(low):
        if candidate in ids:
            return ids[candidate]

    # 2) уникальная часть УЛИЦЫ («Пушкина», «Мира»)
    for word, listing_id in _street_index(listings).items():
        if re.search(rf"\b{word}\b", low):
            return listing_id

    # 3) дом/квартира — только с маркером («дом 10», «кв. 42»), голое число домом не считается
    for number in HOUSE_RE.findall(low):
        matched = [item.id for item in listings
                   if any(n == number for n in HOUSE_RE.findall((item.address or "").lower()))]
        if len(matched) == 1:
            return matched[0]

    # 4) «сообщение целиком = ссылка»: даты/единицы исключаем ДО номерного пути
    if DATE_LIKE_RE.search(low) or TIME_RE.search(low) or UNIT_RE.search(low):
        return None
    rest = POLITE_RE.sub(" ", low)
    rest = " ".join(re.sub(r"[^\wа-яё\s]", " ", rest).split())
    if not rest:
        return None
    if rest.isdigit():
        index = int(rest)
        return listings[index - 1].id if 1 <= index <= len(listings) else None
    if len(rest.split()) == 1:
        number = next((value for stem, value in ORDINALS.items() if rest.startswith(stem)), None)
        if number and 1 <= number <= len(listings):
            return listings[number - 1].id
    return None


def match_slot(text: str, slots: list[str]) -> str | None:
    """Слот принимается ТОЛЬКО из предложенных. Формы: ISO, «01.10 10:00», «1 октября 10:00»."""
    if not text or not slots:
        return None
    lowered = re.sub(r"\s+", " ", text.lower())
    hits = [slot for slot in slots if slot_matches(lowered, slot)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # названо только время, а подходит несколько дней → просим уточнить дату
        times = {slot_time(slot) for slot in hits}
        return None if len(times) == 1 else hits[0]
    if DATE_LIKE_RE.search(lowered):
        return None  # клиент назвал дату, но ни один предложенный слот ей не отвечает
    # только время, однозначное среди предложенных
    time_match = TIME_RE.search(lowered)
    if time_match:
        wanted = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
        same = [slot for slot in slots if slot_time(slot) == wanted]
        if len(same) == 1:
            return same[0]
    return None


def _mentioned_when(lowered_text: str) -> str:
    """Что именно назвал клиент — для фразы «на <это> просмотров нет». Пусто — скажем «это время».

    Нужно ровно для честности ответа: «на 10:30 просмотров нет» читается как ответ, а «на это время» —
    как отписка. Дату называем словами клиента только если времени в тексте нет.
    """
    time_match = TIME_RE.search(lowered_text)
    if time_match:
        return f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
    date_match = DATE_LIKE_RE.search(lowered_text)
    return date_match.group(0).strip() if date_match else ""


def slot_matches(lowered_text: str, slot: str) -> bool:
    """Есть ли в тексте явная дата+время этого слота (год/день/месяц в разных формах)."""
    date_part, _, time_part = canonical_slot(slot).partition("T")
    if not date_part or not time_part:
        return False
    try:
        year, month, day = (int(part) for part in date_part.split("-"))
        hour, minute = (int(part) for part in time_part.split(":"))
    except ValueError:
        return False
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return False  # мусорный месяц/день/час не должен бросать IndexError
    if slot.lower() in lowered_text or slot.replace("T", " ").lower() in lowered_text:
        return True
    dates = (
        f"{day}.{month:02d}.{year}", f"{day:02d}.{month:02d}.{year}",
        f"{day}.{month}", f"{day:02d}.{month:02d}",
        f"{day} {MONTHS_GENITIVE[month - 1]}", f"{day:02d} {MONTHS_GENITIVE[month - 1]}",
        f"{MONTHS_GENITIVE[month - 1]} {day}",
    )
    times = (f"{hour}:{minute:02d}", f"{hour:02d}:{minute:02d}", f"{hour}:{minute}", f"{hour:02d}.{minute:02d}")
    has_date = any(date in lowered_text for date in dates)
    if not has_date:
        return False
    return any(time in lowered_text for time in times)


def parse_name(text: str) -> str | None:
    """Имя: короткая строка без вопросов/телефонов/BOOKING-мусора (иначе шаг не двигаем)."""
    candidate = " ".join((text or "").split())
    if not 2 <= len(candidate) <= MAX_NAME_LEN:
        return None
    # Нажатие reply-кнопки или ник (`@nick`) не должны становиться именем клиента.
    if candidate.startswith("@") or menu.resolve_menu(candidate) is not None:
        return None
    if "?" in candidate or any(char in candidate for char in "{}<>|/"):
        return None
    if canonical_phone(candidate):
        return None
    return candidate



@dataclass
class ChatState:
    platform: str
    chat_id: str
    step: str = "listing"          # listing → slot → name → phone → confirm
    listing_id: str | None = None
    slot: str | None = None
    name: str | None = None
    phone: str | None = None
    turns: int = 0
    # Этапы 3–4: что именно предложено клиенту кнопками (whitelist для тапов) и когда.
    offered_slots: list[str] = field(default_factory=list)
    offered_at: float = 0.0
    offered_listing: str | None = None
    offer_reminded_at: float = 0.0
    # DD-1: ПОРЯДОК показанных объектов (id). Индекс кнопки/номер из текста ищется ЗДЕСЬ,
    # иначе после фильтра подмножество нумеруется иначе и выбор указывает на другой объект.
    offered_listings: list[str] = field(default_factory=list)
    # DD-8: активное ограничение клиента (rooms/price_max/price_min/area_min/keywords)
    client_filter: dict | None = None


@dataclass(frozen=True)
class AgentReply:
    """Ответ агента вместе с тем, что платформа сообщила о самом вызове.

    `usage` (`{cost, input, output, total, type}`) и `session_id` приходят от канала и раньше
    выбрасывались: из-за этого в отчёте владельцу стояло «нет данных», хотя числа были под рукой.
    Оба поля необязательные — их отсутствие не ошибка, а «платформа не сообщила».
    """

    text: str
    done: bool = True
    usage: dict | None = None
    session_id: str = ""


@dataclass
class Booking:
    id: str
    platform: str
    chat_id: str
    listing_id: str
    slot: str
    client_name: str
    client_phone: str
    status: str = "pending"


class DialogService:
    # ---------- durable-состояние хода ---------- #

    async def _remember(self, *, platform: str, chat_id: str, turn, role: str, text: str, step: str) -> None:
        """Записать реплику хода в историю. Падение записи НЕ роняет ход — это шов, как и трасса.

        Почему здесь, а не в рантайме: история принадлежит РАЗГОВОРУ, а не способу его исполнить.
        Иначе строки появлялись бы только на `AGENT_RUNTIME=own`, и смена рантайма рвала бы контекст.

        Кап 600 символов на реплику: длинная простыня в истории — это деньги в КАЖДОМ следующем запросе,
        а окно и так ограничено восемью репликами (`own.HISTORY_LIMIT`).
        """
        if self._record_message is None or not (text or "").strip():
            return
        body = text if len(text) <= 600 else text[:600] + "…"
        try:
            await self._record_message(
                platform=platform,
                chat_id=chat_id,
                turn_key=str(getattr(turn, "turn_key", "") or ""),
                role=role,
                text=body,
                step=step,
            )
        except Exception as error:  # noqa: BLE001 — история не роняет ответ клиенту
            log.warning("dialog: реплика (%s) не записана: %s: %s", role, type(error).__name__, error)

    async def _begin_turn(self, *, platform: str, chat_id: str):
        """Зарегистрировать ход и взять его в работу. `None` — состояния хода нет (шов выключен).

        Ход регистрируется под уникальным ключом: повторная регистрация того же ключа не
        создаёт вторую запись, а захват идёт с lease — так один ход достаётся одному воркеру.
        """
        if self._turns is None:
            return None
        key = new_turn_key()
        state = await self._turns.begin(platform=platform, chat_id=chat_id, turn_key=key)
        claimed = await self._turns.claim(platform=platform, chat_id=chat_id, turn_key=key)
        return claimed or state

    async def _mark_turn_sent(self, turn) -> None:
        if self._turns is None or turn is None:
            return
        await self._turns.mark_sent(turn.id)

    async def _answer_turn(self, turn, reply_text: str) -> None:
        """Закрыть ход с ответом агента — ДО веток: ветка может упасть, а ход обязан быть закрыт."""
        if self._turns is None or turn is None:
            return
        await self._turns.mark_awaiting(turn.id)
        await self._turns.mark_answered(turn.id, reply_text=reply_text[:2000])

    async def _record_sent_text(self, turn, reply_text: str) -> None:
        """Уточнить текст хода ФАКТИЧЕСКИ ОТПРАВЛЕННЫМ (ветки фильтра заменяют ответ агента каталогом).

        Без этого по базе нельзя проверить, что именно увидел клиент: в записи стоит разбор агента, а
        клиенту уходит каталог с пометкой — расхождение.
        """
        if self._turns is None or turn is None:
            return
        await self._turns.mark_answered(turn.id, reply_text=reply_text[:2000])

    async def _fail_turn(self, turn, error: str) -> None:
        if self._turns is None or turn is None:
            return
        await self._turns.mark_failed(turn.id, error=error)

    async def _knowledge_for(self, client_text: str) -> tuple[str, ...]:
        """Фрагменты базы знаний для реплики клиента.

        **Не бросает никогда** — и это осознанно. База знаний не поднята, сервис
        эмбеддингов молчит, корпус пуст — всё это означает «знаний нет», а не «ход
        сломался»: клиент должен получить ответ, а не тишину. Причина пишется в лог,
        чтобы сбой не выглядел как честное «в базе ничего не нашлось».
        """
        if self._search_knowledge is None:
            return ()
        query = (client_text or "").strip()
        if not query:
            return ()
        try:
            fragments = await self._search_knowledge(query, self._knowledge_limit)
        except Exception as error:  # noqa: BLE001 — поиск не роняет диалог
            log.warning("dialog: поиск по базе знаний не удался (%s: %s) — ход без RAG",
                        type(error).__name__, error)
            return ()
        return tuple(fragment for fragment in fragments if fragment)

    async def _observe_ladder(self, *, text: str, booking, data_step: bool, selecting: bool,
                              agent_filter: dict | None) -> None:
        """Замер полосы лестницы: что можно закрыть локально и чем за это платим.

        **Почему считаем три числа, а не одно.** «Сколько ходов закроется без облака» без второго
        числа — это только приманка: если спек, который вывел агент, отличается от локального разбора,
        то локальное закрытие применило бы ДРУГОЙ фильтр, то есть изменило бы выдачу клиенту. Поэтому
        рядом считаются `same`/`differs` — это и есть измерение риска до включения механики.

        Ничего не решает и не меняет: только журнал и метрика. Недоступность метрики не ломает ход:
        наблюдение не должно уметь ронять диалог.
        """
        if self._report_ladder is None:
            return
        try:
            await self._report_ladder("turn")
            if booking is not None or data_step or not selecting:
                return
            local = parse_constraints(text)
            if not is_pure_constraint(text) or not local:
                return
            await self._report_ladder("band")
            if agent_filter is None:
                return
            # Спек сравниваем нормализованным: порядок ключей и «шумные» поля не должны выглядеть
            # расхождением, иначе число риска окажется завышенным на пустом месте.
            same = normalize_spec(agent_filter) == normalize_spec(local)
            await self._report_ladder("same" if same else "differs")
        except Exception as error:  # noqa: BLE001 — наблюдение не роняет ход
            log.warning("dialog: замер лестницы не удался (%s: %s)", type(error).__name__, error)

    async def _observe_contract(self, client_text: str) -> None:
        """Отметить контракт ответа: обещание без действия или предложенную настоящую кнопку.

        Смотрим по тексту, который **получил клиент**. Обещание («уточню у менеджера») хуже
        отказа: отказ виден, а обещание — нет, и клиент ждёт человека, который ничего не
        получил. Запрет живёт в контракте хода, здесь — **счётчик**: без числа не понять,
        нужна ли автоматическая эскалация и кому она должна идти.
        """
        if self._report_contract is None:
            return
        promise = find_unfulfilled_promise(client_text)
        if promise:
            log.warning(
                "dialog: обещание без действия (%r) — по нему менеджеру ничего не уходит", promise)
        try:
            if promise:
                await self._report_contract("promise_without_action")
            elif offers_escalation(client_text):
                await self._report_contract("escalation_offered")
        except Exception as error:  # noqa: BLE001 — наблюдение не роняет ход
            log.warning("dialog: отметка контракта не записана (%s)", type(error).__name__)

    async def _topics_for(self) -> tuple[str, ...]:
        """Механизмы, по которым в базе есть данные — чтобы было что предложить клиенту.

        Не бросает никогда, как и поиск: недоступный список означает «предложить нечего»,
        а не сломанный ход. Причина пишется в лог, чтобы отказ агента не выглядел как
        отсутствие данных в корпусе.
        """
        if self._knowledge_topics is None:
            return ()
        try:
            return tuple(await self._knowledge_topics())
        except Exception as error:  # noqa: BLE001 — список тем не роняет диалог
            log.warning("dialog: список тем базы знаний недоступен (%s: %s) — предложу без него",
                        type(error).__name__, error)
            return ()

    def __init__(
        self,
        *,
        ask_agent: Callable[[str, str], Awaitable[AgentReply]],
        listings: Callable[[], Awaitable[list[Listing]]],
        free_slots: Callable[[str | None], Awaitable[list[str]]],
        listing_exists: Callable[[str], Awaitable[bool]],
        slot_free: Callable[[str, str], Awaitable[bool]],
        busy_slots: Callable[[list[str]], Awaitable[set[str]]] | None = None,
        save_booking: Callable[[Booking], Awaitable[None]],
        notify_manager: Callable[[Booking], Awaitable[None]],
        save_state: Callable[[ChatState], Awaitable[None]],
        load_state: Callable[[str, str], Awaitable[ChatState | None]],
        count_listings: Callable[[], Awaitable[int]] | None = None,
        search_knowledge: Callable[[str, int], Awaitable[list[str]]] | None = None,
        # Механизмы, по которым есть данные. Поставщик, а не значение: список
        # меняется при наливке корпуса и кэшируется у ретривера. Нужен, чтобы агент предлагал
        # клиенту конкретное вместо общего «в базе нет».
        knowledge_topics: Callable[[], Awaitable[list[str]]] | None = None,
        # Куда сообщить выбор агента «ответил / уточнил»: журнал и метрика.
        # Необязательный шов: без него ход идёт как раньше, просто без наблюдения.
        report_coverage: Callable[[bool], Awaitable[None]] | None = None,
        # Наблюдение за контрактом ответа: чем бот пообещал без действия и предложил ли
        # настоящую кнопку вызова человека. Необязательный шов, как и прочие.
        report_contract: Callable[[str], Awaitable[None]] | None = None,
        # Наблюдение за ЛЕСТНИЦЕЙ в диалоге, шаг 0): попадает ли ход в полосу, где его можно
        # закрыть локально, и совпадает ли при этом спек агента с локальным разбором. Поведение НЕ
        # меняется — это замер возможности и риска перед тем, как что-то включать.
        report_ladder: Callable[[str], Awaitable[None]] | None = None,
        # Трасса хода: одна запись на клиентский ход — токены, стоимость, латентность, исход
        # разбора. Шов необязательный и НЕ решающий: падение записи не роняет ход.
        record_trace: Callable[[dict], Awaitable[None]] | None = None,
        # Реплики хода в БАЗУ: своему рантайму нужна история, а платформенная сессия её не
        # отдаёт. Пишет САЙТ, а не рантайм — иначе таблица заполнялась бы только на `AGENT_RUNTIME=own`,
        # и переключение рантайма рвало бы контекст разговора в одну сторону.
        record_message: Callable[..., Awaitable[None]] | None = None,
        knowledge_limit: int = 4,
        turns: object | None = None,
        reply_is_delivered: Callable[[str], bool] | None = None,
        local_gate: object | None = None,
        ladder_enabled: bool = False,
        new_id: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self._ask = ask_agent
        # Поиск по базе знаний: необязательный шов. Нет его или пустой корпус —
        # ход идёт как раньше, без блока «База знаний». База знаний не должна быть
        # условием работы диалога.
        self._search_knowledge = search_knowledge
        self._knowledge_topics = knowledge_topics
        self._report_coverage = report_coverage
        self._report_contract = report_contract
        self._report_ladder = report_ladder
        self._record_trace = record_trace
        self._record_message = record_message
        self._knowledge_limit = knowledge_limit
        # Durable-состояние хода. Шов необязательный: без него диалог работает как
        # раньше — просто не переживает падение процесса, и это отсутствие честно.
        self._turns = turns
        # Локальное закрытие хода (, шаг 2): флаг ВЫКЛ по умолчанию, потому что решение меняет
        # ответы клиенту на части ходов. Шов необязательный: нет флага или решателя — ход идёт к
        # агенту, как раньше; сбой решателя тоже оставляет прежнее поведение.
        self._ladder_enabled = ladder_enabled
        self._local_gate = local_gate
        self._reply_is_delivered = reply_is_delivered or (lambda _text: False)
        self._listings = listings
        self._free_slots = free_slots
        self._listing_exists = listing_exists
        self._slot_free = slot_free
        self._busy_slots = busy_slots
        self._save_booking = save_booking
        self._notify_manager = notify_manager
        self._save_state = save_state
        self._load_state = load_state
        self._count_listings = count_listings
        self._new_id = new_id
        self._lexemes: dict[str, str] = {}

    # ---------- public ---------- #

    async def _limit_note(self, shown: int) -> str:
        """«Показаны первые N из M» — зависимость ОПЦИОНАЛЬНА (): нет счётчика — нет приписки."""
        if self._count_listings is None:
            return ""
        try:
            return fmt.catalog_note(int(await self._count_listings()), shown)
        except Exception as error:  # noqa: BLE001 — приписка не критична для каталога
            log.warning("счётчик объектов недоступен: %s", type(error).__name__)
            return ""

    async def handle_message(self, *, platform: str, chat_id: str, text: str) -> Reply:
        state = await self._load_state(platform, chat_id) or ChatState(platform=platform, chat_id=chat_id)
        client_text = neutralize_client_blocks(text)
        listings = await self._listings()
        slots = await self._free_slots(state.listing_id)

        # Шаг двигает bridge (LLM за это не отвечает): разбор детерминированный.
        was_listing_step = state.step == "listing"
        was_listing_id = state.listing_id          # до `advance_state`: отличает «выбор в этом ходу» (b)
        was_name_step = state.step == "name"
        was_phone_step = state.step == "phone"
        # DD-1: номер из текста сопоставляем с тем же списком, что клиент видел кнопками
        numbered = await self._shown_listings(state)
        #: `advance_state` выберет объект только при явном номере/слове — фраза-ограничение это фильтр
        state = self.advance_state(state, text=client_text, listings=numbered, slots=slots)
        state.turns += 1
        if was_listing_step and state.listing_id:
            slots = await self._free_slots(state.listing_id)  # слоты выбранного объекта

        # Кнопочный путь (): если клиент шёл кнопками (есть предложение), телефон и карточку ведёт
        # bridge без LLM. Чисто текстовый путь сохраняет прежний контракт с агентом и BOOKING-блоком.
        button_path = bool(state.offered_slots)
        if button_path and was_phone_step and state.step == "confirm" and state.phone:
            await self._save_state(state)
            return await self.confirm_card(state)
        if button_path and was_name_step and state.step == "phone":
            await self._save_state(state)
            #: вместо скрытия меню показываем reply-кнопку запроса номера
            return Reply(text=PHONE_ASK_TEXT, rows=phone_request_kb(), kind=REPLY)

        # ШАГ СЛОТА, ЯВНОЕ ВРЕМЯ МИМО СПИСКА: отвечаем сами, агента не зовём.
        # Живой случай: клиент написал «в 10:30» (слоты идут по часам), и агент ответил
        # общим «уточните, что именно вас интересует» — формально не ложь, но клиент не узнал
        # главного. Ответ здесь известен заранее, поэтому он детерминированный и не стоит денег.
        unavailable = await self._unavailable_slot_reply(state=state, text=client_text)
        if unavailable is not None:
            return unavailable

        # ЛОКАЛЬНОЕ ЗАКРЫТИЕ ХОДА (, шаг 2): сообщение-условие можно разобрать у себя, не зовя
        # агента. Флаг по умолчанию ВЫКЛ: замер по хранимой истории даёт совпадение выдачи на 11
        # ходах из 12, но на трёх поведение всё же меняется, поэтому решение — отдельное.
        closed = await self._close_locally(platform=platform, chat_id=chat_id, state=state, text=client_text)
        if closed is not None:
            return closed

        lexeme = secrets.token_hex(3)[:6]
        self._lexemes[f"{platform}:{chat_id}"] = lexeme
        knowledge = await self._knowledge_for(client_text)
        topics = await self._topics_for()
        # Подсказка только при подключённом поиске: без базы знаний говорить о покрытии
        # нечего, и блока в контракте быть не должно.
        coverage = (coverage_hint(client_text, knowledge, topics)
                    if self._search_knowledge is not None else "")
        ctx = TurnContext(
            listings=listings,
            free_slots=slots,
            step=state.step,
            lexeme=lexeme,
            listing_id=state.listing_id,
            slot=state.slot,
            name=state.name,
            phone_mask=mask_phone(state.phone) if state.phone else "",
            knowledge=knowledge,
            topics=topics,
            coverage=coverage,
        )
        # Телефон из свободного текста не должен уходить в LLM открытым (инвариант, блокер NB-1).
        prompt_text = mask_phones(client_text)
        if contains_unmasked_phone(prompt_text):
            log.error("в промпт попал незамаскированный телефон (chat=%s) — маскирую повторно", chat_id)
            prompt_text = mask_phones(prompt_text)
        # Durable-состояние хода: регистрируем ход и помечаем НАЧАЛО отправки
        # ДО сетевого вызова. Если процесс умрёт во время вызова, при восстановлении ход
        # будет виден как «ответ мог уйти» и не будет переигран — ровно ради этого пункт и делался.
        turn = await self._begin_turn(platform=platform, chat_id=chat_id)
        if turn is not None:
            await self._mark_turn_sent(turn)
        turn_text = build_turn_text(client_text=prompt_text, ctx=ctx)
        _log_turn_io("in", chat_id=chat_id, step=state.step, text=turn_text)
        #: В историю идёт РЕПЛИКА КЛИЕНТА (`prompt_text`), а не `turn_text`: блок состояния собирается
        #: заново на каждом ходу и в истории был бы устаревшей правдой, которую модель приняла бы за
        #: текущую. Хранится только маскированный текст.
        await self._remember(platform=platform, chat_id=chat_id, turn=turn, role="user",
                             text=prompt_text, step=state.step)
        started = _time.perf_counter()
        try:
            reply = await self._ask(f"{platform}:{chat_id}", turn_text)
        except Exception as error:  # ход обязан быть закрыт, даже если упал
            await self._fail_turn(turn, f"{type(error).__name__}: {error}")
            # Трасса пишется и на падении: без неё сбойный ход неотличим от «хода не было»,
            # а именно такие ходы в отчёте и нужны.
            await self._trace(platform=platform, chat_id=chat_id, state=state, turn=turn,
                              turn_text=turn_text, context_items=len(knowledge) + len(topics),
                              latency_ms=int((_time.perf_counter() - started) * 1000),
                              agent_called=True, outcome="error", parser_verdict="not_applicable")
            raise
        latency_ms = int((_time.perf_counter() - started) * 1000)
        _log_turn_io("out", chat_id=chat_id, step=state.step, text=reply.text)

        # проверяем только то, что реально выбрано: пустой слот/объект — это не «занято», а «ещё не выбран»
        listing_ok = bool(state.listing_id) and await self._listing_exists(state.listing_id or "")
        slot_ok = (
            None if not (state.listing_id and state.slot)
            else await self._slot_free(state.listing_id, state.slot)
        )
        booking, reason = parse_booking_block(
            reply.text, lexeme=lexeme, listing_id_valid=listing_ok, slot_valid=slot_ok,
        )
        # Исход разбора — это ПРИЧИНА парсера (`bad_challenge_lexeme`, `no_block_in_last_line`,
        # `bad_phone`, …), а не свой словарь из четырёх значений: словарь терял бы различие между
        # «блока нет» и «лексема не сошлась», а это разные сбои и лечатся они по-разному.
        parser_verdict = "ok" if booking is not None else (reason or "no_block_in_last_line")
        #: служебные блоки вырезаны; внутренние id в тексте человеку заменяем адресом
        # Маркер выбора «ответил или уточнил»: снимаем из текста клиенту и
        # отдаём наружу — он и делает границу «ответ / уточнение» наблюдаемой величиной.
        answered = parse_coverage_block(reply.text)
        if answered is not None and self._report_coverage is not None:
            try:
                await self._report_coverage(answered)
            except Exception as error:  # noqa: BLE001 — наблюдение не роняет ход
                log.warning("dialog: отметка покрытия не записана (%s)", type(error).__name__)
        clean = fmt.replace_listing_ids(
            strip_coverage_block(strip_filter_block(self._strip_block(reply.text))), listings)
        # Ответ агента есть? Запоминаем ДО подмены на «канал не ответил»: в ветке «условие не
        # распознано» от этого зависит, показывать разбор агента или честную фразу-заготовку.
        has_answer = bool(clean.strip())
        if not clean.strip():
            #: ответа от агента нет (таймаут канала) — отвечаем честно, а не пустотой/«…»
            clean = CHANNEL_TIMEOUT_TEXT
            log.info("ui.channel_timeout chat=%s step=%s", chat_id, state.step)
        await self._observe_contract(clean)
        # Ход закрыт: ответ получен и разобран. Всё, что ниже (бронь, уведомления), идёт
        # после — состояние хода отвечает за «ровно один ответ», а не за запись брони.
        await self._answer_turn(turn, clean)
        out = await self._compose_reply(
            state=state, listings=listings, clean=clean, has_answer=has_answer, booking=booking,
            reason=reason, platform=platform, chat_id=chat_id, text=text, agent_text=reply.text,
            was_listing_id=was_listing_id, was_name_step=was_name_step,
        )
        # ⚠️ В журнале и в базе обязан быть ФАКТИЧЕСКИ ОТПРАВЛЕННЫЙ текст: ветки фильтра заменяют
        # ответ агента каталогом, поэтому запись хода уточняется ПОСЛЕ сборки ответа и ДО сетевого
        # вызова (сам вызов — в `bot.py`), иначе по журналу нельзя проверить, что увидел клиент.
        _log_turn_io("sent", chat_id=chat_id, step=state.step, text=out.text)
        await self._record_sent_text(turn, out.text)
        #: Ответ агента — в историю ТОЛЬКО тем, что ФАКТИЧЕСКИ УШЛО КЛИЕНТУ (`out.text`): `clean` ещё не
        #: знает про ветки фильтра, которые заменяют ответ агента каталогом. Сырой `reply.text` класть
        #: нельзя тем более: сырой ответ проносит в историю служебный блок вроде
        #: `COVERAGE|{"answered": false}`, и модель получает его обратно в следующем промпте как «то, что
        #: она говорила» — то есть учится повторять служебный блок.
        await self._remember(platform=platform, chat_id=chat_id, turn=turn, role="assistant",
                             text=mask_phones(out.text or ""), step=state.step)
        # «Ответа нет» и «ответ был, но без текста» — РАЗНЫЕ вещи: служебный блок без прозы
        # клиенту показывать нечего, но агент ответил. В трассе это разные исходы, иначе сбойный
        # ход с блоком выглядел бы как молчание канала.
        answered = bool(reply.text and reply.text.strip())
        await self._trace(platform=platform, chat_id=chat_id, state=state, turn=turn,
                          turn_text=turn_text, context_items=len(knowledge) + len(topics),
                          latency_ms=latency_ms, agent_called=True,
                          outcome="ok" if answered else "channel_timeout",
                          parser_verdict=parser_verdict if answered else "empty",
                          usage=getattr(reply, "usage", None),
                          session_id=getattr(reply, "session_id", ""))
        return out

    async def _unavailable_slot_reply(self, *, state: ChatState, text: str) -> Reply | None:
        """Клиент назвал ВРЕМЯ или ДАТУ, которых нет среди свободных — называем это и показываем сетку.

        `None` — «решать не мне»: шаг не слот, объекта нет, оффера кнопок не было (в чисто текстовом
        пути выбор слота и BOOKING-блок ведёт агент), либо в тексте нет явного времени/даты
        («а есть ещё?» — это вопрос, а не выбор).
        """
        if state.step != "slot" or not state.listing_id:
            return None
        lowered = re.sub(r"\s+", " ", text.lower())
        if not (TIME_RE.search(lowered) or DATE_LIKE_RE.search(lowered)):
            return None
        if not state.offered_slots:
            # Оффера не было (текстовый путь) — там слот выбирает агент, контракт не меняем.
            return None
        listings = await self._listings()
        listing = next((item for item in listings if item.id == state.listing_id), None)
        if listing is None:
            return None
        if await self.refresh_offer(state):
            # Оффер протух: сначала показываем АКТУАЛЬНОЕ, а не утверждаем про старое время —
            # иначе «на 10:30 просмотров нет» могло бы быть неправдой через двадцать минут.
            reply = self._slots_reply(state, listing, prefix=OFFER_EXPIRED_TEXT)
        else:
            when = _mentioned_when(lowered) or "это время"
            log.info("ui.slot_unavailable chat=%s when=%s (агент не вызывался)", state.chat_id, when)
            reply = self._slots_reply(state, listing, prefix=SLOT_UNAVAILABLE_TEXT.format(when=when))
        #: ❗ Побочные записи — ТЕ ЖЕ, что у любого другого хода: иначе этот ответ выпадал бы из истории
        #: (история принадлежит разговору, а не способу исполнения) и не попадал бы в трассу.
        turn = await self._begin_turn(platform=state.platform, chat_id=state.chat_id)
        if turn is not None:
            await self._mark_turn_sent(turn)
        await self._remember(platform=state.platform, chat_id=state.chat_id, turn=turn, role="user",
                             text=mask_phones(text), step=state.step)
        await self._remember(platform=state.platform, chat_id=state.chat_id, turn=turn, role="assistant",
                             text=mask_phones(reply.text or ""), step=state.step)
        await self._answer_turn(turn, reply.text)
        await self._trace(platform=state.platform, chat_id=state.chat_id, state=state, turn=None,
                          latency_ms=None, agent_called=False, outcome="slot_unavailable",
                          parser_verdict="not_applicable")
        return reply

    async def _close_locally(self, *, platform: str, chat_id: str, state: ChatState,
                             text: str) -> Reply | None:
        """Закрыть ход без агента: сообщение — только условие, спек даёт локальный разбор.

        `None` означает «закрывать нельзя» — тогда ход идёт к агенту, как раньше. Так ведут себя:
        выключенный флаг, отсутствие решателя, шаги ввода данных (имя/телефон/подтверждение),
        выбранный объект (там текст — это выбор слота, а не условие), пустой локальный разбор,
        ответ решателя «не только условие» и ЛЮБОЙ сбой решателя.

        Модель не строит спецификацию — её собирает наш разбор (`local_close/spec.py`), а
        решатель отвечает только на вопрос «это условие или что-то другое». Поэтому выдумать фильтр
        на этом пути нечем.
        """
        if not self._ladder_enabled or self._local_gate is None:
            return None
        if state.step in ("name", "phone", "confirm"):
            return None
        if state.step != "listing" or state.listing_id:
            return None
        spec = local_spec(text)
        if not spec:
            return None
        # Решателя спрашиваем ПОСЛЕ разбора: если применять нечего, платить за вопрос незачем.
        try:
            if not await self._local_gate(text):
                return None
        except Exception as error:  # noqa: BLE001 — гейт не роняет ход
            log.warning("dialog: гейт локального закрытия не сработал (%s)", type(error).__name__)
            return None
        turn = await self._begin_turn(platform=platform, chat_id=chat_id)
        if turn is not None:
            await self._mark_turn_sent(turn)
        #: ❗ История пишется и у хода БЕЗ агента. История принадлежит РАЗГОВОРУ, а не способу его
        #: исполнить: без этих двух строк вопрос клиента и ответ каталогом исчезали из окна, и следующий
        #: ход своего рантайма их не увидит.
        #: Маску берём ЗДЕСЬ: сюда `text` приходит ещё НЕмаскированным — маска применяется ниже по
        #: функции, уже после возврата этого ответа.
        await self._remember(platform=platform, chat_id=chat_id, turn=turn, role="user",
                             text=mask_phones(text), step=state.step)
        reply = await self._filtered_catalog(state, spec)
        _log_turn_io("sent", chat_id=chat_id, step=state.step, text=reply.text)
        await self._answer_turn(turn, reply.text)
        await self._remember(platform=platform, chat_id=chat_id, turn=turn, role="assistant",
                             text=mask_phones(reply.text or ""), step=state.step)
        log.info("ui.local_close chat=%s spec=%s (агент не вызывался)", chat_id, describe(spec))
        # Трасса и у хода БЕЗ агента: промпта не было — значит и хеша нет, и вложенного контекста нет.
        await self._trace(platform=platform, chat_id=chat_id, state=state, turn=None, latency_ms=None,
                          agent_called=False, outcome="local_close", parser_verdict="not_applicable")
        await self._observe_local_close()
        return reply

    async def _trace(self, *, platform: str, chat_id: str, state: ChatState, latency_ms: int | None,
                     agent_called: bool, outcome: str, parser_verdict: str,
                     turn_text: str = "", context_items: int = 0, usage: dict | None = None,
                     session_id: str = "", turn=None) -> None:
        """Записать трассу хода — ОДНА строка на клиентский ход, даже когда агента не звали.

        Почему одна точка: всё, что нужно строке, видно здесь и больше нигде — шаг, текст хода
        (из него хеш, то есть версия промпта), состав вложенного контекста, исход разбора,
        длительность вызова и числа, которые сообщила платформа. Расползись это по вызовам, отчёт
        считался бы по неполным строкам. Запись локально закрытого хода обязательна: иначе
        «$/диалог» считался бы только по ходам с облаком и был бы завышен.

        Наблюдение не роняет ход: падение записи — предупреждение в журнал.
        """
        if self._record_trace is None:
            return
        try:
            await self._record_trace({
                "platform": platform,
                "chat_id": chat_id,
                "turn_id": getattr(turn, "id", None),
                "step": state.step,
                "prompt_hash": hashlib.sha256(turn_text.encode()).hexdigest()[:16] if turn_text else "",
                "context_items": context_items,
                "agent_called": agent_called,
                "outcome": outcome,
                "parser_verdict": parser_verdict,
                "usage": usage,
                "session_id": session_id or "",
                "latency_ms": latency_ms,
                #: трейс платформы, в который попал этот ход. Пусто, когда трассировка выключена:
                # строка отчёта и трейс тогда друг на друга не указывают — и это видно, а не угадывается.
                "trace_id": telemetry.trace_id(),
            })
        except Exception as error:  # noqa: BLE001 — наблюдение не роняет ход
            log.warning("dialog: трасса хода не записана (%s)", type(error).__name__)

    async def _observe_local_close(self) -> None:
        """Счётчики локального закрытия — те же, что у полосы, плюс отдельное число закрытых.

        Отдельное число обязательно: без него «сколько закрыто локально» смешалось бы с полосой,
        которая считает возможность, а не факт.
        """
        if self._report_ladder is None:
            return
        try:
            await self._report_ladder("turn")
            await self._report_ladder("closed")
        except Exception as error:  # noqa: BLE001 — наблюдение не роняет ход
            log.warning("dialog: метрика локального закрытия не записана (%s)", type(error).__name__)

    async def _compose_reply(self, *, state: ChatState, listings: list[Listing], clean: str,
                             has_answer: bool, booking: ParsedBooking | None, reason: str,
                             platform: str, chat_id: str, text: str, agent_text: str,
                             was_listing_id: str | None, was_name_step: bool) -> Reply:
        """Собрать ответ клиенту по состоянию хода: фильтр, каталог, бронь или обычный ответ.

        Отдельным шагом — ради ОДНОГО места, где ответ становится известен целиком: `turn_state.reply_text`
        обязан совпадать с отправленным текстом, а ветки фильтра заменяют ответ агента каталогом.
        """
        # DD-8/: фильтр применяет МОСТ, и в СЦЕНАРИИ ВЫБОРА ответ обязан быть КНОПКАМИ.
        # Гейт — по состоянию ПОСЛЕ `advance_state` (шаг «до» не годится: после заявки состояние `done`,
        # и внутри хода оно само становится `listing` — на этом и терялись кнопки,).
        agent_filter = parse_filter_block(agent_text)
        data_step = state.step in ("name", "phone", "confirm")          # текст = данные клиента, не фильтр
        selecting = ((state.step == "listing" and not state.listing_id)  # объект не выбран (в т.ч. после заявки)
                     # объект выбран РАНЬШЕ (не в этом ходу) — предлагаем новый выбор (.2)
                     or (state.step == "slot" and bool(state.listing_id) and bool(was_listing_id)))
        # ЗАМЕР ЛЕСТНИЦЫ (шаг 0): считаем ДО ветки, поведение не меняем.
        await self._observe_ladder(text=text, booking=booking, data_step=data_step,
                                   selecting=selecting, agent_filter=agent_filter)
        if booking is None and not data_step and selecting:
            spec = agent_filter or parse_constraints(text)
            active = spec or (state.client_filter if state.step == "slot" else None)
            if active:
                # сохраняем: блок FILTER| от агента подавляет его текст ВСЕГДА,
                # локальный разбор — только «чистую» фразу; иначе ответ агента остаётся телом сообщения
                suppress = bool(spec) and (bool(agent_filter) or is_pure_constraint(text))
                log.info("ui.filter chat=%s source=%s suppress=%s step=%s spec=%s",
                         chat_id, "agent" if agent_filter else "fallback", suppress, state.step,
                         describe(active))
                return await self._filtered_catalog(state, active, agent_text=None if suppress else clean)
            if state.step == "slot":
                #.2: текст при выбранном объекте = запрос НОВОГО выбора ⇒ объект/слот сбрасываем
                state.listing_id, state.slot, state.step = None, None, "listing"
                state.offered_slots, state.offered_listing = [], None
            stale = state.client_filter
            if stale:
                #: текстом фильтр не подтверждён ⇒ снимаем, иначе «залипнет» невидимо (кнопки сброса нет)
                state.client_filter = None
                log.info("ui.filter_reset chat=%s reason=unparsed_text stale=%s", chat_id, describe(stale))
            #.2: текст похож на условие, но локально не разобрался — говорим честно, а не «вот ваш результат»
            unparsed = looks_like_constraint(text)
            if unparsed:
                log.info("ui.filter_unparsed chat=%s step=%s", chat_id, state.step)
            state.offered_listings = [item.id for item in listings]      #: ДО отправки кнопок
            await self._save_state(state)
            note = await self._limit_note(len(listings))
            objects = _objects_text_for(listings, note=note)
            if unparsed:
                #.2: условие не применено — говорим об этом коротко, но ответ агента
                # НЕ выбрасываем: его разбор клиенту важнее нашей заготовки. Заготовка с советом
                # «что уточнить» — только когда ответа нет (канал не ответил).
                prefix = FILTER_UNPARSED_NOTE if has_answer else FILTER_UNPARSED
                body = f"{clean}\n\n{objects}" if has_answer else objects
            else:
                prefix = FILTER_RESET_NOTICE if stale else clean
                body = objects
            return Reply(text=f"{prefix}\n\n{body}".strip() if prefix else body,
                         rows=listings_kb(listings), kind=INLINE)
        if booking is not None and not self._matches_state(state, booking):
            booking, reason = None, "mismatch_state"
        if booking is None and state.step in ("confirm", "phone"):
            log.info("BOOKING не принят (%s) на шаге %s — бронь не создана", reason, state.step)

        if booking is not None and state.step in ("confirm", "phone"):
            created = self._create_booking(state, booking)
            if created is not None:
                try:
                    await self._save_booking(created)
                except ValueError:
                    await self._save_state(state)
                    return Reply(text=f"{clean}\n\nЭто время уже занято. Выберите другой слот.".strip())
                await self._notify_manager(created)
                await self._save_state(ChatState(platform=platform, chat_id=chat_id, step="done", turns=state.turns))
                masked = mask_phone(created.client_phone)
                return Reply(
                    #: объект — адресом («ул. Пушкина, 10, кв. 42»), внутренний id клиенту не показываем
                    text=(f"{clean}\n\nЗаявка принята: {created.slot}, "
                          f"объект {fmt.address_label(created.listing_id, listings)}, "
                          f"{created.client_name}, {masked}. Напишем, как только менеджер подтвердит."),
                    rows=client_menu_kb(),
                    kind=REPLY,
                )
        just_entered_name = not was_name_step and state.step == "name"
        if just_entered_name:
            clean = f"{clean}\n{MENU_RETURN_NOTE}".strip()
        await self._save_state(state)
        return Reply(text=clean, clear_keyboard=just_entered_name)

    # ---------- internals ---------- #

    def advance_state(self, state: ChatState, *, text: str, listings: list[Listing], slots: list[str]) -> ChatState:
        """Двигает шаг по ответу клиента. Нет уверенного разбора — шаг НЕ двигаем (агент переспросит)."""
        if state.step == "done":
            #: заявка оформлена — начинаем новый выбор, но НЕ теряем данные клиента и офферы
            state.listing_id, state.slot = None, None
            state.step = "listing"
            return state
        if state.step == "listing":
            chosen = explicit_object_ref(text, listings)
            if chosen:
                state.listing_id, state.step = chosen, "slot"
        elif state.step == "slot":
            chosen = match_slot(text, slots)
            if chosen:
                state.slot, state.step = chosen, "name"
        elif state.step == "name":
            name = parse_name(text)
            if name:
                state.name, state.step = name, "phone"
        elif state.step == "phone":
            phone = canonical_phone(text)
            if phone:
                state.phone, state.step = phone, "confirm"
        return state

    # ---------- кнопочный путь: шаг ведёт bridge, агент не участвует ---------- #

    async def pick_listing(self, state: ChatState, index: int) -> Reply:
        """Тап по объекту: индекс ищется в ПОКАЗАННОМ списке (whitelist), подделать нельзя."""
        listings = await self._shown_listings(state)
        if not (0 <= index < len(listings)):
            return Reply(text=STALE_OFFER_TEXT, rows=await self._catalog_rows(), kind=INLINE)
        state.listing_id = listings[index].id
        state.step = "slot"
        state.slot = None                     # слот прошлого объекта недействителен для нового ()
        state.offered_slots = await self._free_slots(state.listing_id)
        state.offered_at = _time.time()
        state.offered_listing = state.listing_id
        state.offer_reminded_at = 0.0          # новое предложение ⇒ напоминание снова уместно
        await self._save_state(state)
        return self._slots_reply(state, listings[index])

    async def refresh_offer(self, state: ChatState) -> bool:
        """Обновляет предложенные слоты, если оффер протух (TTL 20 мин). True — обновили."""
        if not state.listing_id:
            return False
        if not state.offered_slots and not state.offered_at:
            return False                    # оффера не было вовсе
        if _time.time() - state.offered_at <= OFFER_TTL_S:
            return False
        state.offered_slots = await self._free_slots(state.listing_id)
        state.offered_at = _time.time()
        state.offered_listing = state.listing_id
        state.offer_reminded_at = 0.0
        await self._save_state(state)
        return True

    async def pick_day(self, state: ChatState, day_index: int, *, back: bool = False) -> Reply:
        """Тап по дню (или «‹ Все дни»): показываем времена выбранного дня (оффер освежаем по TTL)."""
        listings = await self._listings()
        listing = next((item for item in listings if item.id == state.listing_id), None)
        expired = await self.refresh_offer(state)
        if not listing or not state.offered_slots:
            return Reply(text=STALE_OFFER_TEXT, rows=await self._catalog_rows(), kind=INLINE)
        if expired:
            return self._slots_reply(state, listing, prefix=OFFER_EXPIRED_TEXT)
        if back:
            return self._slots_reply(state, listing)
        times = time_grid(state.offered_slots, day_index)
        if not times:
            return self._slots_reply(state, listing)
        day = fmt.day_label_pretty(_day_slot_of(state.offered_slots, day_index))
        return Reply(text=SLOTS_DAY_HEADER.format(day=day), rows=times, kind=INLINE)

    async def pick_time(self, state: ChatState, slot_index: int) -> Reply:
        """Тап по времени: слот обязан быть из предложенных; протухший оффер сначала обновляем."""
        if await self.refresh_offer(state):
            listings = await self._listings()
            listing = next((item for item in listings if item.id == state.listing_id), None)
            if listing is not None:
                return self._slots_reply(state, listing, prefix=OFFER_EXPIRED_TEXT)
        if not (0 <= slot_index < len(state.offered_slots)):
            return Reply(text=STALE_OFFER_TEXT, kind=INLINE)
        chosen = state.offered_slots[slot_index]
        if await self._slot_busy_global(chosen):
            state.offered_slots = await self._free_slots(state.listing_id)
            state.offered_at = _time.time()
            state.step = "slot"
            await self._save_state(state)
            listing = next((item for item in await self._listings() if item.id == state.listing_id), None)
            if listing is not None:
                return self._slots_reply(state, listing, prefix=BUSY_TEXT)     # не голый day_grid ()
            return Reply(text=BUSY_TEXT, kind=REPLY)
        state.slot = chosen
        if state.name and state.phone:
            state.step = "confirm"           # данные уже есть — не спрашиваем заново ()
            await self._save_state(state)
            return await self.confirm_card(state)
        state.step = "name"
        await self._save_state(state)
        return Reply(text=NAME_ASK_TEXT, clear_keyboard=True)

    async def confirm_card(self, state: ChatState) -> Reply:
        """Карточка подтверждения (): телефон маской, поля только из состояния bridge."""
        if not (state.listing_id and state.slot and state.name and state.phone):
            return Reply(text="Заполните данные заявки: напишите, что ищете, — подскажу шаги.", kind=REPLY)
        listings = await self._listings()
        listing = next((item for item in listings if item.id == state.listing_id), None)
        return Reply(
            text=client_card(listing=listing, slot_iso=state.slot, name=state.name,
                             phone_mask=mask_phone(state.phone)),
            rows=confirm_kb(), kind=INLINE,
        )

    async def confirm_booking(self, state: ChatState) -> Reply:
        """Тап «✅ Подтвердить запись»: бронь создаёт ТОЛЬКО bridge и только из своего состояния.

        Гейты (): шаг обязан быть `confirm`, слот — из предложенного этому объекту; иначе старая
        карточка (кнопка без состояния) могла бы забронировать «новый объект + старое время».
        """
        if state.step != "confirm" or state.offered_listing != state.listing_id:
            return Reply(text=STALE_OFFER_TEXT, kind=REPLY)
        if not (state.listing_id and state.slot and state.name and state.phone):
            return Reply(text=STALE_OFFER_TEXT, kind=REPLY)
        if state.offered_slots and state.slot not in state.offered_slots:
            return Reply(text=STALE_OFFER_TEXT, kind=REPLY)
        if not await self._listing_exists(state.listing_id):
            state.listing_id, state.step = None, "listing"
            await self._save_state(state)
            return Reply(text=STALE_OFFER_TEXT, rows=await self._catalog_rows(), kind=INLINE)
        if await self._slot_busy_global(state.slot):
            state.offered_slots = await self._free_slots(state.listing_id)
            state.offered_at = _time.time()
            state.step = "slot"
            await self._save_state(state)
            listings = await self._listings()
            listing = next((item for item in listings if item.id == state.listing_id), None)
            if listing is not None:
                return self._slots_reply(state, listing, prefix=BUSY_TEXT)   # не тупик из пустой клавиатуры
            return Reply(text=BUSY_TEXT, kind=REPLY)
        booking = Booking(id=self._new_id(), platform=state.platform, chat_id=state.chat_id,
                          listing_id=state.listing_id,
                          slot=state.slot, client_name=state.name, client_phone=state.phone)
        try:
            await self._save_booking(booking)
        except ValueError as error:            # гонка: слот занял кто-то другой (uq_slot_active/global)
            log.warning("бронь не создана (%s): %s", type(error).__name__, error)
            state.offered_slots = await self._free_slots(state.listing_id)
            state.offered_at = _time.time()
            state.step = "slot"
            await self._save_state(state)
            listing = next((item for item in await self._listings() if item.id == state.listing_id), None)
            if listing is not None:
                return self._slots_reply(state, listing, prefix=BUSY_TEXT)
            return Reply(text=BUSY_TEXT, kind=REPLY)
        state.step = "done"
        await self._save_state(state)
        try:
            #: кнопочная бронь обязана уведомлять так же, как текстовая — иначе «Заявки (0)».
            await self._notify_manager(booking)
        except Exception as error:  # noqa: BLE001 — заявка уже создана: сбой доставки не ломает ответ клиенту
            log.warning("карточка менеджеру не отправлена (%s): заявка %s", type(error).__name__, booking.id)
        return Reply(text=DONE_TEXT, rows=done_menu_kb(), kind=REPLY)

    async def edit_field(self, state: ChatState, field: str) -> Reply:
        """«✏️ Изменить» (): сбрасываем одно поле и возвращаемся к его шагу, остальные сохраняем."""
        listings = await self._listings()
        if field == "listing":
            state.listing_id, state.slot = None, None
            state.offered_slots, state.offered_listing = [], None
            state.step = "listing"
            await self._save_state(state)
            return Reply(text=_objects_text_for(listings), rows=listings_kb(listings), kind=INLINE)
        if field == "slot":
            state.slot = None
            state.step = "slot"
            state.offered_slots = await self._free_slots(state.listing_id)
            state.offered_at = _time.time()
            state.offered_listing = state.listing_id
            await self._save_state(state)
            listing = next((item for item in listings if item.id == state.listing_id), None)
            if listing is None:
                return Reply(text=STALE_OFFER_TEXT, rows=await self._catalog_rows(), kind=INLINE)
            return self._slots_reply(state, listing)
        if field == "name":
            state.name = None
            state.step = "name"
            await self._save_state(state)
            return Reply(text=NAME_ASK_TEXT, clear_keyboard=True)
        # phone (по умолчанию)
        state.phone = None
        state.step = "phone"
        await self._save_state(state)
        return Reply(text=PHONE_ASK_TEXT, rows=phone_request_kb(), kind=REPLY)

    async def accept_contact(self, state: ChatState, phone: str) -> Reply:
        """Пришёл контакт от кнопки «📱 Отправить мой номер»: берём номер и показываем карточку."""
        canonical = canonical_phone(phone)
        if canonical is None:
            return Reply(text="Не разобрал номер — напишите его текстом, например +7 912 345-67-89.",
                         rows=phone_request_kb(), kind=REPLY)
        state.phone = canonical
        state.step = "confirm"
        await self._save_state(state)
        return await self.confirm_card(state)

    async def repeat_catalog(self, state: ChatState) -> Reply:
        """«🔄 Записаться ещё»: показываем каталог БЕЗ сброса состояния ().

        Сохраняем объект/слот/имя/телефон/фильтр — клиент выбирает только новый объект и время.
        Порядок показа фиксируем в `offered_listings` (иначе индекс кнопки ищется в другом списке).
        """
        state.offer_reminded_at = 0.0
        if state.client_filter:
            reply = await self._filtered_catalog(state, state.client_filter)
            if reply is not None:
                return reply
        listings = await self._listings()
        state.offered_listings = [item.id for item in listings]
        await self._save_state(state)
        return await self.offer_catalog(listings)

    async def _filtered_catalog(self, state: ChatState, spec: dict, *, agent_text: str | None = None) -> Reply:
        """Каталог по ограничению: запоминаем фильтр в состоянии и показываем подходящее кнопками.

        `agent_text` — ответ агента, который надо сохранить телом сообщения (случай «ограничение внутри
        вопроса»,): тогда шапку каталога (OBJECTS_TITLE/FILTER_FOUND) не добавляем, иначе
        получился бы тройной заголовок.
        """
        listings = await self._listings()
        spec = normalize_spec(spec) or {}
        shown = apply_filter(listings, spec)
        # Текст-ограничение не выбирает объект: если матчер что-то «нашёл», откатываем ()
        state.listing_id, state.slot = None, None
        state.client_filter = spec or None
        log.info("ui.filter_apply chat=%s spec=%s before=%d after=%d", state.chat_id, describe(spec),
                 len(listings), len(shown))
        #.1: объекты без данных об этаже не отбрасываем, но помечаем при этажном условии
        floor_mark = any(key in spec for key in ("floor", "floor_min", "floor_max"))
        if shown:
            note = await self._limit_note(len(shown))
            state.offered_listings = [item.id for item in shown]
            await self._save_state(state)
            if agent_text:
                head = f"{agent_text}\n{FILTER_ACTIVE.format(spec=describe(spec))}"
                body = _object_lines(shown, floor_mark=floor_mark)
            else:
                head = f"{FILTER_FOUND.format(n=len(shown))}\n{FILTER_ACTIVE.format(spec=describe(spec))}"
                body = _objects_text_for(shown, floor_mark=floor_mark)
            text = f"{head}\n\n{body}"
            return Reply(text=f"{text}\n\n{note}" if note else text,
                         rows=[*listings_kb(shown), [_reset_action()]], kind=INLINE)
        # Ничего не подошло — честно показываем всё, фильтр запоминаем, чтобы клиент мог его снять.
        # Приписки «показаны первые N из M» здесь нет и быть не может: показываем ВСЕ объекты, а не
        # совпавшие, и приписка по числу совпавших врала бы («показаны первые 0 из 4» при четырёх).
        state.offered_listings = [item.id for item in listings]
        await self._save_state(state)
        head = (f"{agent_text}\n{FILTER_ACTIVE.format(spec=describe(spec))}" if agent_text else
                f"{FILTER_NONE}\n{FILTER_ACTIVE.format(spec=describe(spec))}")
        text = f"{head}\n\n{_objects_text_for(listings, floor_mark=floor_mark)}"
        return Reply(text=text, rows=[*listings_kb(listings), [_reset_action()]], kind=INLINE)

    async def filter_reset(self, state: ChatState) -> Reply:
        """Снять ограничение и показать полный каталог (кнопка «✖ Сбросить фильтр»)."""
        state.client_filter = None
        listings = await self._listings()
        state.offered_listings = [item.id for item in listings]
        await self._save_state(state)
        return Reply(text=_objects_text_for(listings), rows=listings_kb(listings), kind=INLINE)

    async def cancel_offer(self, state: ChatState) -> Reply:
        """Тап «❌ Отменить»: сбрасываем заявку и показываем каталог."""
        state.listing_id, state.slot, state.name, state.phone = None, None, None, None
        state.step = "listing"
        state.offered_slots, state.offered_listing = [], None
        await self._save_state(state)
        return Reply(text="Хорошо, заявку отменил.", rows=client_menu_kb(), kind=REPLY)

    def reminder_reply(self) -> Reply:
        """Напоминание по свежему, но не использованному офферу ()."""
        return Reply(text=OFFER_REMINDER_TEXT,
                     rows=[[Action(OFFER_REMINDER_SHOW, KIND_BACK)],
                           [Action("❌ Отменить", KIND_CANCEL)]],
                     kind=INLINE)

    async def mark_offer_reminded(self, state: ChatState) -> None:
        """Отмечаем напоминание в состоянии, чтобы не повторять его бесконечно."""
        state.offer_reminded_at = _time.time()
        await self._save_state(state)

    def _slots_reply(self, state: ChatState, listing, *, prefix: str = "") -> Reply:
        rows = day_grid(state.offered_slots)
        if not rows:
            #: «На ближайшие дни всё занято…» + доступ к «запросу звонка» (в клиентском меню)
            return Reply(text=SLOTS_EMPTY_TEXT, rows=client_menu_kb(), kind=REPLY)
        address = fmt.full_address(listing)
        head = f"{prefix}\n\n" if prefix else ""
        return Reply(
            text=f"{head}📅 Свободное время — «{address}» (Пн–Сб, 10:00–19:00 МСК)\n\n{SLOTS_PICK_DAY_TEXT}",
            rows=rows, kind=INLINE,
        )

    async def _catalog_rows(self, listings: list | None = None) -> list[list]:
        return listings_kb(listings if listings is not None else await self._listings())

    async def _slot_busy_global(self, slot: str) -> bool:
        """Занято ли время на ЛЮБОМ объекте (). При отсутствии метода БД — проверяем по объекту."""
        if not slot:
            return False
        counter = getattr(self, "_busy_slots", None)
        if counter is None:
            return False
        try:
            return slot in await counter([slot])
        except Exception as error:  # noqa: BLE001 — проверка занятости не должна ронять подтверждение
            log.warning("проверка занятости времени недоступна: %s", type(error).__name__)
            return False

    async def _shown_listings(self, state: ChatState) -> list:
        """Список объектов В ТОМ ПОРЯДКЕ, в котором их видел клиент (DD-1).

        Если порядок не сохранён (старые состояния/текстовый путь) — отдаём полный список,
        как было раньше: без этого нумерация в тексте и кнопках разошлась бы после фильтра.
        """
        listings = await self._listings()
        if not state.offered_listings:
            return listings
        by_id = {item.id: item for item in listings}
        shown = [by_id[item_id] for item_id in state.offered_listings if item_id in by_id]
        return shown or listings

    async def offer_catalog(self, listings: list, *, source: str = "all", note: str = "") -> Reply:
        """Каталог объектов кнопками + фиксируем порядок показа в состоянии (DD-1)."""
        return Reply(
            text=_objects_text_for(listings, note=note),
            rows=listings_kb(listings) if listings else [],
            kind=INLINE,
        )

    @staticmethod
    def _matches_state(state: ChatState, booking: ParsedBooking) -> bool:
        """Агент не может подменить объект/слот: блок обязан совпасть с состоянием bridge."""
        if not state.listing_id or not state.slot:
            return False
        return booking.listing_id == state.listing_id and booking.slot == state.slot.replace(" ", "T")

    @staticmethod
    def _strip_block(text: str) -> str:
        lines = [ln for ln in (text or "").splitlines() if not ln.strip().upper().startswith("BOOKING|")]
        return "\n".join(lines).strip()

    def _create_booking(self, state: ChatState, parsed: ParsedBooking) -> Booking | None:
        """Телефон/имя — из состояния bridge; из блока берём только если в состоянии пусто.

        Так цифры телефона не уходят в LLM: агент видит маску и возвращает её в блоке,
        а реальные цифры подставляет bridge (маска в поле блока допустима).
        """
        if not state.listing_id or not state.slot:
            return None
        phone = canonical_phone(state.phone or "") or canonical_phone(parsed.phone or "")
        name = (state.name or parsed.name or "").strip()
        if not phone or not name:
            return None
        return Booking(
            id=self._new_id(),
            platform=state.platform,
            chat_id=state.chat_id,
            listing_id=state.listing_id,
            slot=state.slot,
            client_name=name,
            client_phone=phone,
        )
