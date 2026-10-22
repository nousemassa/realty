"""Офлайн-тесты поиска RAG.

Что проверяется БЕЗ сети и БД:

  * клиент эмбеддингов: корректный разбор ответа, отказ при сетевой ошибке
    (`EmbeddingUnavailable`) и при несовпадении размерности (`EmbeddingDimensionMismatch`);
  * ретривер: при недоступных эмбеддингах падает на полнотекстовую половину, а не молчит
    и не бросает; пустой запрос — пустой результат; шов подменяется двойником;
  * сборка корпуса: карточка объекта и заметка менеджера, привязка по адресу, отброс шума
    и сохранение короткого, но осмысленного знания;
  * идемпотентность наливки: рассинхрон черновиков и векторов — ошибка, а не тихая запись;
  * ход диалога: блок «База знаний» появляется только при найденных фрагментах и несёт
    пометку «недоверенный текст»;
  * `DialogService._knowledge_for` не бросает НИКОГДА: сбой поиска — это «нет знаний».

Схема векторной базы (расширение, размерность, HNSW) проверяется отдельным тестом,
который пропускается, если базы нет под рукой: он требует живой PostgreSQL.
"""
from __future__ import annotations

import json
from typing import ClassVar, Self

import httpx
import pytest
import yaml

from agent_core.db.schema import EMBEDDING_DIM, VECTOR_DDL, VECTOR_SCHEMA_CHECK_EXPECTED
from agent_core.retrieval.embeddings import (
    EmbeddingDimensionMismatch,
    EmbeddingUnavailable,
    embed_texts,
    probe_dimension,
)
from agent_core.retrieval.pgvector import PgVectorRetriever, _vector_literal
from realty_agent.channels.turn_context import Listing, TurnContext, build_turn_text
from realty_agent.retrieval.ingest import (
    MAX_CHUNK_CHARS,
    build_corpus,
    chunk_article,
    chunk_listing,
    chunk_note,
    upsert_chunks,
)
from realty_agent.retrieval.retriever import RealtyRetriever


def _vector_dsn() -> str:
    """Адрес векторной базы для живого теста: только из окружения, без литеральных кредов.

    Нет адреса или пароля — тест пропускается: подставлять «username:password» значило бы проверять
    выдуманную базу и прятать пропущенную настройку.
    """
    import os

    url = os.environ.get("VECTOR_DB_URL", "")
    password = os.environ.get("VECTOR_DB_PASSWORD", "")
    if not url or not password:
        import pytest

        pytest.skip("нет VECTOR_DB_URL/VECTOR_DB_PASSWORD — живой тест векторной базы пропущен")
    parsed = url.replace("postgresql+asyncpg://", "postgresql://").rsplit("@", 1)
    if len(parsed) != 2:
        import pytest

        pytest.skip("VECTOR_DB_URL без кредов — живой тест векторной базы пропущен")
    return f"{parsed[0]}@127.0.0.1:55433/{parsed[1].split('/', 1)[1]}"


# --- подмена HTTP: без сети ----------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Заменяет `httpx.AsyncClient` в тестах: отдаёт заранее заданный ответ."""

    payload: ClassVar[dict] = {}
    status: ClassVar[int] = 200
    error: ClassVar[Exception | None] = None

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def post(self, *_args, **_kwargs) -> _FakeResponse:
        if _FakeAsyncClient.error is not None:
            raise _FakeAsyncClient.error
        return _FakeResponse(_FakeAsyncClient.payload, _FakeAsyncClient.status)


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    _FakeAsyncClient.payload = {}
    _FakeAsyncClient.status = 200
    _FakeAsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    yield


def _vector(dim: int = EMBEDDING_DIM, value: float = 0.01) -> list[float]:
    return [value] * dim


