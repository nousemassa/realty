"""Тесты durable-состояния хода.

**Почему на живой базе, а не на двойнике.** Реализация `TurnStore` — это SQL: `SKIP LOCKED`,
lease-условие, `ON CONFLICT`, частичный уникальный индекс. Двойник повторил бы эти правила
своими словами, и тест проверял бы двойника, а не поведение. Поэтому здесь настоящий
PostgreSQL, а если его нет под рукой — тесты честно пропускаются, а не притворяются
пройденными.

Что проверяется:

  * идемпотентность регистрации хода (один ход — одна запись);
  * lease: второй воркер НЕ берёт ход, пока lease жив;
  * переходы `created → sent → awaiting → answered | failed` и терминальность;
  * **главное свойство**: залипший ход в `sent`/`awaiting` НЕ переигрывается, а уходит в
    `failed`; в `created` — освобождается безопасно.
"""
from __future__ import annotations

import pytest

from realty_agent.turns import (
    STATUS_ANSWERED,
    STATUS_AWAITING,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_SENT,
    TurnStore,
    new_turn_key,
)


@pytest.fixture
async def store(empty_test_db: str):
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(empty_test_db, min_size=1, max_size=2)
    except Exception:  # noqa: BLE001
        pytest.skip("база моста не поднята — проверьте docker ps и DB_URL")
    created: list[str] = []

    def make(owner: str = "test") -> TurnStore:
        return TurnStore(pool, owner=owner, lease_seconds=60)

    class _Ctx:
        def __init__(self) -> None:
            self.pool = pool
            self.make = make
            self.created = created

    ctx = _Ctx()
    try:
        yield ctx
    finally:
        if created:
            await pool.execute("DELETE FROM turn_state WHERE turn_key = ANY($1::text[])", created)
        await pool.close()


async def _begin(store_ctx, *, chat: str = "1") -> tuple[TurnStore, str]:
    store = store_ctx.make()
    key = new_turn_key()
    store_ctx.created.append(key)
    await store.begin(platform="tg", chat_id=chat, turn_key=key)
    return store, key


def test_turn_key_is_unique():
    assert new_turn_key() != new_turn_key()


@pytest.mark.asyncio
async def test_begin_is_idempotent(store):
    s1, key = await _begin(store)
    first = await s1.begin(platform="tg", chat_id="1", turn_key=key)
    second = await s1.begin(platform="tg", chat_id="1", turn_key=key)

    assert first.id == second.id, "повторная регистрация не должна создавать второй ход"
    assert second.status == STATUS_CREATED


@pytest.mark.asyncio
async def test_claim_holds_lease_against_second_worker(store):
    s1, key = await _begin(store)
    other = store.make(owner="other")

    first = await s1.claim(platform="tg", chat_id="1", turn_key=key)
    second = await other.claim(platform="tg", chat_id="1", turn_key=key)

    assert first is not None and first.attempts == 1
    assert second is None, "второй воркер не должен взять ход с живым lease"


@pytest.mark.asyncio
async def test_full_transition_chain(store):
    s, key = await _begin(store)
    turn = await s.claim(platform="tg", chat_id="1", turn_key=key)

    await s.mark_sent(turn.id)
    assert (await s.get(turn.id)).status == STATUS_SENT
    await s.mark_awaiting(turn.id)
    assert (await s.get(turn.id)).status == STATUS_AWAITING
    await s.mark_answered(turn.id, reply_text="ответ клиенту")

    final = await s.get(turn.id)
    assert final.status == STATUS_ANSWERED and final.is_terminal
    assert final.reply_text == "ответ клиенту"


@pytest.mark.asyncio
async def test_terminal_turn_is_not_claimed_again(store):
    s, key = await _begin(store)
    turn = await s.claim(platform="tg", chat_id="1", turn_key=key)
    await s.mark_answered(turn.id, reply_text="ответ")

    assert await s.claim(platform="tg", chat_id="1", turn_key=key) is None


@pytest.mark.asyncio
async def test_stuck_in_sent_is_failed_not_replayed(store):
    """Главное свойство пункта: ответ мог уйти клиенту — повтор дал бы ВТОРОЙ ответ."""
    s, key = await _begin(store)
    turn = await s.claim(platform="tg", chat_id="1", turn_key=key)
    await s.mark_sent(turn.id)
    # Подделываем «просроченный lease»: процесс, взявший ход, умер.
    await store.pool.execute(
        "UPDATE turn_state SET lease_until = now() - interval '1 hour' WHERE id = $1", turn.id
    )

    assert [state.id for state in await s.stuck()] == [turn.id]
    resolved = await s.sweep()

    assert resolved[0].status == STATUS_FAILED
    assert "разбор вручную" in (resolved[0].error or "")
    assert await s.claim(platform="tg", chat_id="1", turn_key=key) is None, "переигровки быть не должно"


@pytest.mark.asyncio
async def test_stuck_in_created_is_released_without_replay(store):
    """Отправка не начиналась — терять нечего, ход просто отпускается."""
    s, key = await _begin(store)
    turn = await s.claim(platform="tg", chat_id="1", turn_key=key)
    await store.pool.execute(
        "UPDATE turn_state SET lease_until = now() - interval '1 hour' WHERE id = $1", turn.id
    )

    resolved = await s.sweep()

    assert resolved[0].status == STATUS_CREATED, "ход без отправки не должен становиться failed"
    again = await s.claim(platform="tg", chat_id="1", turn_key=key)
    assert again is not None, "освобождённый ход можно взять снова"


@pytest.mark.asyncio
async def test_reconcile_at_startup_returns_resolved(store):
    s, key = await _begin(store)
    turn = await s.claim(platform="tg", chat_id="1", turn_key=key)
    await s.mark_sent(turn.id)
    await store.pool.execute(
        "UPDATE turn_state SET lease_until = now() - interval '1 hour' WHERE id = $1", turn.id
    )

    resolved = await s.reconcile()

    assert any(state.id == turn.id and state.status == STATUS_FAILED for state in resolved)


@pytest.mark.asyncio
async def test_lease_owner_is_recorded(store):
    s, key = await _begin(store)
    turn = await s.claim(platform="tg", chat_id="1", turn_key=key)

    assert turn.lease_owner == "test", "видно, кто держит ход"


def test_outbox_worker_is_real_and_carries_no_process_labels():
    """Очередь исходящих умеет работать — и не несёт в публичном коде меток процесса.

    Проверка статическая (по исходнику), потому что тесты очереди ходят в живую базу и здесь могут быть
    пропущены. Заглушка сторожится с обратной стороны: модуль обязан ДАТЬ работу, а не обещать её.
    Второе свойство — публичность: служебные метки разделов и шагов процесса в
    поставляемом модуле не нужны.
    """
    from paths import PROJECT

    source = PROJECT / "realty_agent" / "outbox.py"
    text = source.read_text(encoding="utf-8")

    assert "NotImplementedError" not in text, "отправка обязана работать, а не обещать"
    assert "заглушка" not in text.lower(), "модуль не заглушка — и не должен себя так называть"
    assert "§" not in text, "служебные метки процесса в публичном коде не нужны"
    for label in ("этап ", "раунд", "окно "):
        assert label not in text, f"метка процесса «{label}» в публичном коде не нужна"
