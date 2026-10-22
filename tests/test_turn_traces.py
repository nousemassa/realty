"""Трассы ходов: одна строка на клиентский ход, токены и стоимость — из ответа платформы.

Без сети: диалог поднимается двойниками, а разбор записи `usage` и агрегаты проверяются отдельно.
Главное, что здесь проверяется, — не «строка появилась», а **когда её быть НЕ должно** и **что в ней
лежит**: до вызова агента мост не пишет ничего (инвариант), а у локально закрытого хода
`agent_called=false` — иначе «$/диалог» считался бы только по ходам с облаком.
"""
from __future__ import annotations

from datetime import date

import pytest
from test_dialog import RecordingGate, RecordingTurns, make_service

from agent_core.db.store import Db, usage_fields
from realty_agent.channels.turn_context import Listing


class RecordingTrace:
    """Двойник писателя трасс: собирает переданные записи."""

    def __init__(self, *, broken: bool = False) -> None:
        self.rows: list[dict] = []
        self.broken = broken

    async def __call__(self, trace: dict) -> None:
        if self.broken:
            raise RuntimeError("писатель трасс упал (тест)")
        self.rows.append(trace)


def listings():
    return [
        Listing(id="L-1", address="ул. Пушкина, 10 (2-комн., 54 м²)", rooms="2", price="8500000"),
        Listing(id="L-3", address="ул. Садовая, 27 (3-комн., 76 м²)", rooms="3", price="12500000"),
    ]


@pytest.mark.asyncio
async def test_trace_row_per_agent_turn_carries_platform_numbers():
    """Успешный ход: строка есть, и в ней токены/цена, которые сообщила платформа."""
    trace = RecordingTrace()
    svc, _ = make_service(agent_text="Сейчас в продаже: ул. Пушкина, 10", record_trace=trace)
    svc._ask = asking("Сейчас в продаже: ул. Пушкина, 10",
                      usage={"cost": 0.085, "input": 4580, "output": 2, "total": 9062})

    await svc.handle_message(platform="tg", chat_id="1", text="какие есть квартиры?")

    assert len(trace.rows) == 1
    row = trace.rows[0]
    assert row["agent_called"] is True
    assert row["outcome"] == "ok"
    assert row["usage"]["cost"] == 0.085
    assert row["session_id"] == "sess-1"
    assert row["prompt_hash"], "хеш промпта — это версия промпта в отчёте"
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_trace_marks_bad_lexeme_verdict():
    """Лексема блока не сошлась — в трассе это отдельный исход, а не «блока нет»."""
    trace = RecordingTrace()
    svc, _ = make_service(agent_text="текст", record_trace=trace)
    # «zzzzzz» не может совпасть с настоящей лексемой: она шестнадцатеричная (`token_hex`).
    svc._ask = asking("BOOKING|L-1|2026-10-01T11:00|Иван|+7 912 345-67-89@chzzzzzz")

    await svc.handle_message(platform="tg", chat_id="1", text="хочу на просмотр")

    assert trace.rows[0]["parser_verdict"] == "bad_challenge_lexeme"


@pytest.mark.asyncio
async def test_trace_marks_missing_block():
    """Служебного блока нет — тоже отдельный исход разбора."""
    trace = RecordingTrace()
    svc, _ = make_service(agent_text="Позову менеджера.", record_trace=trace)

    await svc.handle_message(platform="tg", chat_id="1", text="хочу на просмотр")

    assert trace.rows[0]["parser_verdict"] == "no_block_in_last_line"


@pytest.mark.asyncio
async def test_no_trace_when_the_agent_is_not_called():
    """Инвариант: до согласия мост агента не зовёт — строки трассы быть не должно."""
    trace = RecordingTrace()
    turns = RecordingTurns()
    svc, _ = make_service(agent_text="Ответ агента.", listings=listings(), record_trace=trace,
                          turns=turns, local_gate=RecordingGate(), ladder_enabled=True)
    # Кнопочный путь: телефон приходит кнопкой, агент не вызывается вовсе.
    async def load_state(platform: str, chat_id: str):
        # Шаг «имя», объект выбран ранее: клиент присылает имя, и мост сам отвечает кнопкой
        # запроса телефона — агент на этом пути не вызывается вовсе.
        return _state(step="name", offered_slots=["2026-10-01 11:00"], listing_id="L-1")

    svc._load_state = load_state
    await svc.handle_message(platform="tg", chat_id="1", text="Иван")

    assert trace.rows == [], "нет вызова агента — нет строки трассы"