# --- клиент эмбеддингов --------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_texts_parses_response():
    _FakeAsyncClient.payload = {"data": [{"embedding": _vector()}, {"embedding": _vector()}]}

    vectors = await embed_texts(["a", "b"], base_url="http://x/v1", api_key="k", model="m")

    assert len(vectors) == 2 and len(vectors[0]) == EMBEDDING_DIM


@pytest.mark.asyncio
async def test_embed_texts_raises_on_network_error():
    _FakeAsyncClient.error = httpx.ConnectError("нет сервиса")

    with pytest.raises(EmbeddingUnavailable):
        await embed_texts(["a"], base_url="http://x/v1", api_key="k", model="m")


@pytest.mark.asyncio
async def test_embed_texts_raises_on_wrong_dimension():
    """Усечённая сборка модели: схема и вектор разной длины — сравнивать нельзя."""
    _FakeAsyncClient.payload = {"data": [{"embedding": _vector(dim=512)}]}

    with pytest.raises(EmbeddingDimensionMismatch):
        await embed_texts(["a"], base_url="http://x/v1", api_key="k", model="m")


@pytest.mark.asyncio
async def test_embed_texts_raises_on_wrong_count():
    _FakeAsyncClient.payload = {"data": [{"embedding": _vector()}]}

    with pytest.raises(EmbeddingUnavailable):
        await embed_texts(["a", "b"], base_url="http://x/v1", api_key="k", model="m")


@pytest.mark.asyncio
async def test_probe_dimension_reports_model_dimension():
    _FakeAsyncClient.payload = {"data": [{"embedding": _vector(dim=768)}]}

    assert await probe_dimension(base_url="http://x/v1", api_key="k", model="m") == 768


def test_vector_literal_format():
    assert _vector_literal([1.0, -0.5]) == "[1.0000000,-0.5000000]"


# --- ретривер ------------------------------------------------------------------------

