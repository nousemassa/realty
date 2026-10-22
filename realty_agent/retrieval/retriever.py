"""Ретривер продукта: гибридный поиск по корпусу плюс список тем.

Общий слой даёт механику поиска (вектор + полнотекст, объединение рангов); продукт добавляет то, что
относится к его корпусу: имя таблицы, язык полнотекста и список механизмов для агента.
"""
from __future__ import annotations

from agent_core.retrieval.pgvector import PgVectorRetriever
from realty_agent.retrieval.topics import TopicsCache

#: Имя таблицы корпуса и язык полнотекста — свойства корпуса продукта.
CORPUS_TABLE = "knowledge_chunks"
CORPUS_FTS_LANGUAGE = "russian"


class RealtyRetriever(PgVectorRetriever):
    """Гибридный поиск по корпусу агентства плюс список тем."""

    def __init__(self, pool, *, embed_url: str, embed_api_key: str, embed_model: str,
                 embed_timeout_s: float = 15.0) -> None:
        super().__init__(
            pool,
            table=CORPUS_TABLE,
            fts_language=CORPUS_FTS_LANGUAGE,
            embed_url=embed_url,
            embed_api_key=embed_api_key,
            embed_model=embed_model,
            embed_timeout_s=embed_timeout_s,
        )
        self._topics = TopicsCache(pool, table=CORPUS_TABLE)

    async def topics(self, *, ttl_s: float = 3600.0, limit: int = 40) -> tuple[str, ...]:
        return await self._topics.topics(ttl_s=ttl_s, limit=limit)
