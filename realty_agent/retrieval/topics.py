"""Список механизмов, по которым в корпусе есть материал.

Агент, не найдя ответа во фрагментах, предлагает клиенту конкретные механизмы (задаток, обременение,
дарение), а не отвечает общим «в базе нет». Список берётся из БАЗЫ — он обязан совпадать с тем, что
реально залито; файл манифеста мог обновиться, а наливка не пройти. Служебные источники (карточки
объектов, заметки менеджеров) в темы не идут: это данные агентства, а не механизмы сделки.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

#: Источник, из которого берутся темы: правовые статьи корпуса.
SOURCE_WIKI = "wikipedia"


class TopicsCache:
    """Кэш списка тем: он нужен на каждом ходу, а меняется только при наливке корпуса."""

    def __init__(self, pool, *, table: str = "knowledge_chunks", source: str = SOURCE_WIKI) -> None:
        self._pool = pool
        self._table = table
        self._source = source
        self._cache: tuple[float, tuple[str, ...]] = (0.0, ())

    async def topics(self, *, ttl_s: float = 3600.0, limit: int = 40) -> tuple[str, ...]:
        now = time.monotonic()
        cached_at, cached = self._cache
        if cached and now - cached_at < ttl_s:
            return cached
        try:
            rows = await self._pool.fetch(
                f"""SELECT DISTINCT source_id FROM {self._table}
                     WHERE source = $1 ORDER BY source_id LIMIT $2""",
                self._source, limit)
        except Exception as error:  # noqa: BLE001 — без списка тем ход обязан состояться
            log.warning("retrieval: список тем недоступен (%s) — предложу без него",
                        type(error).__name__)
            return ()
        topics = tuple(str(row["source_id"]) for row in rows if row["source_id"])
        self._cache = (now, topics)
        return topics
