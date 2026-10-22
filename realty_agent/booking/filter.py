"""Фильтр объектов по ограничению клиента (DD-7/DD-8).

Почему отдельный модуль и почему «детерминированно первым»: разбор ограничения («до 7 млн»,
«двушка», «от 50 м²») — это арифметика, а не семантика, и стоит 0 LLM-ходов. Агент подключается
только если локально разобрать не удалось (тогда он предлагает блок `FILTER|{...}`, а применяет его
ВСЁ РАВНО мост — агент не может подсунуть объект, которого нет).

Границы (): «ремонт», этаж и метро ищутся по тексту адреса — там они и лежат
(`… (2-комн., 54 м², 7/9 эт., 5 мин до метро)`), отдельных колонок в схеме нет. Отрицания поддержаны:
«без ремонта» → `exclude_keywords`, «не студия» → `exclude_rooms`, «не первый этаж» → `floor_not`,
«не последний этаж» → `exclude_last_floor` (этажность выводится из «N/M эт.», иначе объект НЕ отбрасываем).

Порядок разбора принципиален (иначе инверсия): СНАЧАЛА вырезаем негативы из текста, ПОТОМ ищем позитивы
по очищенному тексту и вычитаем негативные ключи — иначе «без ремонта» ставило бы требование ремонта.
"""
from __future__ import annotations

import re

from realty_agent.channels.turn_context import Listing
from realty_agent.ui import format as fmt

# Ключи, которые разрешено принимать и от агента (белый список — чужие поля игнорируем).
ALLOWED_KEYS = ("rooms", "price_max", "price_min", "area_min", "keywords",
                "exclude_keywords", "exclude_rooms", "floor_not", "exclude_last_floor",
                #: этажные СРАВНЕНИЯ («выше второго», «до 5 этажа», «на 3 этаже»)
                "floor", "floor_min", "floor_max")
LIST_KEYS = ("keywords", "exclude_keywords", "exclude_rooms", "floor_not")

ROOM_WORDS = {
    # «студия» в базе хранится как rooms=1 ⇒ фильтруем словом «студи», а не равенством комнатности
    "студия": -1, "студию": -1, "студии": -1,
    "однушка": 1, "однушку": 1, "однокомнатная": 1, "1-комнатная": 1,
    "двушка": 2, "двушку": 2, "двухкомнатная": 2, "2-комнатная": 2,
    "трёшка": 3, "трешка": 3, "трёшку": 3, "трешку": 3, "трёхкомнатная": 3, "3-комнатная": 3,
}
MONEY_RE = re.compile(r"(до|от|не дороже|не дешевле)\s*(\d+(?:[.,]\d+)?)\s*(млн|млрд|тыс|тысяч|к)?", re.IGNORECASE)
AREA_RE = re.compile(r"(?:от|больше|не меньше)\s*(\d{2,3})\s*(?:м²|м2|кв\.?\s*м|квадрат)", re.IGNORECASE)
ROOMS_RE = re.compile(r"(\d)\s*[- ]?комнат", re.IGNORECASE)
# «этаж» убран: в адресах стоит «эт.», из-за чего фильтр по слову «этаж» давал пустой каталог ()
KEYWORD_WORDS = ("ремонт", "метро", "парк", "балкон", "лоджия", "центр")
# Этаж/этажность живут в скобках адреса: «7/9 эт.», «14/14 эт.»
FLOOR_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*эт", re.IGNORECASE)
# Порядковые слова → числа: «второго этажа» разбираем как «2 этажа» (иначе нужны десятки регексов)
ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5,
            "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}