class _FakePool:
    """Двойник пула: возвращает заранее заданные строки и считает запросы."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.calls: list[str] = []

    async def fetch(self, sql: str, *args):
        self.calls.append("hybrid" if "vector_hits" in sql else "text")
        return self._rows


def _retriever(pool) -> PgVectorRetriever:
    return RealtyRetriever(pool, embed_url="http://x/v1", embed_api_key="k", embed_model="m")


@pytest.mark.asyncio
async def test_retriever_uses_hybrid_when_embeddings_available():
    _FakeAsyncClient.payload = {"data": [{"embedding": _vector()}]}
    pool = _FakePool([{"source": "listing", "source_id": "L-1", "title": "t", "text": "текст", "score": 0.5}])

    chunks = await _retriever(pool).search("вопрос", limit=3)

    assert pool.calls == ["hybrid"] and chunks[0].source_id == "L-1" and chunks[0].score == 0.5


@pytest.mark.asyncio
async def test_retriever_degrades_to_text_when_embeddings_unavailable():
    """Сервис эмбеддингов молчит — поиск обязан остаться работоспособным, а не упасть."""
    _FakeAsyncClient.error = httpx.ConnectError("нет сервиса")
    pool = _FakePool([{"source": "listing", "source_id": "L-2", "title": "t", "text": "x", "score": 0.1}])

    chunks = await _retriever(pool).search("вопрос", limit=3)

    assert pool.calls == ["text"], "должна работать полнотекстовая половина гибрида"
    assert chunks[0].source_id == "L-2"


@pytest.mark.asyncio
async def test_retriever_empty_query_returns_nothing():
    pool = _FakePool([])

    assert await _retriever(pool).search("   ", limit=3) == []
    assert pool.calls == []


# --- сборка корпуса -------------------------------------------------------------------

def test_chunk_listing_builds_one_fragment():
    drafts = chunk_listing(listing_id="L-1", address="ул. Садовая, 12", price=9300000, rooms=2)

    assert len(drafts) == 1 and "9 300 000 ₽" in drafts[0].text and drafts[0].source == "listing"


def test_chunk_listing_skips_empty_address():
    assert chunk_listing(listing_id="L-9", address="", price=None, rooms=None) == []


def test_chunk_note_keeps_short_but_meaningful_knowledge():
    """«окна на юг» — знание об объекте, хотя и короче любого «разумного» порога."""
    drafts = chunk_note(note="окна на юг", address="ул. Садовая, 12", listing_id="L-3")

    assert len(drafts) == 1 and "окна на юг" in drafts[0].text and drafts[0].source_id == "L-3"


def test_chunk_note_drops_noise():
    for noise in ("тест", "ок", "-", "проверка"):
        assert chunk_note(note=noise, address="ул. Садовая, 12") == [], f"шум {noise!r} не отброшен"


def test_build_corpus_links_note_to_object_by_address():
    drafts = build_corpus(
        listings=[{"id": "L-1", "address": "ул. Садовая, 12", "price": 100, "rooms": 2}],
        notes=[{"address": "ул. Садовая, 12", "notes": "окна на юг"}],
    )

    note = next(d for d in drafts if d.source == "listing_note")
    assert note.source_id == "L-1", "заметка должна привязаться к объекту по адресу"


def test_build_corpus_keeps_note_without_matching_object():
    """Адрес не совпал — заметка не теряется, но объекта за ней нет (ограничение схемы)."""
    drafts = build_corpus(listings=[], notes=[{"address": "неизвестный адрес", "notes": "окна на юг"}])

    assert len(drafts) == 1 and drafts[0].source_id == "неизвестный адрес"


@pytest.mark.asyncio
async def test_upsert_rejects_count_mismatch():
    class _Pool:
        def acquire(self):  # pragma: no cover — до транзакции не доходит
            raise AssertionError("не должно вызываться")

    drafts = chunk_listing(listing_id="L-1", address="ул. Садовая, 12", price=1, rooms=1)
    with pytest.raises(ValueError):
        await upsert_chunks(_Pool(), drafts=drafts, vectors=[])


# --- ход диалога -----------------------------------------------------------------------

def test_turn_text_includes_knowledge_with_untrusted_marking():
    ctx = TurnContext(
        listings=[Listing(id="L-1", address="ул. Пушкина, 10", price="9 300 000 ₽", rooms="2-комн.")],
        free_slots=["2026-10-15 12:00"],
        knowledge=("ул. Пушкина, 10. окна на юг",),
    )
    text = build_turn_text(client_text="какая сторона света?", ctx=ctx)

    assert "База знаний" in text and "НЕДОВЕРЕННЫЙ" in text and "окна на юг" in text
    assert text.index("База знаний") < text.index("## Сообщение клиента")


def test_turn_text_omits_knowledge_block_when_empty():
    ctx = TurnContext(listings=[], free_slots=[])

    assert "База знаний" not in build_turn_text(client_text="привет", ctx=ctx)


def test_catalog_cap_is_untouched_by_knowledge():
    """RAG дополняет каталог, а не заменяет его: кап в 6 объектов должен сохраниться."""
    listings = [Listing(id=f"L-{i}", address=f"адрес {i}", price="1", rooms="1") for i in range(10)]
    text = build_turn_text(client_text="привет", ctx=TurnContext(listings=listings, free_slots=[], knowledge=("ф",)))

    assert "адрес 5" in text and "адрес 6" not in text, "кап каталога не должен поехать"


@pytest.mark.asyncio
async def test_dialog_knowledge_never_raises():
    """Сбой поиска — это «нет знаний», а не падение хода."""
    from realty_agent.dialog import DialogService

    async def boom(_query: str, _limit: int) -> list[str]:
        raise RuntimeError("база знаний упала")

    service = DialogService(
        ask_agent=lambda *_: None, listings=lambda: None, free_slots=lambda *_: None,
        listing_exists=lambda *_: None, slot_free=lambda *_: None, save_booking=lambda *_: None,
        notify_manager=lambda *_: None, save_state=lambda *_: None, load_state=lambda *_: None,
        search_knowledge=boom,
    )

    assert await service._knowledge_for("вопрос") == ()


@pytest.mark.asyncio
async def test_dialog_knowledge_absent_without_retriever():
    from realty_agent.dialog import DialogService

    service = DialogService(
        ask_agent=lambda *_: None, listings=lambda: None, free_slots=lambda *_: None,
        listing_exists=lambda *_: None, slot_free=lambda *_: None, save_booking=lambda *_: None,
        notify_manager=lambda *_: None, save_state=lambda *_: None, load_state=lambda *_: None,
    )

    assert await service._knowledge_for("вопрос") == ()


# --- схема векторной базы (нужен живой PostgreSQL) -------------------------------------

@pytest.mark.asyncio
async def test_vector_schema_on_live_database():
    """Расширение, размерность колонки и индекс HNSW — по каталогу, а не по тексту DDL."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        conn = await asyncpg.connect(_vector_dsn())
    except Exception:  # noqa: BLE001
        pytest.skip("векторная база не поднята — проверьте VECTOR_DB_URL и docker ps")

    try:
        extensions = await conn.fetchval("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        dimension = await conn.fetchval(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
              FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
             WHERE c.relname = 'knowledge_chunks' AND a.attname = 'embedding'
            """
        )
        hnsw = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_embedding_hnsw'"
        )
    finally:
        await conn.close()

    assert extensions == 1, "расширение vector не установлено"
    assert dimension == f"vector({EMBEDDING_DIM})", f"размерность колонки: {dimension}"
    assert hnsw and "hnsw" in hnsw.lower() and "vector_cosine_ops" in hnsw


def test_schema_check_constants_are_consistent():
    """Число проверок и подсказка не должны разъезжаться с чеками в гейте."""
    assert VECTOR_SCHEMA_CHECK_EXPECTED == 4
    assert "knowledge_chunks" in VECTOR_DDL and "hnsw" in VECTOR_DDL


def _retrieval_cases_path():
    """Файл набора кейсов: свой в репозитории, иначе общий в воркспейсе.

    Набора может не быть в публикации: он живёт вне этого репозитория, у замера. Тогда проверки
    пропускаются с названной причиной, а не падают на чтении несуществующего файла.
    """
    from paths import PROJECT, WORKSPACE

    for root in (PROJECT, WORKSPACE):
        candidate = root / "eval" / "retrieval_cases.yaml"
        if candidate.is_file():
            return candidate
    pytest.skip("набор кейсов eval/retrieval_cases.yaml живёт вне этого репозитория — замер не проводится")


def test_retrieval_cases_file_is_valid():
    """Набор кейсов читается и не пуст — замер без набора невозможен."""
    data = yaml.safe_load(_retrieval_cases_path().read_text(encoding="utf-8"))
    cases = data.get("cases", [])

    assert cases and any(c.get("expect_id") for c in cases)
    assert all("query" in c for c in cases)


def test_cases_file_is_not_silently_empty_of_negatives():
    """Кейс без ответа нужен: он показывает, что поиск не выдумывает знание."""
    cases = yaml.safe_load(_retrieval_cases_path().read_text(encoding="utf-8"))["cases"]

    assert any(not c.get("expect_id") for c in cases)


def test_json_dump_of_chunk_meta_is_readable():
    """Метаданные чанка уходят в jsonb — должны сериализоваться предсказуемо."""
    drafts = chunk_listing(listing_id="L-1", address="ул. Садовая, 12", price=1, rooms=1)

    assert json.loads(json.dumps(drafts[0].meta, ensure_ascii=False))["kind"] == "object_card"


# --- чистка статей: мусор в корпус не попадает ------------------------------------
#
# В корпус не попадает разметка (заголовки `== X ==` и строки оглавления `«1»`, `«A»`, `«/»`):
# иначе агент, честно отказываясь отвечать, получает мусор прямо в промпте. Правило — ФОРМА, не
# длина: порог по длине выбрасывает валидное короткое знание («< 40 знаков», «< 15 знаков»).

def test_chunk_article_drops_headings_and_list_markers():
    """Заголовки и маркеры оглавления фрагментами не становятся — в этом был весь мусор."""
    body = "== Примечания ==\n\n1\n\nA\n\n/\n\n== Договор ==\n\nДоговор дарения составляется письменно."

    drafts = chunk_article(title="Дарение", text=body)

    texts = [d.text for d in drafts]
    assert texts == ["Дарение. Договор. Договор дарения составляется письменно."], texts


def test_chunk_article_strips_wiki_markup():
    """Разметка снимается: агент не должен видеть `==`, `[[ ]]`, `'''`."""
    body = "== Залог ==\n\n'''Залог''' — это [[имущество|способ]] обеспечения по обязательству."

    drafts = chunk_article(title="Залог", text=body)

    assert len(drafts) == 1
    assert "==" not in drafts[0].text and "[[" not in drafts[0].text and "'''" not in drafts[0].text
    assert drafts[0].text == "Залог. Залог. Залог — это способ обеспечения по обязательству."


def test_chunk_article_keeps_heading_as_label_not_as_fragment():
    """Заголовок идёт приставкой к телу раздела: как ориентир полезен, знанием не является."""
    drafts = chunk_article(title="Ипотека", text="== Ипотечный кредит ==\n\nКредит на покупку недвижимости.")

    assert len(drafts) == 1
    assert drafts[0].text.startswith("Ипотека. Ипотечный кредит. ")
    assert drafts[0].meta["section"] == "Ипотечный кредит"


def test_chunk_article_drops_service_sections():
    """«См. также» и «Специальная литература» — навигация и библиография, а не знание."""
    body = (
        "== Основное ==\n\nЗадаток обеспечивает обязательство.\n\n"
        "== См. также ==\n\nАрра. Гражданский кодекс\n\n"
        "== Специальная литература ==\n\nИванов А. Задаток в практике. М., 2019\n"
    )

    drafts = chunk_article(title="Задаток", text=body)

    assert [d.text for d in drafts] == ["Задаток. Основное. Задаток обеспечивает обязательство."]


def test_chunk_article_drops_geographic_homonyms():
    """«Залог — деревня в Кличевском районе» — не знание о залоге недвижимости, а ловушка.

    В Википедии статьи с такими названиями описывают ещё и населённые пункты; попав в корпус,
    такой фрагмент выглядит как ответ и уводит клиента в сторону.
    """
    body = (
        "== Беларусь ==\n\nЗалог — деревня в Кличевском районе Могилёвской области.\n\n"
        "== Основное ==\n\nЗалог недвижимости обеспечивает обязательство перед кредитором."
    )

    drafts = chunk_article(title="Залог", text=body)

    assert len(drafts) == 1
    assert "деревня" not in drafts[0].text


def test_chunk_article_keeps_short_but_meaningful_prose():
    """Короткая проза остаётся: правило про форму, а не про длину.

    Порог «< 40 знаков» в объявлениях и «< 15 знаков» в заметках выбрасывает короткое, но
    осмысленное: правило — про форму, а не про длину.
    """
    drafts = chunk_article(title="Новостройка", text="== Определение ==\n\nНовостройка — новое здание.")

    assert len(drafts) == 1 and "новое здание" in drafts[0].text


def test_chunk_article_splits_long_text_by_sentences():
    """Длинный раздел режется по границам предложений — тот же предел, что у заметок."""
    long_section = " ".join(f"Предложение номер {i} о сделке с недвижимостью." for i in range(40))

    drafts = chunk_article(title="Сделка", text=f"== Раздел ==\n\n{long_section}")

    assert len(drafts) > 1
    assert all(len(d.text) <= MAX_CHUNK_CHARS + len("Сделка. Раздел. ") for d in drafts)
