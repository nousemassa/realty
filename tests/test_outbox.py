"""Очередь исходящих — на ЖИВОЙ базе, как и тесты durable-хода.

**Почему не на двойнике.** Семантика очереди — это SQL: условный `UPDATE … RETURNING` (захват одной
строки), порядок по `seq`, lease, разбор залипших строк. Двойник повторил бы эти правила своими
словами, и тест проверял бы двойника. Если базы нет под рукой, тесты **честно пропускаются**
(`pytest.skip`), а не притворяются пройденными.

**Главное свойство очереди** (проверяется отдельным тестом): если процесс умер после захвата строки,
но до подтверждения отправки, то при восстановлении строка **не отправляется второй раз** — она
помечается `failed` с причиной «разбор вручную». Так же поступает `turns.sweep` для хода диалога.

**База — СВОЯ.** Воркер берёт САМУЮ СТАРУЮ строку `new`, поэтому на общей базе тест забирает **чужую**
строку боевого контура и помечает её своей. Тесты идут в `realty_test`, которую фикстура пересоздаёт
на прогон (`tests/conftest.py`), а сторож `empty_test_db` падает, если в базе обнаружатся чужие строки.
"""
from __future__ import annotations

import pytest

from realty_agent.outbox import MAX_ATTEMPTS, RetryAfter, process_once


@pytest.mark.asyncio
async def test_guard_reddens_on_a_busy_database():
    """Сторож пригодности проверки ОБЯЗАН краснеть — проверяем его на БОЕВОЙ базе (только чтение).

    Смысл: «тесты зелёные» доказывают изоляцию только тогда, когда сам сторож умеет падать. Здесь он
    смотрит в базу, где строки есть по определению (боевой контур), и обязан сказать «чужие строки».
    Без этой проверки следующая правка фикстуры могла бы снова увести тесты в боевую базу — и это
    заметили бы только по следующему инциденту.
    """
    pytest.importorskip("asyncpg")
    from live_db import assert_no_foreign_rows, foreign_rows, live_bridge_dsn

    dsn = live_bridge_dsn()
    if not dsn:
        pytest.skip("нет DB_URL в .env продукта — живая проверка сторожа не запускается")
    try:
        if not any((await foreign_rows(dsn)).values()):
            pytest.skip("боевая база пуста — сторожу нечего ловить (проверка недействительна)")
        with pytest.raises(AssertionError) as caught:
            await assert_no_foreign_rows(dsn, where="боевая база моста")
    except OSError:
        pytest.skip("боевая база недоступна — проверьте docker ps")

    assert "ЧУЖИЕ строки" in str(caught.value), "сторож обязан назвать причину, а не просто упасть"


