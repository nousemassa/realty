"""Построение корпуса из того, что уже есть в базе.

**Что кладём в базу знаний.** Решение владельца: корпус собирается **автоматически**, без
отдельных текстов агентства. Значит источников ровно два, и оба уже существуют:

  * `listings` — карточки объектов (`address`, `price`, `rooms`);
  * `listing_jobs.fields->>'notes'` — заметки менеджеров, которые вводятся при генерации
    объявления и до сих пор **никуда не сохранялись** (в `upsert_listing` уходят только
    адрес, цена и комнаты).

**Честное ограничение этого корпуса — и его надо называть, а не прятать.** По объекту в
базу попадает то же, что и так уходит в ход агента (`context.py:35`), поэтому прирост для
диалога будет небольшим. Содержательный свободный текст — только заметки менеджеров, и
привязка «заметка ↔ объект» здесь **по адресу**: в `listing_jobs` нет `listing_id`, связь
берётся по совпадению строки адреса. Заметки, чей адрес не совпал с карточкой, кладутся
с `source_id` по своему тексту — они не потеряются, но и объекта за ними не будет.

**Нарезка.** Карточка объекта — один фрагмент: она короткая, дробить нечего. Заметка
менеджера режется по предложениям с пределом длины, чтобы фрагмент оставался осмысленным:
чанк — это то, что имеет смысл показать целиком, а не окно фиксированного размера.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Предел длины фрагмента. Заметка длиннее режется по границам предложений: обрезать
# посередине фразы хуже, чем сделать второй фрагмент.
MAX_CHUNK_CHARS = 600

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

SOURCE_LISTING = "listing"
SOURCE_NOTE = "listing_note"
SOURCE_WIKI = "wikipedia"

# Шум, а не знание. Фильтр — явный список, а не порог по длине: заметка «окна на юг»
# короче любых «разумных» 15 знаков, но это настоящее знание об объекте, и выбрасывать её
#: нельзя: порог по длине отбрасывает короткое, но осмысленное — как правило «< 40 знаков» в разборе
#: объявлений.
NOISE_NOTES = {"тест", "test", "ок", "ok", "проверка", "нет", "не указано", "-", "—", ".", ".."}


def _is_noise(note: str) -> bool:
    return note.strip().lower().strip(".!") in NOISE_NOTES


@dataclass(frozen=True)
class ChunkDraft:
    """Фрагмент до наливки в базу: без вектора, но с адресом источника."""

    source: str
    source_id: str
    chunk_no: int
    title: str
    text: str
    meta: dict


def _price_text(price: int | None) -> str:
    if not price:
        return "цена не указана"
    return f"{price:,}".replace(",", " ") + " ₽"


def chunk_listing(*, listing_id: str, address: str, price: int | None, rooms: int | None) -> list[ChunkDraft]:
    """Карточка объекта — один фрагмент.

    В текст идут и данные, и формулировка: поиск по вопросу «сколько стоит двушка на
    Садовой» должен находить карточку не только по вектору, но и по полнотексту.
    """
    address = (address or "").strip()
    if not address:
        return []
    rooms_text = f"{rooms}-комнатная" if rooms else "квартира"
    text = f"Объект: {address}. {rooms_text.capitalize()}, {_price_text(price)}."
    return [
        ChunkDraft(
            source=SOURCE_LISTING,
            source_id=listing_id,
            chunk_no=0,
            title=address,
            text=text,
            meta={"rooms": rooms, "price": price, "kind": "object_card"},
        )
    ]


def chunk_note(*, note: str, address: str = "", listing_id: str = "") -> list[ChunkDraft]:
    """Заметка менеджера — фрагменты по границам предложений.

    `source_id` — идентификатор объекта, если адрес совпал с карточкой; иначе берётся
    сама заметка. Так заметка без объекта не теряется, и повторный ингест её не дублирует.
    """
    note = (note or "").strip()
    if note == "" or _is_noise(note):
        return []

    pieces: list[str] = _split_long(note)

    source_id = listing_id or address or note[:64]
    title = address or "заметка менеджера"
    return [
        ChunkDraft(
            source=SOURCE_NOTE,
            source_id=source_id,
            chunk_no=index,
            title=title,
            text=f"{title}. {piece}" if address else piece,
            meta={"kind": "manager_note"},
        )
        for index, piece in enumerate(pieces)
    ]


def _split_long(text: str) -> list[str]:
    """Длинный текст — по границам предложений, с пределом `MAX_CHUNK_CHARS`.

    Один помощник на заметки и статьи: правило «резать по предложению, а не по знаку» должно
    быть в одном месте, иначе две копии разъедутся.
    """
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in SENTENCE_SPLIT_RE.split(text):
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > MAX_CHUNK_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


# --- чистка вики-статей ---------------------------------------------------------------
#
# Разметка Википедии знанием не является, а в промпт агента она попадала: в живом ходе
# фрагменты выглядели как «Ипотека. == Ипотечный брокеридж ==». Хуже того, заголовки и
# строки оглавления становились ОТДЕЛЬНЫМИ фрагментами — в базе из 327 фрагментов 92 (28 %)
# были короче 100 знаков, вплоть до «Ипотека. 1» (10 знаков). Агент на таком «знании»
# честно отказывался отвечать, и выглядело это как осторожность, а не как дефект корпуса.
WIKI_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.DOTALL | re.IGNORECASE)
WIKI_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
WIKI_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")
WIKI_QUOTES_RE = re.compile(r"'{2,5}")
WIKI_HEADING_RE = re.compile(r"^\s*=+\s*(?P<title>.+?)\s*=+\s*$")
WIKI_TABLE_ROW_RE = re.compile(r"^\s*[|!].*$")
WIKI_LIST_MARKER_RE = re.compile(r"^\s*(?:[*#]+|\d+[.)]?|[A-Za-zА-Яа-я][.)]?)\s*$")

#: Признак ПРОЗЫ. Это правило формы, а не порог по длине: порог оставляет без знания и короткую
#: заметку («окна на юг»), и длинный заголовок без фактов.
# Заголовок `== Примечания ==` может быть длинным и всё равно не нести ни одного факта, а
# короткая заметка «окна на юг» — нести. Требуем минимум два слова со буквами.
PROSE_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]{3,}")


# Служебные разделы статьи: навигация и библиография, а не знание о предмете. Сравниваем по
# ПОСЛЕДНЕМУ слову заголовка, потому что бывают «Специальная литература» и «Источники
# информации»: список при этом остаётся явным, а не «по вхождению подстроки».
SERVICE_SECTION_WORDS = frozenset({
    "также", "ссылки", "примечания", "литература", "источники", "библиография", "сноски",
    "notes", "references", "links",
})

# Географические однофамильцы. Статьи «Залог», «Новостройка», «Аренда» в Википедии описывают
# ещё и населённые пункты с таким названием. Для продукта это не знание, а ловушка: фрагмент
# «Залог — деревня в Кличевском районе» выглядит как ответ про залог недвижимости и уводит
# клиента в сторону. Шаблоны перечислены явно — по формам, которые реально встретились.
GEO_PATTERNS = (
    re.compile(r"\b(?:село|деревня|пос[её]лок|хутор|станица|аул|аал|разъезд)\s+в\s+\S+\s+районе",
               re.IGNORECASE),
    re.compile(r"[-—–]\s*(?:историческая\s+местность|местность|микрорайон)\b", re.IGNORECASE),
    re.compile(r"[-—–]\s*(?:село|деревня|пос[её]лок|хутор|станица|аул|аал|разъезд)\b", re.IGNORECASE),
)


def _is_geo_homonym(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in GEO_PATTERNS)


def _is_service_section(title: str) -> bool:
    words = [w.strip(" .,:;()[]") for w in (title or "").lower().split()]
    return bool(words) and words[-1] in SERVICE_SECTION_WORDS


def _clean_wiki(text: str) -> str:
    """Снять разметку: ссылки, шаблоны, сноски, апострофы выделения."""
    cleaned = WIKI_REF_RE.sub(" ", text or "")
    cleaned = WIKI_TEMPLATE_RE.sub(" ", cleaned)
    cleaned = WIKI_LINK_RE.sub(r"\1", cleaned)
    cleaned = WIKI_QUOTES_RE.sub("", cleaned)
    return " ".join(cleaned.split())


def _has_prose(text: str) -> bool:
    """Есть ли во фрагменте текст, а не заголовок, оглавление или одиночный символ."""
    words = [word for word in (text or "").split() if PROSE_WORD_RE.search(word)]
    return len(words) >= 2


def _sections(body: str) -> list[tuple[str, str]]:
    """Разделы статьи: `(заголовок раздела, текст)`, разметка снята.

    Заголовок не становится отдельным фрагментом — он идёт ПРИСТАВКОЙ к тексту своего
    раздела: как ориентир он полезен (агент видит, о чём фрагмент), а сам по себе знанием
    не является. Разделы без прозы (пустые, из одних маркеров) не возвращаются вовсе.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in (body or "").splitlines():
        heading = WIKI_HEADING_RE.match(line)
        if heading:
            sections.append((_clean_wiki(heading.group("title")), []))
            continue
        if WIKI_TABLE_ROW_RE.match(line) or WIKI_LIST_MARKER_RE.match(line):
            continue          # строки таблиц и маркеры списков оглавления — не абзацы
        sections[-1][1].append(line)

    result: list[tuple[str, str]] = []
    for title_text, lines in sections:
        if _is_service_section(title_text):
            continue          # навигация и библиография — не знание о предмете
        paragraphs = [_clean_wiki(p) for p in re.split(r"\n\s*\n", "\n".join(lines))]
        text = " ".join(p for p in paragraphs if p).strip()
        if text and _has_prose(text):
            result.append((title_text, text))
    return result


