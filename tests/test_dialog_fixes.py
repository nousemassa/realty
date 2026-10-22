"""Правки клиентского диалога: маска телефона в промпте, согласие, овнер-флоу, роли менеджеров.

Что закреплено: телефон не уходит к модели открытым; овнер-флоу не ломает шаг диалога; меню приходит
вместе с ответом на согласие; деактивированный менеджер теряет роль, а канал/супергруппа её не
получает; пауза, `/noai` и согласие словом не зовут агента; недостижимый адресат снимает готовность
и уведомляет владельца; ник показывается с одним «@».
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fakes import (
    MANAGER,
    OWNER,
    SECRET,
    FakeCallback,
    FakeDb,
    FakeMsg,
    fake_client,
    make_settings,
    make_transport,
    queued_notices,
)

from realty_agent.db.repo import RealtyDb
from realty_agent.phones import contains_unmasked_phone, mask_phones
from realty_agent.tg import handlers
from realty_agent.tg.bot import UNREACHABLE_ERRORS
from realty_agent.tg.roles import resolve_role


class CapturingChannels:
    """Стаб канала, который записывает промпт — на нём проверяем маску телефона (NB-1)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ask(self, *, user: str, text: str, wait_s: float | None = None):
        self.prompts.append(text)
        return SimpleNamespace(text="Хорошо, записал.", done=False, usage=None, session_id="s")


def transport_for(db, *, channels=None, settings=None):
    t = make_transport(db, channels=channels, settings=settings)
    t._limiter._now = lambda: 1000.0
    return t


# ---------- телефон не уходит в LLM открытым ----------

@pytest.mark.parametrize("text", [
    "мой номер +7 912 345-67-89",
    "звоните 89123456789",
    "мой 9123456789",
    "+7(912)3456789",
    "8 912 345 67 89",
    "брат 89123456789 звонил",
])
def test_mask_phones_covers_all_forms(text):
    masked = mask_phones(text)
    assert "912" in masked, "маска сохраняет код оператора для читаемости"
    assert not contains_unmasked_phone(masked), f"остался открытый телефон: {masked}"
    assert mask_phones(masked) == masked, "маскирование идемпотентно"


def test_contains_unmasked_phone_detects_bare_ten_digits():
    assert contains_unmasked_phone("9123456789")
    assert not contains_unmasked_phone("+7 912 •••-••-89")


async def test_turn_prompt_masks_phone_from_free_text():
    """Блокер NB-1: телефон из свободного текста не должен попадать в промпт агента."""
    channels = CapturingChannels()
    t = transport_for(FakeDb(), channels=channels)
    await t._on_message(FakeMsg("мой номер +7 912 345-67-89, перезвоните"))

    assert channels.prompts, "агент должен был получить ход"
    prompt = channels.prompts[0]
    assert not contains_unmasked_phone(prompt)
    assert "9123456789" not in prompt


# ---------- owner_flow не ломает клиентский диалог ----------

class StubPool:
    def __init__(self) -> None:
        self.queries: list[tuple] = []

    async def execute(self, query, *args):
        self.queries.append((query, args))

    async def fetchval(self, query, *args):
        self.queries.append((query, args))

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return []


async def test_set_owner_flow_keeps_dialog_step_valid():
    """M1: шаг `new` не существует в `advance_state` ⇒ чат навсегда терял возможность брони."""
    pool = StubPool()
    db = RealtyDb("postgresql://stub")
    db._pool = pool
    await db.set_owner_flow(platform="tg", chat_id="1", flow="add_manager")

    sql = " ".join(query for query, _ in pool.queries)
    assert "'listing'" in sql
    assert "'new'" not in sql, "шаг `new` ломает advance_state (M1)"


# ---------- меню клиента приходит вместе с ответом на согласие ----------

async def test_consent_callback_attaches_client_menu():
    t = transport_for(FakeDb(consent=False))
    from realty_agent.tg import render

    event = FakeCallback(render.CONSENT_CALLBACK, chat_id="777")
    await t._on_callback(event)

    sent = [item for item in event.sent if item[1]]
    assert sent, "ответ после согласия должен содержать клавиатуру (M2)"
    assert len(sent[0][1]) >= 2, "меню клиента должно прийти вместе с ответом (третий ряд «👤 Я менеджер» добавит P2c)"


# ---------- legacy-менеджер не обходит `active` ----------

async def test_legacy_manager_loses_role_after_deactivation():
    settings = make_settings()
    db = FakeDb(managers={MANAGER: {"name": "petr", "active": False, "ready": True}})

    assert await resolve_role(MANAGER, settings=settings, db=db) == "client", "M4: снятый legacy — не менеджер"
    assert await resolve_role(OWNER, settings=settings, db=db) == "owner", "владелец проверяется первым"


# ---------- канал/супергруппа не становятся менеджером ----------