class _Sender:
    """Шов доставки: записывает, что ему передали, и умеет сломаться по сценарию."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def __call__(self, item: dict) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append(item)


@pytest.fixture
async def repo(empty_test_db: str):
    pytest.importorskip("asyncpg")   # без драйвера тест не имеет смысла — пропускаем честно
    from agent_core.db.store import Db

    # Берём НАСТОЯЩИЙ `Db` с `apply_ddl=False`: схему накатывает фикстура (`conftest`) тем же DDL, что
    # бот в бою. База — СВОЯ: на общей тест забрал бы чужую строку `new` (воркер берёт самую старую).
    db = Db(empty_test_db, apply_ddl=False)
    try:
        await db.connect()
    except Exception:  # noqa: BLE001
        pytest.skip("база моста не поднята — проверьте docker ps и DB_URL")
    pool = db.pool

    class _Ctx:
        def __init__(self) -> None:
            self.pool = pool
            self.db = db

        async def enqueue(self, kind: str = "test", payload: dict | None = None) -> int:
            return await self.db.enqueue_outbox(kind=kind, payload=payload or {"chat_id": "1", "text": "привет"})

        async def row(self, outbox_id: int) -> dict:
            return dict(await pool.fetchrow(
                """SELECT id, kind, status, seq, attempts, last_error, next_attempt_at,
                          lease_owner, sent_at
                     FROM outbox WHERE id = $1""", outbox_id))

    ctx = _Ctx()
    created: list[int] = []

    original_enqueue = ctx.enqueue

    async def enqueue(*args, **kwargs) -> int:
        outbox_id = await original_enqueue(*args, **kwargs)
        created.append(outbox_id)
        return outbox_id

    ctx.enqueue = enqueue
    ctx.created = created
    try:
        yield ctx
    finally:
        if created:
            await pool.execute("DELETE FROM outbox WHERE id = ANY($1::bigint[])", created)
        await db.close()


@pytest.mark.asyncio
async def test_queue_is_empty_means_zero(repo):
    """Пустая очередь — это ноль обработанных строк, а не ожидание.

    Тест обязан уметь КРАСНЕТЬ: «`>= 0`» не проверяет ничего. Поэтому сначала убеждаемся, что
    забирать действительно нечего (иначе пропуск с причиной — честнее ложной зелени), и только потом
    требуем ровно ноль.
    """
    pending = await repo.pool.fetchval("SELECT count(*) FROM outbox WHERE status = 'new'")
    if pending:
        pytest.skip(f"в очереди {pending} чужих строк — тест требует пустой очереди")

    assert await process_once(repo.db, _Sender()) == 0


@pytest.mark.asyncio
async def test_delivery_marks_sent_and_records_time(repo):
    """Обычный путь: строка уходит, помечается `sent`, время отправки записано."""
    outbox_id = await repo.enqueue(payload={"chat_id": "1", "text": "карточка"})
    sender = _Sender()

    moved = await process_once(repo.db, sender)

    assert moved == 1
    row = await repo.row(outbox_id)
    assert row["status"] == "sent" and row["sent_at"] is not None
    assert sender.sent and sender.sent[0]["payload"]["text"] == "карточка"


@pytest.mark.asyncio
async def test_order_is_by_seq(repo):
    """Порядок доставки — по `seq`: очередь, а не «как получится»."""
    first = await repo.enqueue(payload={"text": "первое"})
    second = await repo.enqueue(payload={"text": "второе"})
    assert (await repo.row(first))["seq"] < (await repo.row(second))["seq"]

    sender = _Sender()
    await process_once(repo.db, sender)
    await process_once(repo.db, sender)

    assert [item["payload"]["text"] for item in sender.sent] == ["первое", "второе"]


@pytest.mark.asyncio
async def test_claimed_row_is_not_taken_by_second_worker(repo):
    """Захват эксклюзивен: пока строка в работе, второй воркер берёт СЛЕДУЮЩУЮ, а не ту же.

    Это проверка условного `UPDATE … RETURNING`, который в проекте заменяет `FOR UPDATE SKIP LOCKED`.
    """
    busy = await repo.enqueue(payload={"text": "занятая"})
    free = await repo.enqueue(payload={"text": "свободная"})
    claimed = await repo.db.claim_outbox(lease_seconds=120, owner="другой")
    assert claimed and claimed["id"] == busy, "первым берётся самый старый по seq"

    sender = _Sender()
    await process_once(repo.db, sender)

    assert [item["id"] for item in sender.sent] == [free], "берём СЛЕДУЮЩУЮ, а не занятую"
    assert (await repo.row(busy))["status"] == "in_flight", "чужая строка не тронута"
    assert (await repo.row(free))["status"] == "sent"


@pytest.mark.asyncio
async def test_crash_between_claim_and_mark_does_not_resend(repo):
    """ГЛАВНОЕ свойство: смерть после захвата и ДО отметки — строку не переигрываем.

    Захват помечает строку `in_flight` ДО сетевого вызова, поэтому «отправка могла начаться».
    Разбор (sweep) обязан не отправить её второй раз, а пометить `failed` с причиной: иначе клиент
    получил бы второй ответ — тот самый дефект, ради которого очередь и делается.
    """
    outbox_id = await repo.enqueue(payload={"text": "могло уйти"})
    claimed = await repo.db.claim_outbox(lease_seconds=120, owner="умерший")
    assert claimed and claimed["id"] == outbox_id

    # Имитация смерти: lease принудительно просрочен, отметки результата нет.
    await repo.pool.execute(
        "UPDATE outbox SET lease_until = now() - interval '1 second' WHERE id = $1", outbox_id)

    resolved = await repo.db.sweep_outbox()
    assert [item["id"] for item in resolved] == [outbox_id]

    row = await repo.row(outbox_id)
    assert row["status"] == "failed", "залипшая строка идёт на разбор, а не в очередь"
    assert "разбор вручную" in (row["last_error"] or "")
    assert row["sent_at"] is None

    # И повторной отправки НЕ происходит: строки в очереди больше нет.
    sender = _Sender()
    assert await process_once(repo.db, sender) == 0
    assert sender.sent == [], "повторная отправка залипшей строки — это и был бы дефект"


@pytest.mark.asyncio
async def test_rate_limit_requeues_instead_of_losing(repo):
    """Лимит (`RetryAfter`) — временная причина: строка возвращается в очередь и НЕ теряется."""
    outbox_id = await repo.enqueue(payload={"text": "под лимитом"})
    sender = _Sender(fail=RetryAfter(30, "Too many requests"))

    moved = await process_once(repo.db, sender)

    assert moved == 1
    row = await repo.row(outbox_id)
    assert row["status"] == "new", "строка обязана вернуться в очередь, а не пропасть"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    assert "лимит" in (row["last_error"] or "")
    # Попытка ещё не наступила — воркер её сейчас не возьмёт.
    assert await process_once(repo.db, _Sender()) == 0


@pytest.mark.asyncio
async def test_permanent_error_keeps_row_with_reason(repo):
    """Постоянная ошибка: строка остаётся в таблице ВМЕСТЕ с причиной, а не исчезает в журнале."""
    outbox_id = await repo.enqueue(payload={"text": "сломается"})
    sender = _Sender(fail=ValueError("пользователь заблокировал бота"))

    await process_once(repo.db, sender)

    row = await repo.row(outbox_id)
    assert row["status"] == "failed"
    assert "ValueError" in (row["last_error"] or "")
    assert "заблокировал" in (row["last_error"] or "")


@pytest.mark.asyncio
async def test_attempts_are_capped_not_endless(repo, monkeypatch):
    """Лимит, который не отпускает, не должен крутиться вечно: после N попыток — `failed`."""
    outbox_id = await repo.enqueue(payload={"text": "вечный лимит"})
    sender = _Sender(fail=RetryAfter(0, "Too many requests"))
    # Джиттер задержки отключаем: иначе повтор может ждать до секунды, и цикл не выберет строку
    # снова — тест проверял бы таймер, а не предельное число попыток.
    monkeypatch.setattr("realty_agent.outbox.random.uniform", lambda a, b: 0.0)

    for _ in range(MAX_ATTEMPTS):
        await process_once(repo.db, sender)

    row = await repo.row(outbox_id)
    assert row["status"] == "failed", f"после {MAX_ATTEMPTS} попыток строка идёт на разбор"
    assert "не отпустил" in (row["last_error"] or "")


@pytest.mark.asyncio
async def test_lag_grows_for_unsent_row(repo):
    """Лаг очереди — возраст самой старой неотправленной строки: по нему видно затор."""
    assert await repo.db.outbox_lag_s() >= 0.0
    outbox_id = await repo.enqueue(payload={"text": "ждёт"})
    await repo.pool.execute(
        "UPDATE outbox SET created_at = now() - interval '5 minutes' WHERE id = $1", outbox_id)

    assert await repo.db.outbox_lag_s() >= 300.0

    await repo.db.mark_outbox_sent(outbox_id)
    fresh = await repo.db.outbox_lag_s()
    assert fresh < 300.0, "отправленная строка в лаг не входит"


@pytest.mark.asyncio
async def test_max_metric_keeps_the_peak_not_the_sum(repo):
    """`outbox_lag_s` — это МАКСИМУМ за день: складывать лаг бессмысленно (в сумме он растёт всегда)."""
    # Дата берётся из базы (`now()::date`), чтобы тест не зависел от часового пояса процесса.
    # Именно `date`, а не строка: asyncpg выводит тип параметра как `date` и строку не примет.
    day = await repo.pool.fetchval("SELECT now()::date")
    metric = "test_outbox_lag_s"
    await repo.db.set_max_metric(metric=metric, value=10.0, day=day)
    await repo.db.set_max_metric(metric=metric, value=3.0, day=day)
    await repo.db.set_max_metric(metric=metric, value=25.0, day=day)

    value = await repo.pool.fetchval(
        "SELECT value FROM meta_metrics WHERE day = $1::date AND metric = $2", day, metric)
    assert float(value) == 25.0, "пик важнее последнего значения и суммы"
    await repo.pool.execute("DELETE FROM meta_metrics WHERE day = $1::date AND metric = $2", day, metric)


@pytest.mark.asyncio
async def test_outcome_is_reported_for_metrics(repo):
    """Исход каждого прохода сообщается наружу (`sent`/`failed`/`retry`) — из него делаются счётчики.

    Очередь сама метрик не знает: она умеет доставлять, а куда писать числа — дело процесса. Здесь
    проверяется именно контракт: какие слова она произносит.
    """
    outcomes: list[str] = []

    async def collect(outcome: str, **_: object) -> None:
        outcomes.append(outcome)

    await repo.enqueue(payload={"text": "уйдёт"})
    await process_once(repo.db, _Sender(), on_result=collect)
    assert outcomes == ["sent"]

    await repo.enqueue(payload={"text": "под лимитом"})
    await process_once(repo.db, _Sender(fail=RetryAfter(1, "Too many requests")), on_result=collect)
    assert outcomes == ["sent", "retry"]

    await repo.enqueue(payload={"text": "сломается"})
    await process_once(repo.db, _Sender(fail=ValueError("нельзя")), on_result=collect)
    assert outcomes == ["sent", "retry", "failed"]


@pytest.mark.asyncio
async def test_cycle_drains_queue_and_stops_on_cancel(repo):
    """Цикл (`run_forever`) разгребает очередь и останавливается по отмене — как остальные петли.

    Проверяется и то, что остановка НЕ оставляет висящую задачу: `stop()` транспорта отменяет её
    так же, и незавершаемая задача держала бы процесс при выключении.
    """
    import asyncio

    from realty_agent.outbox import run_forever

    outbox_id = await repo.enqueue(payload={"text": "через цикл"})
    sender = _Sender()
    ticks: list[int] = []

    async def on_tick() -> None:
        ticks.append(1)

    task = asyncio.create_task(run_forever(repo=repo.db, sender=sender, tick_s=0.05, on_tick=on_tick))
    # Ждём ОБА признака: строка отправлена И проход отчитался. Иначе тест отменял бы задачу в окне
    # между отметкой `sent` и вызовом `on_tick` — и падал бы не по делу (гонка, а не дефект).
    for _ in range(60):
        await asyncio.sleep(0.05)
        if ticks and (await repo.row(outbox_id))["status"] == "sent":
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await repo.row(outbox_id))["status"] == "sent"
    assert sender.sent and sender.sent[0]["payload"]["text"] == "через цикл"
    assert ticks, "проход обязан сообщать о себе (из этого пишется пик лага)"


@pytest.mark.asyncio
async def test_wake_up_delivers_without_waiting_for_idle_interval(repo):
    """Пробуждение: постановщик будит воркер, и сообщение не ждёт конца паузы простоя.

    Замер живьём показал цену без пробуждения: алерт доставлен через 29 с, потому что цикл спал
    «по простою». Здесь интервал специально длинный (`tick_s=30`): без `wake` строка ждала бы его
    целиком, и тест краснеет — это и есть проверка свойства, а не таймера.
    """
    import asyncio

    from realty_agent.outbox import run_forever

    wake = asyncio.Event()
    sender = _Sender()
    task = asyncio.create_task(run_forever(repo=repo.db, sender=sender, tick_s=30.0, wake=wake))
    await asyncio.sleep(0.2)                       # воркер ушёл в длинную паузу
    outbox_id = await repo.enqueue(payload={"text": "срочное"})
    wake.set()                                     # так делает постановщик в транспорте

    for _ in range(40):
        await asyncio.sleep(0.05)
        if (await repo.row(outbox_id))["status"] == "sent":
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await repo.row(outbox_id))["status"] == "sent", "пробуждение обязано доставить сразу"
