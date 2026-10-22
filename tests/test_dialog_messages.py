"""История реплик диалога: порядок, окно, маска и стирание по отзыву согласия.

**Зачем эта таблица вообще.** Платформа держала историю в сессии канала; своему рантайму (`own`) нужна
своя, иначе уточнения («второй», «а дешевле?») разрешаются блоком состояния, а не предыдущей репликой —
и это класс NO-GO: не тот адрес, слот или цена.

**Почему пишет САЙТ, а не рантайм.** Проверяется здесь же: запись идёт через шов диалога, поэтому история
одна и та же при `AGENT_RUNTIME=platform` и `own` — переключение рантайма не рвёт контекст.

**Главная защита — ПДн.** В историю попадает ТОЛЬКО маскированный текст: она уезжает в следующий запрос к
модели, то есть открытый телефон утёк бы в третий сервис. Стирание — вместе с отзывом согласия, в той же
транзакции (иначе `/stop` оставлял бы переписку жить).
"""

from __future__ import annotations

import pytest
from test_dialog import make_service

from realty_agent.phones import contains_unmasked_phone, mask_phones


async def test_turn_writes_user_then_assistant_with_masked_text():
    recorded: list[dict] = []

    async def recorder(**kwargs):
        recorded.append(kwargs)

    svc, _saved = make_service(agent_text="Здравствуйте! Подберу объект.", record_message=recorder)
    await svc.handle_message(platform="tg", chat_id="1", text="мой телефон +7 912 345-67-89")

    assert [item["role"] for item in recorded] == ["user", "assistant"]
    #: Клиентская реплика уходит в историю МАСКИРОВАННОЙ — это вход следующего запроса к модели.
    assert contains_unmasked_phone(recorded[0]["text"]) is False
    assert recorded[0]["text"] == mask_phones("мой телефон +7 912 345-67-89")
    #: В историю идёт РЕПЛИКА, а не собранный `turn_text` с блоком состояния: иначе в истории оседала бы
    #: устаревшая копия состояния, и модель приняла бы её за текущую.
    assert "АКТИВНЫЕ ОБЪЕКТЫ" not in recorded[0]["text"]
    assert recorded[1]["role"] == "assistant"


async def test_assistant_phone_is_masked_too():
    recorded: list[dict] = []

    async def recorder(**kwargs):
        recorded.append(kwargs)

    svc, _saved = make_service(agent_text="Записал, перезвоню с +7 912 345-67-89", record_message=recorder)
    await svc.handle_message(platform="tg", chat_id="1", text="запишите меня")

    assistant = [item for item in recorded if item["role"] == "assistant"]
    assert assistant, "ответ агента обязан попасть в историю"
    assert contains_unmasked_phone(assistant[0]["text"]) is False


async def test_long_reply_is_truncated_to_the_cap():
    """Простыня в истории — это деньги в КАЖДОМ следующем запросе, поэтому кап жёсткий."""
    recorded: list[dict] = []

    async def recorder(**kwargs):
        recorded.append(kwargs)

    svc, _saved = make_service(agent_text="а" * 2000, record_message=recorder)
    await svc.handle_message(platform="tg", chat_id="1", text="расскажи подробнее")

    assistant = next(item for item in recorded if item["role"] == "assistant")
    assert len(assistant["text"]) <= 601, "кап 600 символов + маркер усечения"


async def test_history_is_not_unconditional():
    """Шва нет — ход идёт как раньше: отсутствие истории не роняет ответ клиенту."""
    svc, _saved = make_service(agent_text="ок")
    reply = await svc.handle_message(platform="tg", chat_id="1", text="привет")

    assert reply.text


async def test_service_blocks_never_reach_the_history():
    """В историю идёт то, что УВИДЕЛ КЛИЕНТ: служебные блоки вырезаны.

    Сырой ответ модели с служебным блоком (`COVERAGE|{"answered": false}`) в истории означает, что в
    следующем промпте модель получит его как «то, что она говорила» — то есть будет учиться повторять
    служебный блок.
    """
    recorded: list[dict] = []

    async def recorder(**kwargs):
        recorded.append(kwargs)

    svc, _saved = make_service(
        agent_text='Вот подходящие объекты.\nCOVERAGE|{"answered": false}\nFILTER|{"rooms": 2}',
        record_message=recorder)
    reply = await svc.handle_message(platform="tg", chat_id="1", text="какие есть?")

    assistant = next(item for item in recorded if item["role"] == "assistant")
    assert "COVERAGE|" not in assistant["text"]
    assert "FILTER|" not in assistant["text"]
    #: И это РОВНО то, что ушло клиенту: `FILTER|` заменяет ответ агента каталогом, и в истории обязан
    #: остаться каталог, а не проза модели, которую клиент не видел.
    assert assistant["text"] == reply.text