async def test_channel_cannot_become_manager():
    local = FakeDb()
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=555, username="some_channel", bot=False,
                                                 broadcast=True, megagroup=None))
    from agent_core.ui import tokens
    from realty_agent.ui.kinds import KIND_OWNER

    tap = FakeCallback(tokens.sign(SECRET, chat_id=OWNER, kind=KIND_OWNER, arg="add"), chat_id=OWNER)
    await t._on_callback(tap)
    await t._on_message(FakeMsg("@some_channel", chat_id=OWNER))

    assert local.managers == {}, "канал не должен получать права менеджера (M5)"


# ---------- пауза, /noai и согласие ----------

async def test_start_is_paused_and_does_not_call_agent():
    t = transport_for(FakeDb(bot_mode="paused"))
    event = FakeMsg("/start", chat_id="777")
    await t._on_message(event)

    assert t._channels.calls == 0, "/start не должен тратить LLM на паузе (M6)"
    assert handlers.PAUSED_TEXT in "\n".join(item[0] or "" for item in event.sent)


async def test_noai_blocks_agent_turn():
    local = FakeDb()
    local.ai = False
    t = transport_for(local)
    event = FakeMsg("хочу посмотреть квартиру", chat_id="777")
    await t._on_message(event)

    assert t._channels.calls == 0, "/noai: данные не должны уходить в ИИ (M7)"
    assert handlers.NOAI_TEXT in "\n".join(item[0] or "" for item in event.sent)


async def test_consent_on_pause_does_not_call_agent():
    from realty_agent.tg import render

    t = transport_for(FakeDb(consent=False, bot_mode="paused"))
    event = FakeCallback(render.CONSENT_CALLBACK, chat_id="777")
    await t._on_callback(event)

    assert t._channels.calls == 0, "на паузе LLM-ход не тратим (M8)"
    assert handlers.PAUSED_TEXT in "\n".join(item[0] or "" for item in event.sent)


# ---------- «согласен» текстом ----------

async def test_text_consent_is_recorded_when_missing():
    local = FakeDb(consent=False)
    t = transport_for(local)
    await t._on_message(FakeMsg("согласен", chat_id="777"))

    assert local.consent_saves == 1, "текстовое согласие должно фиксироваться"


async def test_text_consent_in_mid_dialog_goes_to_agent():
    local = FakeDb(consent=True)
    t = transport_for(local)
    await t._on_message(FakeMsg("согласен", chat_id="777"))

    assert local.consent_saves == 0, "при наличии согласия слово — обычная реплика"
    assert t._channels.calls == 1


# ---------- недостижимый адресат ----------

def test_peer_id_invalid_counts_as_unreachable():
    from telethon.errors import PeerIdInvalidError

    assert PeerIdInvalidError in UNREACHABLE_ERRORS, "иначе ready залипает навсегда"


async def test_unreachable_recipient_marks_not_ready_and_notifies_owner():
    from telethon.errors import UserIsBlockedError

    from realty_agent.dialog import Booking

    local = FakeDb(managers={"222": {"name": "petr", "active": True, "ready": True}})
    t = transport_for(local, settings=make_settings(telegram_manager_chat_id="222"))

    # Реакция «адресат недостижим» следует за РЕАЛЬНОЙ попыткой доставки: карточка
    # ставится в очередь, а вывод делается по её исходу — иначе снятие готовности потерялось бы
    # вместе с прямой отправкой. Здесь проверяется сама реакция; то, что воркер передаёт ей ошибку,
    # доказывает тест очереди на живой базе.
    await t._notify_manager(Booking(id="9", platform="tg", chat_id="777", listing_id="L-1",
                                    slot="2026-10-05T10:00", client_name="Аня", client_phone="+79990000000"))
    queued = [row for row in local.outbox_rows if row["payload"]["chat_id"] == "222"]
    assert queued, "карточка обязана быть принята к доставке"

    await t._outbox_result("failed", item=queued[0], error=UserIsBlockedError(request=None))

    assert local.managers["222"]["ready"] is False, "C-3: готовность снимается"
    assert any("недостижим" in text for _chat, text in queued_notices(t)), \
        "C-4: владелец должен узнать, что менеджер недостижим"


async def test_manager_texts_have_single_at():
    local = FakeDb(managers={"777": {"name": "lenavik", "active": True, "ready": True}})
    t = transport_for(local)
    t._client = fake_client(user=SimpleNamespace(id=777, username="lenavik", bot=False))
    from agent_core.ui import tokens
    from realty_agent.ui.kinds import KIND_OWNER

    await t._on_callback(FakeCallback(tokens.sign(SECRET, chat_id=OWNER, kind=KIND_OWNER, arg="add"),
                                      chat_id=OWNER))
    event = FakeMsg("lenavik", chat_id=OWNER)
    await t._on_message(event)

    text = "\n".join(item[0] or "" for item in event.sent)
    assert "@@ " not in text and "@@" not in text, f"двойная собака в тексте: {text}"
    assert handlers.MANAGERS_EXISTS.format(nick="lenavik") in text