def chunk_article(*, title: str, text: str, revid: int = 0, license_name: str = "") -> list[ChunkDraft]:
    """Статья-источник — фрагменты по разделам и абзацам, без разметки и без пустышек.

    `source_id` — заголовок статьи, `revid` уходит в метаданные: по нему видно, какая
    именно ревизия текста попала в корпус, и воспроизводимость не теряется.

    **Что изменилось и почему.** Раньше каждая строка-абзац становилась фрагментом, включая
    заголовки (`== Примечания ==`) и маркеры оглавления (`1`, `A`). Из-за этого 28 % корпуса
    были мусором, а агент, получив такой фрагмент, честно отказывался отвечать. Теперь
    фрагмент — только текст с прозой, а заголовок идёт приставкой к своему разделу.
    """
    title = (title or "").strip()
    body = (text or "").strip()
    if not title or not body:
        return []

    drafts: list[ChunkDraft] = []
    for section_title, section_text in _sections(body):
        label = f"{title}. {section_title}." if section_title else f"{title}."
        for piece in _split_long(section_text):
            if not _has_prose(piece):
                continue
            if _is_geo_homonym(piece):
                continue      # населённый пункт-однофамилец, а не знание о недвижимости
            drafts.append(
                ChunkDraft(
                    source=SOURCE_WIKI,
                    source_id=title,
                    chunk_no=len(drafts),
                    title=title,
                    text=f"{label} {piece}",
                    meta={
                        "kind": "article", "revid": revid, "license": license_name,
                        "section": section_title,
                    },
                )
            )
    return drafts