async def test_locally_closed_turn_also_joins_the_history():
    """Ход БЕЗ агента тоже оставляет реплики: история принадлежит разговору, а не способу исполнения.

    Вопрос клиента и ответ каталогом обязаны попадать в окно: иначе следующий ход своего рантайма
    их не видит.
    """
    recorded: list[dict] = []

    async def recorder(**kwargs):
        recorded.append(kwargs)

    async def gate(text: str) -> bool:
        return True  # «это только условие» — лестница закрывает ход сама

    svc, _saved = make_service(agent_text="агента тут не будет", record_message=recorder,
                               local_gate=gate, ladder_enabled=True)
    reply = await svc.handle_message(platform="tg", chat_id="1", text="хочу с парком +7 912 345-67-89")

    assert [item["role"] for item in recorded] == ["user", "assistant"]
    #: Ветка локального закрытия получает текст ДО маски — значит маска обязана стоять на записи.
    assert contains_unmasked_phone(recorded[0]["text"]) is False
    assert contains_unmasked_phone(recorded[1]["text"]) is False
    assert reply.text, "ответ клиенту всё равно уходит"


# --- хранилище: порядок, окно и стирание (реальная база, как прочие SQL-тесты) ----------------------


@pytest.mark.asyncio
async def test_window_keeps_chronological_order_and_limit(bridge_test_dsn: str):
    pytest.importorskip("asyncpg")
    from realty_agent.db.repo import RealtyDb

    db = RealtyDb(bridge_test_dsn, apply_ddl=True, tz="Europe/Moscow")
    await db.connect()
    try:
        for turn in range(1, 6):
            await db.append_dialog_message(platform="tg", chat_id="777", turn_key=f"k{turn}",
                                           role="user", text=f"вопрос {turn}", step="listing")
            await db.append_dialog_message(platform="tg", chat_id="777", turn_key=f"k{turn}",
                                           role="assistant", text=f"ответ {turn}", step="listing")
        window = await db.load_dialog_history(platform="tg", chat_id="777", limit=8)

        #: Восемь последних — это ходы 2..5, и порядок ПРЯМОЙ: модель читает разговор, а не реверс.
        assert [row["content"] for row in window] == [
            "вопрос 2", "ответ 2", "вопрос 3", "ответ 3",
            "вопрос 4", "ответ 4", "вопрос 5", "ответ 5",
        ]
        assert window[0]["role"] == "user" and window[-1]["role"] == "assistant"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prune_keeps_the_tail_and_does_not_touch_neighbours(bridge_test_dsn: str):
    """Чистка оставляет `keep` последних и НЕ задевает другого клиента (иначе «чистка» стирает чужие)."""
    pytest.importorskip("asyncpg")
    from realty_agent.db.repo import RealtyDb

    db = RealtyDb(bridge_test_dsn, apply_ddl=True, tz="Europe/Moscow")
    await db.connect()
    try:
        for turn in range(1, 6):
            await db.append_dialog_message(platform="tg", chat_id="999", turn_key=f"k{turn}",
                                           role="user", text=f"вопрос {turn}")
        await db.append_dialog_message(platform="tg", chat_id="111", turn_key="other",
                                       role="user", text="чужой клиент")

        deleted = await db.prune_dialog_messages(platform="tg", chat_id="999", keep=2)

        assert deleted == 3
        assert [row["content"] for row in await db.load_dialog_history(platform="tg", chat_id="999", limit=8)] \
            == ["вопрос 4", "вопрос 5"]
        assert await db.load_dialog_history(platform="tg", chat_id="111", limit=8) == [
            {"role": "user", "content": "чужой клиент"}
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_revoke_consent_erases_the_conversation(bridge_test_dsn: str):
    """`/stop` обязан стирать переписку — иначе текст клиента переживает отзыв согласия."""
    pytest.importorskip("asyncpg")
    from realty_agent.db.repo import RealtyDb

    db = RealtyDb(bridge_test_dsn, apply_ddl=True, tz="Europe/Moscow")
    await db.connect()
    try:
        await db.append_dialog_message(platform="tg", chat_id="888", turn_key="k1",
                                       role="user", text="здравствуйте", step="listing")
        await db.append_dialog_message(platform="tg", chat_id="888", turn_key="k1",
                                       role="assistant", text="здравствуйте!", step="listing")
        await db.revoke_consent(platform="tg", chat_id="888")

        assert await db.load_dialog_history(platform="tg", chat_id="888", limit=8) == []
    finally:
        await db.close()