ORDINAL_RE = re.compile(r"\b(перв|втор|трет|четв|пят|шест|седьм|восьм|девят|десят)\w*", re.IGNORECASE)
# Этажные сравнения (после порядок обязателен: сначала «не выше/не ниже», потом «выше/ниже», потом «от/до/с/на»)
NOT_ABOVE_RE = re.compile(r"\bне\s+выше\s+(\d{1,2})\s*(?:-?го)?\s*этаж\w*")
NOT_BELOW_RE = re.compile(r"\bне\s+ниже\s+(\d{1,2})\s*(?:-?го)?\s*этаж\w*")
ABOVE_RE = re.compile(r"\b(?:выше|больше|старше)\s+(\d{1,2})\s*(?:-?го)?\s*этаж\w*")
BELOW_RE = re.compile(r"\b(?:ниже|меньше)\s+(\d{1,2})\s*(?:-?го)?\s*этаж\w*")
FROM_RE = re.compile(r"\b(?:от|с|начиная\s+с)\s+(\d{1,2})\s*(?:-?го)?\s*этаж\w*")
TO_RE = re.compile(r"\b(?:до|по)\s+(\d{1,2})\s*(?:-?го|й)?\s*этаж\w*")
AT_RE = re.compile(r"\b(?:на\s+)?(\d{1,2})\s*(?:-?м)?\s*этаж\w*")
# Негативы НЕ должны съедать слова сравнения (иначе «не выше» → мусорное «без: выше», «не дороже» → теряется цена)
NEG_STOP = ("выше", "ниже", "дороже", "дешевле", "этаж", "этажность",
            "перв", "последн", "больше", "меньше",
            #: предлоги — служебные слова, в негативы попадать не должны («без: возле»)
            "возле", "около", "рядом", "близ", "подле", "прямо")


def _digits(text: str) -> str:
    """«второго этажа» → «2 этажа»: порядковые слова приводим к цифрам (для этажных сравнений)."""
    return ORDINAL_RE.sub(lambda m: str(ORDINALS[m.group(1).lower()]), text)


# Сравнения с ПОРЯДКОВЫМ словом без слова «этаж» («не ниже третьего», «выше второго»):
# безопасны, потому что порядковое слово не может быть ценой/площадью ()
ORD_CMP_RE = re.compile(
    r"\b(не\s+)?(выше|ниже|больше|меньше|от|с|до|по)\s+"
    r"(перв|втор|трет|четв|пят|шест|седьм|восьм|девят|десят)\w*", re.IGNORECASE)


NOT_LAST_RE = re.compile(r"\bне\s+(?:на\s+)?последн\w*(?:\s*этаж\w*)?")
NOT_FIRST_RE = re.compile(r"\bне\s+(?:на\s+)?(?:перв\w*(?:\s*этаж\w*)?|1\s*этаж\w*)")
#: между «не/без» и словом может стоять предлог («не у парка», «не с ремонтом», «не возле метро»),
# иначе слово уходило в ПОЗИТИВНЫЕ ключевые слова — мост «выбирал наоборот».
PREPOSITIONS = r"у|с|со|в|во|на|за|перед|возле|около|рядом(?:\s+с)?|близ|подле|прямо"
_EXCL_SEP = rf"\s+(?:(?:{PREPOSITIONS})\s+)*"
EXCL_RE = re.compile(rf"\bбез{_EXCL_SEP}([а-яё]{{4,}})|\bне{_EXCL_SEP}([а-яё]{{4,}})")