@pytest.mark.asyncio
async def test_local_close_writes_a_row_without_an_agent():
    """Локально закрытый ход: строка есть, `agent_called=false`, стоимость пустая."""
    trace = RecordingTrace()
    svc, _ = make_service(agent_text="Ответ агента.", listings=listings(), record_trace=trace,
                          local_gate=RecordingGate(), ladder_enabled=True)

    reply = await svc.handle_message(platform="tg", chat_id="1", text="двушки дешевле 9 млн")

    assert "🔎 Фильтр" in reply.text
    assert len(trace.rows) == 1
    row = trace.rows[0]
    assert row["agent_called"] is False
    assert row["outcome"] == "local_close"
    assert row["usage"] is None and row["latency_ms"] is None
    assert row["prompt_hash"] == "", "промпта не было — и хеша быть не должно"


@pytest.mark.asyncio
async def test_broken_trace_writer_does_not_break_the_turn():
    """Наблюдение не роняет ход: падение писателя — только предупреждение в журнал."""
    svc, _ = make_service(agent_text="Сейчас в продаже: ул. Пушкина, 10",
                          record_trace=RecordingTrace(broken=True))

    reply = await svc.handle_message(platform="tg", chat_id="1", text="какие есть квартиры?")

    assert reply is not None and "ул. Пушкина" in reply.text


def test_usage_fields_reads_platform_record():
    """Разбор записи платформы: числа приходят и числами, и строками; непонятное — пусто, не ноль."""
    tokens_in, tokens_out, tokens_total, cost = usage_fields(
        {"cost": 0.085, "input": "4580", "output": 2, "total": 9062, "type": "llm"})

    assert (tokens_in, tokens_out, tokens_total) == (4580, 2, 9062)
    assert cost == pytest.approx(0.085)
    assert usage_fields(None) == (None, None, None, None)
    assert usage_fields({"cost": "мусор"})[3] is None


@pytest.mark.asyncio
async def test_aggregation_counts_money_latency_and_local_closes(empty_test_db: str):
    """Агрегаты на живой схеме: «$/диалог» считается по ВСЕМ ходам, латентность — только с агентом."""
    import asyncpg

    dsn = empty_test_db   # СВОЯ база, а не боевая: прогон тестов не должен видеть боевые строки
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    except Exception:  # noqa: BLE001
        pytest.skip("база моста не поднята — проверьте docker ps и DB_URL")
    db = Db(dsn, apply_ddl=False)
    db._pool = pool  # отчёт только читает; своего соединения не открываем
    day = date(2001, 1, 1)  # свой день, чтобы не смешиваться с боевыми строками
    try:
        await pool.execute("DELETE FROM turn_traces WHERE day = $1", day)
        for cost, latency, called in ((0.10, 1000, True), (0.10, 3000, True), (None, None, False)):
            await db.save_turn_trace(day=day, platform="tg", chat_hash="t", step="listing",
                                     prompt_hash="x", context_items=0, agent_called=called,
                                     outcome="ok" if called else "local_close",
                                     parser_verdict="ok" if called else "not_applicable",
                                     usage={"cost": cost, "input": 10, "output": 1, "total": 11}
                                     if cost else None, latency_ms=latency)
        rows = await pool.fetch("""SELECT count(*) AS n, coalesce(sum(cost_usd), 0) AS cost,
                                          percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                                            FILTER (WHERE agent_called) AS p50
                                     FROM turn_traces WHERE day = $1""", day)
        assert rows[0]["n"] == 3
        assert float(rows[0]["cost"]) == pytest.approx(0.20)
        assert float(rows[0]["p50"]) == pytest.approx(2000.0)
    finally:
        await pool.execute("DELETE FROM turn_traces WHERE day = $1", day)
        await pool.close()


def asking(text: str, *, usage: dict | None = None, session_id: str = "sess-1"):
    """Асинхронный двойник агента: `async def` обязателен — обычная функция вернула бы значение,
    а не корутину, и `await` упал бы с `TypeError` (на этом уже попадались)."""
    from realty_agent.dialog import AgentReply

    async def ask(user: str, prompt: str):
        return AgentReply(text=text, done=True, usage=usage, session_id=session_id)

    return ask


def _state(**kwargs):
    from realty_agent.dialog import ChatState

    return ChatState(platform="tg", chat_id="1", **kwargs)