def build_corpus(
    *, listings: list[dict], notes: list[dict]
) -> list[ChunkDraft]:
    """Собрать корпус целиком: карточки объектов плюс заметки менеджеров.

    Заметка привязывается к объекту по адресу — точного ключа в схеме нет, и это
    ограничение названо в докстринге модуля, а не спрятано.
    """
    by_address = {(item.get("address") or "").strip(): str(item.get("id") or "") for item in listings}

    drafts: list[ChunkDraft] = []
    for listing in listings:
        drafts.extend(
            chunk_listing(
                listing_id=str(listing.get("id") or ""),
                address=listing.get("address") or "",
                price=listing.get("price"),
                rooms=listing.get("rooms"),
            )
        )
    for note in notes:
        address = (note.get("address") or "").strip()
        drafts.extend(chunk_note(note=note.get("notes") or "", address=address, listing_id=by_address.get(address, "")))
    return drafts


async def upsert_chunks(pool, *, drafts: list[ChunkDraft], vectors: list[list[float]]) -> int:
    """Записать фрагменты с векторами. Идемпотентно: ключ `(source, source_id, chunk_no)`.

    Повторный ингест обновляет те же строки, а не плодит дубли — иначе корпус рос бы при
    каждом запуске, и `recall@5` поехал бы просто из-за объёма.
    """
    if len(drafts) != len(vectors):
        raise ValueError(f"черновиков {len(drafts)}, векторов {len(vectors)} — обязаны совпадать")

    written = 0
    async with pool.acquire() as connection, connection.transaction():
        for draft, vector in zip(drafts, vectors, strict=True):
            literal = "[" + ",".join(f"{value:.7f}" for value in vector) + "]"
            await connection.execute(
                """
                    INSERT INTO knowledge_chunks (source, source_id, chunk_no, title, text, embedding, meta)
                    VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb)
                    ON CONFLICT (source, source_id, chunk_no) DO UPDATE
                       SET title = EXCLUDED.title,
                           text = EXCLUDED.text,
                           embedding = EXCLUDED.embedding,
                           meta = EXCLUDED.meta,
                           updated_at = now()
                    """,
                draft.source, draft.source_id, draft.chunk_no, draft.title, draft.text,
                literal, _json(draft.meta),
            )
            written += 1
    return written


async def prune_chunks(pool, *, source: str, keep: dict[str, int]) -> int:
    """Удалить фрагменты источника, которых больше не должно быть.

    Зачем отдельным шагом, а не внутри `upsert_chunks`: upsert идемпотентен по
    `(source, source_id, chunk_no)` и трогает **только те номера, что пришли**. После чистки
    чанкера фрагментов у статьи стало меньше (на живом корпусе 322 → 231), поэтому без
    подчистки «хвоста» старые мусорные фрагменты остались бы в базе — и чистка не дала бы
    ничего, кроме добавления новых строк.

    `keep` — карта «источник → сколько фрагментов оставить». Источник, которого в карте нет,
    считается пустым: всё его содержимое удаляется.
    """
    removed = 0
    async with pool.acquire() as connection, connection.transaction():
        rows = await connection.fetch(
            "SELECT DISTINCT source_id FROM knowledge_chunks WHERE source = $1", source)
        for row in rows:
            source_id = row["source_id"]
            result = await connection.execute(
                "DELETE FROM knowledge_chunks WHERE source = $1 AND source_id = $2 AND chunk_no >= $3",
                source, source_id, keep.get(source_id, 0),
            )
            removed += int(str(result).rsplit(" ", 1)[-1] or 0)
    return removed


def _json(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
