"""Шов очереди в транспорте — без сети, базы и Telegram.

**Почему отдельный файл.** Это проверки ОФЛАЙН (фейковый клиент), а тесты самой очереди идут в живую
базу и могут быть пропущены. Смешав их, мы получили бы файл, который «зелёный» и без проверки шва.

**Что здесь важно.** Очередь обязана различать два состояния: «повторить позже» (лимит Telegram) и
«разбирать вручную» (всё остальное). От этого зависит, отправится сообщение ещё раз или нет, поэтому
отображение `FloodWaitError → RetryAfter` проверяется отдельно — это контракт между транспортом и
очередью, а не деталь реализации.
"""
from __future__ import annotations

import pytest
from fakes import fake_client, make_transport
from telethon.errors import FloodWaitError

from realty_agent.outbox import RetryAfter


def _transport_with(item_raises: Exception | None = None, *, client_raises: Exception | None = None):
    transport = make_transport()
    if item_raises is not None:
        client = fake_client()
        client.send_raises = item_raises
        transport._client = client
    elif client_raises is not None:
        transport._client = None
    else:
        transport._client = fake_client()
    return transport


@pytest.mark.asyncio
async def test_flood_wait_becomes_retry_after():
    """Лимит Telegram — это «подожди и повтори», а не «не доставлено»: очередь обязана отложить строку."""
    transport = _transport_with(FloodWaitError(request=None, capture=42))

    with pytest.raises(RetryAfter) as caught:
        await transport._outbox_sender({"id": 1, "payload": {"chat_id": "7", "text": "карточка"}})

    assert caught.value.seconds == 42.0, "задержка берётся из ответа Telegram, а не выдумывается"


@pytest.mark.asyncio
async def test_flood_wait_without_seconds_uses_floor():
    """Если Telegram не назвал число, ждём нижнюю границу: повтор раньше снова упрётся в лимит."""
    transport = _transport_with(FloodWaitError(request=None, capture=0))

    with pytest.raises(RetryAfter) as caught:
        await transport._outbox_sender({"id": 1, "payload": {"chat_id": "7", "text": "карточка"}})

    assert caught.value.seconds > 0, "нулевая пауза превратила бы очередь в busy-loop"


@pytest.mark.asyncio
async def test_other_errors_propagate_for_manual_review():
    """Остальные ошибки поднимаются наружу: очередь пометит строку `failed` с причиной.

    Проглотить их (как делают прямые швы отправки, возвращающие `False`) значило бы отметить строку
    отправленной, когда она не ушла, — то есть потерять сообщение молча.
    """
    transport = _transport_with(ValueError("пользователь заблокировал бота"))

    with pytest.raises(ValueError):
        await transport._outbox_sender({"id": 1, "payload": {"chat_id": "7", "text": "карточка"}})


@pytest.mark.asyncio
async def test_delivered_item_reaches_the_client():
    """Обычный путь: строка уходит тому же адресату и с тем же текстом, что у прямых швов."""
    transport = _transport_with()

    await transport._outbox_sender({"id": 1, "payload": {"chat_id": "77", "text": "напоминание"}})

    assert transport._client.sent == [(77, "напоминание", None)]


@pytest.mark.asyncio
async def test_item_without_recipient_is_rejected_not_sent_nowhere():
    """Строка без адресата или текста — ошибка постановки: молча «отправить в никуда» нельзя."""
    transport = _transport_with()

    with pytest.raises(ValueError):
        await transport._outbox_sender({"id": 1, "payload": {"chat_id": "", "text": "текст"}})
    with pytest.raises(ValueError):
        await transport._outbox_sender({"id": 2, "payload": {"chat_id": "7", "text": ""}})


@pytest.mark.asyncio
async def test_missing_transport_is_an_error_not_a_silent_skip():
    """Без транспорта отправка невозможна — и это ошибка, а не «сделали вид»."""
    transport = _transport_with(client_raises=RuntimeError)
    transport._client = None

    with pytest.raises(RuntimeError):
        await transport._outbox_sender({"id": 1, "payload": {"chat_id": "7", "text": "карточка"}})


@pytest.mark.asyncio
async def test_queued_card_buttons_are_the_same_as_direct_ones():
    """ГЛАВНОЕ для карточек: кнопка из очереди подписана ТАК ЖЕ, как кнопка прямого пути.

    Карточка брони и напоминание о визите несут `Button.inline` — готовые объекты Telethon, которые в
    очередь не положить (они не сериализуются). Поэтому в payload едут ДЕСКРИПТОРЫ (`label`+`action`), а
    собирает их тот же построитель подписи, что и прямой путь. Здесь это проверяется двумя способами:
    побитовым совпадением `callback_data` и настоящей ПРОВЕРКОЙ подписи (`verify_callback`) — то есть
    кнопка действительно «нажимается», а не просто выглядит кнопкой.

    Без этой проверки очередь могла бы отправить карточку БЕЗ кнопок или с другой подписью — и менеджер
    не смог бы подтвердить бронь, а заметили бы это в бою.
    """
    from realty_agent.tg.callbacks import verify_callback

    transport = _transport_with()
    chat_id = "777"
    direct = transport._booking_buttons(booking_id=7, chat_id=chat_id)
    # Данные кнопки Telethon лежат в `.type.data` (см. `test_render.py:4`), а не в `.data`.
    direct_data = [button.type.data.decode() for button in direct]

    await transport._outbox_sender({
        "id": 1,
        "payload": {
            "chat_id": chat_id,
            "text": "карточка брони",
            "buttons": [{"label": "✅ Подтвердить", "action": "c"},
                        {"label": "❌ Отклонить", "action": "r"}],
            "booking_id": 7,
        },
    })

    _chat, _text, sent_buttons = transport._client.sent[0]
    queued_data = [button.type.data.decode() for button in sent_buttons]

    assert queued_data == direct_data, "подпись кнопок из очереди разошлась с прямым путём"
    assert verify_callback(transport._settings.ui_secret, queued_data[0], chat_id) == (7, "c"), \
        "кнопка из очереди обязана проходить настоящую проверку подписи"