def parse_constraints(text: str) -> dict | None:
    """Детерминированный разбор ограничения (позитивы, негативы, этажные сравнения).

    Один рабочий текстовый поток и строгий порядок вырезания фраз — иначе части перекрывают друг друга:
    1) сравнения с порядковым словом («не ниже третьего», «выше второго») — по СЛОВАМ;
    2) порядковые → цифры (`_digits`);
    3) «не последний/не первый», затем «не выше/не ниже N этажа»;
    4) прочие негативы («без ремонта») со стоп-листом слов сравнения (иначе «не дороже 7 млн» теряло цену);
    5) позитивные этажные сравнения («выше/ниже/от/до/на N этаже»);
    6) комнатность, цена, площадь, ключевые слова — по уже очищенному тексту.
    """
    spec: dict = {}
    work = (text or "").lower()

    # 1) сравнения с порядковым словом: «не выше второго» ⇒ ≤2, «не ниже третьего» ⇒ ≥3
    for found in list(ORD_CMP_RE.finditer(work)):
        negation = bool(found.group(1))
        comparison, number = found.group(2).lower(), ORDINALS[found.group(3).lower()]
        if comparison in ("выше", "больше"):
            spec.setdefault("floor_max" if negation else "floor_min",
                            number if negation else number + 1)
        elif comparison in ("ниже", "меньше"):
            spec.setdefault("floor_min" if negation else "floor_max",
                            number if negation else number - 1)
        else:
            spec["floor_min" if comparison in ("от", "с") else "floor_max"] = number
        work = work.replace(found.group(0), " ", 1)

    # 2) дальше этажные условия удобнее разбирать по цифрам
    work = _digits(work)

    # 3) «не последний этаж» / «не первый этаж» и «не выше/не ниже N этажа»
    if NOT_LAST_RE.search(work):
        spec["exclude_last_floor"] = True
    if NOT_FIRST_RE.search(work):
        spec["floor_not"] = [1]
    work = NOT_FIRST_RE.sub(" ", NOT_LAST_RE.sub(" ", work))
    for pattern, key in ((NOT_ABOVE_RE, "floor_max"), (NOT_BELOW_RE, "floor_min")):
        found = pattern.search(work)
        if found:
            spec[key] = int(found.group(1))
            work = pattern.sub(" ", work)

    # 4) прочие негативы: «без ремонта», «не студия»
    excluded: list[str] = []
    for without, negation in EXCL_RE.findall(work):
        word = without or negation
        if word.startswith(NEG_STOP):
            continue
        if word in ROOM_WORDS:
            rooms = ROOM_WORDS[word]
            if rooms < 0:
                excluded.append("студи")
            else:
                spec.setdefault("exclude_rooms", []).append(rooms)
        else:
            excluded.append(word)
    if excluded:
        spec["exclude_keywords"] = list(dict.fromkeys(excluded))
        work = EXCL_RE.sub(" ", work)

    # 5) позитивные этажные сравнения
    for pattern, key, delta in ((ABOVE_RE, "floor_min", 1), (BELOW_RE, "floor_max", -1),
                                (FROM_RE, "floor_min", 0), (TO_RE, "floor_max", 0),
                                (AT_RE, "floor", 0)):
        found = pattern.search(work)
        if not found:
            continue
        number = int(found.group(1)) + delta
        if number >= 1 and key not in spec:
            spec[key] = number
        work = pattern.sub(" ", work)

    # 6) комнатность, цена, площадь, ключевые слова
    for word, rooms in ROOM_WORDS.items():
        if re.search(rf"\b{word}", work):
            if rooms < 0:
                spec.setdefault("keywords", []).append("студи")
            elif rooms not in spec.get("exclude_rooms", []):
                spec["rooms"] = rooms
            break
    match = ROOMS_RE.search(work)
    if "rooms" not in spec and match and int(match.group(1)) not in spec.get("exclude_rooms", []):
        spec["rooms"] = int(match.group(1))

    money = MONEY_RE.search(work)
    if money:
        value = float(money.group(2).replace(",", "."))
        unit = (money.group(3) or "").lower()
        if unit in ("млн", "млрд"):
            value = int(value * (1000 if unit == "млн" else 1_000_000) * 1000)
        elif unit in ("тыс", "тысяч", "к"):
            value = int(value * 1000)
        elif value < 1000 and not re.match(
                r"\s*(?:-?\s*[а-яё]{0,3}\s*)?(м²|м2|кв|этаж|эт)", work[money.end():money.end() + 12]):
            value = int(value * 1_000_000)           # «до 7» без единиц — обычно миллионы
        elif value >= 1000:
            value = int(value)
        else:
            value = 0                                # «от 50 м²» — это площадь, а не цена
        if value:
            spec["price_max" if money.group(1).lower() in ("до", "не дороже") else "price_min"] = value

    area = AREA_RE.search(work)
    if area:
        spec["area_min"] = int(area.group(1))

    keywords = list(spec.get("keywords", []))
    keywords += [word for word in KEYWORD_WORDS if word in work]
    #: сравниваем по ОСНОВЕ — «не у парка» (исключаем «парка») не должно оставлять позитив «парк»
    excluded_stems = {_base(word) for word in excluded}
    keywords = [word for word in dict.fromkeys(keywords) if _base(word) not in excluded_stems]
    if keywords:
        spec["keywords"] = keywords

    return spec or None

