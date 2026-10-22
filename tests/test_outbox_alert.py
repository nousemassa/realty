"""Алерт через очередь — без сети, на фейках.

**Почему алерт переводят первым и почему это проверяется отдельно.** Сторож зовёт эту ручку, когда
уже что-то не так. Раньше алерт уходил прямым вызовом: смерть моста в этот момент или лимит Telegram —
и сообщение **терялось**; в истории проекта есть эпизоды, когда падение моста как раз и не замечали.
Теперь строка попадает в очередь и доставляется воркером (в том числе после перезапуска), а прямой путь
остаётся **фолбэком** — он нужен именно тогда, когда БАЗА недоступна, а алерт всё равно надо доставить.

Здесь проверяются три вещи, каждая со своей причиной:
1. алерт **не уходит напрямую**, когда очередь доступна (иначе «через очередь» — это надпись);
2. при недоступной очереди он **всё равно доставляется** прямым вызовом (фолбэк не формальность);
3. счётчик означает **принятые** к доставке, а не «доставленные» — смысл поля для сторожа изменился,
   и это названо вслух, а не оставлено на догадку.
"""
from __future__ import annotations

import pytest
from fakes import fake_client, make_transport


class _RefusingDb:
    """База, которая отказывает на постановке в очередь — имитация недоступной БД."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def enqueue_outbox(self, *, kind: str, payload: dict) -> int:
        raise ConnectionError("база недоступна")


def _transport(*, db=None):
    transport = make_transport(db)
    transport._client = fake_client()
    return transport


@pytest.mark.asyncio
async def test_alert_goes_to_queue_not_straight_to_telegram():
    """Очередь доступна ⇒ алерт ставится в неё, а НЕ отправляется прямым вызовом."""
    transport = _transport()

    sent = await transport.alert_ops("упал мост")

    assert sent >= 1, "адресаты есть — принятых должно быть столько же"
    queued = transport._db.outbox_rows
    assert len(queued) == sent, "на каждого адресата — своя строка (у каждого свой чат)"
    assert all("упал мост" in row["payload"]["text"] for row in queued), "текст алерта доехал до очереди"
    assert transport._client.sent == [], "прямая отправка не должна произойти, пока очередь работает"


@pytest.mark.asyncio
async def test_alert_falls_back_to_direct_send_when_queue_is_down():
    """Очередь недоступна ⇒ алерт всё равно уходит: это и есть случай, ради которого фолбэк оставлен.

    Сторож зовёт алерт в том числе когда лежит БАЗА. «Поставить не удалось — не отправлять вовсе»
    означало бы, что самый нужный алерт не доходит.
    """
    transport = _transport()
    transport._db = _RefusingDb(transport._db)

    sent = await transport.alert_ops("база легла")

    assert sent >= 1
    assert transport._client.sent, "фолбэк обязан отправить сообщение напрямую"
    assert all("база легла" in text for _chat, text, _buttons in transport._client.sent)


@pytest.mark.asyncio
async def test_alert_count_means_accepted_not_delivered():
    """Счётчик — «принято к доставке»: у сторожа он означает «алерт не потерян», а не «в Telegram».

    Пока доставкой занимается очередь, обещать «доставлено» нельзя: строка может ждать лимита.
    Поэтому и имя поля в ответе ручки, и смысл числа названы здесь, а не додумываются вызывающим.
    """
    transport = _transport()

    sent = await transport.alert_ops("проверка")

    assert sent == len(transport._db.outbox_rows)
    assert transport._client.sent == [], "ничего не отправлено синхронно — значит и «доставлено» сказать нельзя"