@pytest.mark.asyncio
async def test_rows_travel_as_data_and_keep_their_buttons():
    """Строки кнопок переживают очередь — ДАННЫМИ, а не объектами `Action`.

    Объекты `Action` в payload не сериализуются (`TypeError: Object of type Action is not JSON
    serializable`): строка не встаёт в очередь, напоминание молча уходит другим путём, а наружу это
    выглядит одной строкой предупреждения. Поэтому `rows` едут ДАННЫМИ.

    Здесь проверяется то же, что и в очереди (payload сериализуется), и то, ради чего строки вообще
    едут: кнопки из очереди СОВПАДАЮТ с кнопками прямого пути и проходят настоящую проверку.
    """
    import json

    from agent_core.ui import tokens
    from agent_core.ui.reply import INLINE, Action, Reply
    from realty_agent.tg import render
    from realty_agent.ui.kinds import KIND_BACK, KIND_CANCEL

    transport = _transport_with()
    reply = Reply(
        text="Ещё актуально?",
        rows=[[Action("🕐 Показать время", KIND_BACK)], [Action("❌ Отменить", KIND_CANCEL)]],
        kind=INLINE,
    )

    accepted = await transport._enqueue_notice(
        chat_id="777", text=reply.text, rows=reply.rows, kind=reply.kind)
    assert accepted is True, "с рабочим двойником базы очередь обязана принять строку"
    payload = transport._db.outbox_rows[0]["payload"]
    json.dumps(payload)  # объект вместо данных здесь падал бы TypeError — очередь не приняла бы строку

    await transport._outbox_sender({"id": 1, "payload": payload})

    _chat, _text, sent = transport._client.sent[0]
    direct = render.to_buttons(reply, secret=transport._settings.ui_secret, chat_id="777", token="")
    # Путь через `rows` отдаёт СТРОКИ кнопок (список списков), путь через дескрипторы — плоский список:
    # форма у них разная, и сравнивать надо данные, а не форму.
    assert [button.type.data.decode() for row in sent for button in row] == \
        [button.type.data.decode() for row in direct for button in row], \
        "кнопки из очереди разошлись с прямым путём"
    assert tokens.verify(transport._settings.ui_secret, sent[0][0].type.data.decode(),
                         chat_id="777", token="") == (KIND_BACK, ""), \
        "кнопка из очереди обязана проходить настоящую проверку подписи"


@pytest.mark.asyncio
async def test_offer_reminder_is_queued_not_sent_directly():
    """Напоминание об оффере идёт ОЧЕРЕДЬЮ; шпион на отправке остаётся сторожем.

    Проверка нужна потому, что ошибку здесь нельзя увидеть по результату: отказ очереди производитель
    молча заменял прямым путём, и сообщение доходило. В тесте сторож (`sent == []`) делает подмену
    заметной, а строка очереди — доказательством, что путь действительно новый.
    """
    from agent_core.ui.reply import INLINE
    from realty_agent.ui.kinds import KIND_BACK, KIND_CANCEL

    transport = _transport_with()
    transport._client = fake_client()
    transport._db.pending_offers = [{"chat_id": "777"}]
    transport._db.conversations = {
        "777": {"step": "listing", "continuum": {
            "offered_slots": ["2026-10-15T07:00:00+03:00"], "offered_at": 100.0,
            "offered_listing": "L-1"}},
    }

    await transport._remind_pending_offers()

    assert transport._client.sent == [], "прямой отправки быть не должно — путь идёт через очередь"
    rows = transport._db.outbox_rows
    assert [(row["kind"], row["payload"]["chat_id"]) for row in rows] == [(INLINE, "777")]
    kinds = [item["kind"] for row in rows[0]["payload"]["rows"] for item in row]
    assert kinds == [KIND_BACK, KIND_CANCEL], "кнопки уехали данными, а не объектами"


@pytest.mark.asyncio
async def test_card_for_another_chat_does_not_verify():
    """Подпись привязана к чату: кнопка менеджера в чате клиента не сработает — и это не сломано.

    Свойство было и до очереди; проверяется здесь потому, что очередь пересобирает подпись заново, и
    ошибка в аргументах (`chat_id` вместо адресата) тихо сделала бы кнопки «чужими».
    """
    from realty_agent.tg.callbacks import verify_callback

    transport = _transport_with()
    await transport._outbox_sender({
        "id": 1,
        "payload": {"chat_id": "777", "text": "карточка",
                    "buttons": [{"label": "✅ Подтвердить", "action": "c"}], "booking_id": 7},
    })

    _chat, _text, sent_buttons = transport._client.sent[0]
    data = sent_buttons[0].type.data.decode()
    assert verify_callback(transport._settings.ui_secret, data, "777") == (7, "c")
    assert verify_callback(transport._settings.ui_secret, data, "888") is None, \
        "кнопка, подписанная другим чатом, обязана отвергаться"