# Признаки того, что клиент задал УСЛОВИЕ (даже если локально разобрать не удалось) —.2.
CONSTRAINT_WORDS_RE = re.compile(
    r"\b(этаж\w*|эт\.|выше|ниже|дороже|дешевл\w*|подешевле|подороже|дорог\w*|больше|меньше|"
    r"ремонт\w*|метро|парк\w*|балкон\w*|лоджи\w*|центр\w*|комнат\w*|студи\w*|квадрат\w*|метраж\w*|"
    r"вид|окн\w*|мор\w*|пляж\w*|парковк\w*|кладовк\w*|лифт\w*|новостройк\w*)\b"
)
CONSTRAINT_UNITS_RE = re.compile(
    r"\d+\s*(?:м²|м2|кв\.?\s*м|км|мин|млн|млрд|тыс|этаж\w*|эт\.?|комнат\w*)"
)
QUESTION_RE = re.compile(r"\?|\b(как\w*|сколько|где|когда|можно|есть\s+ли|что|кто)\b")


def looks_like_constraint(text: str) -> bool:
    """Похоже ли сообщение на ОГРАНИЧЕНИЕ (для честной строки «условие не распознал»)?

    Вопрос-гард обязателен: «а какой у вас график?» и «расскажите про ипотеку» — это вопросы, а не условия.
    """
    low = (text or "").lower()
    if CONSTRAINT_UNITS_RE.search(low):
        return True
    if not CONSTRAINT_WORDS_RE.search(low):
        return False
    return not QUESTION_RE.search(low)

STOP_WORDS = {
    "ищу", "ищем", "найди", "найдите", "покажи", "покажите", "хочу", "хотел", "хотела", "нужна",
    "нужен", "нужно", "нужны", "есть", "можно", "дайте", "подбери", "подберите", "вариант", "варианты",
    "квартиру", "квартира", "квартиры", "объект", "объекты", "с", "в", "на", "и", "не", "без", "до", "от",
    "млн", "млрд", "тыс", "тысяч", "руб", "рублей", "м²", "м2", "кв", "кв.м", "эт", "этаж", "этажность",
    "комнат", "комнатная", "пожалуйста", "подскажите", "рассматриваю", "интересует", "если", "что", "то",
    #: без этих слов «двушка не первый этаж» не считалось «чистым» ограничением
    "первый", "первом", "первого", "последний", "последнем", "последнего",
    #: этажные сравнения — тоже часть ограничения («выше второго этажа» ⇒ одно сообщение с каталогом)
    "выше", "ниже", "этаже",
}


def is_pure_constraint(text: str) -> bool:
    """Похоже ли сообщение ТОЛЬКО на ограничение (без вопроса/просьбы по существу)?

    Нужно фолбэку: если агент не вернул блок `FILTER|`, локальный разбор применяется лишь к «чистому»
    ограничению — иначе мы потеряли бы ответ агента на вопрос клиента.
    """
    cleaned = re.sub(r"[^\wа-яёА-ЯЁ\s]", " ", text or "").lower()
    rest = [word for word in cleaned.split() if word and word not in STOP_WORDS
            and not word.isdigit() and len(word) > 2]
    return len(rest) <= 1


def _base(word: str) -> str:
    """Основа слова для сравнения форм: «ремонта»/«ремонтом»/«ремонт» → «ремо»; «парка»/«парк» → «парк».

    4 буквы (а не 5, как раньше): иначе падежи не сходятся — «парка» давала «парка», а «парк» — «парк»,
    и исключение «не у парка» не срабатывало (). Корни в адресах короткие, ложных склеек нет.
    """
    return word.strip().lower()[:4]


def normalize_spec(spec: dict | None) -> dict | None:
    """Привести блок (в т.ч. от агента) к белому списку и правильным типам. Мусор → None."""
    if not isinstance(spec, dict):
        return None
    clean: dict = {}
    for key, value in spec.items():
        if key not in ALLOWED_KEYS or value in (None, "", [], {}):
            continue
        # Санитизация от агента: «keywords:['без ремонта']» — это НЕГАТИВ, а не ключевое слово ()
        if key == "keywords" and isinstance(value, list):
            positive, negative = [], []
            for item in value:
                text = str(item).strip().lower()
                if re.match(r"^(без|не)\s+", text):
                    negative.append(re.sub(r"^(без|не)\s+", "", text))
                else:
                    positive.append(text)
            if positive:
                clean["keywords"] = [word for word in positive if word]
            if negative:
                clean["exclude_keywords"] = list(dict.fromkeys(negative))
            continue
        if key == "exclude_last_floor":
            clean[key] = bool(value)
            continue
        if key in LIST_KEYS:
            items = value if isinstance(value, list) else [value]
            numbers: list[int] = []
            for item in items:
                try:
                    numbers.append(int(float(item)))
                except (TypeError, ValueError):
                    text = str(item).strip().lower()
                    if text:
                        numbers.append(text)          # ключевые слова остаются строками
                continue
            if numbers:
                clean[key] = numbers if key != "keywords" else [str(n) for n in numbers]
            continue
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            continue
        if key == "rooms" and not 0 <= number <= 6:
            continue
        if key != "rooms" and number <= 0:
            continue
        clean[key] = number
    return clean or None


def describe(spec: dict | None) -> str:
    """Человекочитаемое описание фильтра для шапки каталога: «2-комн., до 7 млн ₽, не 1 этаж»."""
    if not spec:
        return ""
    parts: list[str] = []
    if "rooms" in spec:
        parts.append("студия" if spec["rooms"] == 0 else f"{spec['rooms']}-комн.")
    if "price_max" in spec:
        parts.append(f"до {fmt.short_price(str(spec['price_max']))}")
    if "price_min" in spec:
        parts.append(f"от {fmt.short_price(str(spec['price_min']))}")
    if "area_min" in spec:
        parts.append(f"от {spec['area_min']} м²")
    if spec.get("keywords"):
        parts.append(", ".join(str(word) for word in spec["keywords"]))
    if spec.get("exclude_keywords"):
        parts.append("без: " + ", ".join(str(word) for word in spec["exclude_keywords"]))
    if spec.get("exclude_rooms"):
        parts.append("не " + ", ".join(f"{int(room)}-комн." for room in spec["exclude_rooms"]))
    if "floor" in spec:
        parts.append(f"{spec['floor']} этаж")
    if "floor_min" in spec:
        parts.append(f"от {spec['floor_min']} этажа")
    if "floor_max" in spec:
        parts.append(f"до {spec['floor_max']} этажа")
    if spec.get("floor_not"):
        parts.append("не " + ", ".join(f"{int(floor)} этаж" for floor in spec["floor_not"]))
    if spec.get("exclude_last_floor"):
        parts.append("не последний этаж")
    return ", ".join(parts)


def floor_info(address: str | None) -> tuple[int, int] | None:
    """(этаж, этажность) из скобок адреса («7/9 эт.»); None — данных нет (объект не отбрасываем)."""
    found = FLOOR_RE.search(address or "")
    return (int(found.group(1)), int(found.group(2))) if found else None


def apply_filter(listings: list[Listing], spec: dict | None) -> list[Listing]:
    """Сузить каталог по ограничению (позитивы И негативы). Ничего не найдено → пустой список."""
    spec = normalize_spec(spec)
    if not spec:
        return list(listings)

    def matches(item: Listing) -> bool:
        address = (item.address or "").lower()
        if "rooms" in spec:
            rooms = str(item.rooms or "").strip()
            if not rooms.isdigit() or int(rooms) != spec["rooms"]:
                return False
        if spec.get("exclude_rooms"):
            rooms = str(item.rooms or "").strip()
            if rooms.isdigit() and int(rooms) in spec["exclude_rooms"]:
                return False
        price = str(item.price or "").replace(" ", "").replace("\u00a0", "")
        if "price_max" in spec and price.isdigit() and int(price) > spec["price_max"]:
            return False
        if "price_min" in spec and price.isdigit() and int(price) < spec["price_min"]:
            return False
        if "area_min" in spec:
            found = re.search(r"(\d{2,3})\s*м²", fmt.features(item.address))
            if not found or int(found.group(1)) < spec["area_min"]:
                return False
        for word in spec.get("keywords", []):
            if _base(str(word)) not in address:
                return False
        for word in spec.get("exclude_keywords", []):
            if _base(str(word)) in address:          # «ремонт» не должен совпасть с «ремонтом»? — должен
                return False
        floor = floor_info(item.address)
        if floor:                                    # данных нет ⇒ объект оставляем (помечаем в каталоге)
            if spec.get("floor") and floor[0] != spec["floor"]:
                return False
            if spec.get("floor_min") and floor[0] < spec["floor_min"]:
                return False
            if spec.get("floor_max") and floor[0] > spec["floor_max"]:
                return False
        if spec.get("floor_not") and floor and floor[0] in spec["floor_not"]:
            return False
        return not (spec.get("exclude_last_floor") and floor and floor[0] == floor[1])

    return [item for item in listings if matches(item)]
