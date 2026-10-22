"""Офлайн-тесты обработчика callback-кнопок (`bot._on_callback`).

Зачем именно они: «тост менеджеру глушится авто-ответом edit()» и «согласие отвечало бы без
answer()» не видны, пока ни один тест не заходит в транспорт. Здесь это закрыто: порядок
вызовов (`answer` → `edit`) проверяется жёстко.

Фейки, а не сеть: `TelegramTransport` в конструкторе не подключается к Telegram (сеть — только в `start()`).
"""
from __future__ import annotations

from realty_agent.settings import Settings
from realty_agent.tg import handlers, render
from realty_agent.tg.bot import TelegramTransport
from realty_agent.tg.callbacks import sign_callback
from realty_agent.tg.handlers import PRIVACY_TEXT

SECRET = "s" * 40
MANAGER = "100000001"


class FakeEvent:
    """Callback-событие: записывает порядок и содержимое ответов."""

    def __init__(self, data: str, chat_id: str = MANAGER) -> None:
        self.data = (data or "").encode()
        self.chat_id = int(chat_id)
        self.is_private = True
        self.log: list[str] = []
        self.answers: list[str | None] = []
        self.sent: str | None = None
        self.sent_all: list[str] = []       # все ответы по порядку (транспорт шлёт несколько сообщений)

    async def answer(self, text=None, **kwargs) -> None:
        self.log.append("answer")
        self.answers.append(text)

    async def edit(self, **kwargs) -> None:
        self.log.append("edit")

    async def respond(self, *args, **kwargs) -> None:
        self.log.append("respond")
        self.sent = args[0] if args else kwargs.get("text")
        self.sent_all.append(self.sent)


class FakeDb:
    """Минимум для путей callback: бронь, согласие и настройки/каталог для хода."""

    def __init__(self, booking=None) -> None:
        self.booking = booking
        self.consent_saved = False

    async def set_booking_status(self, **kwargs):
        return self.booking

    async def save_consent(self, **kwargs) -> bool:
        self.consent_saved = True
        return True

    async def has_consent(self, **kwargs) -> bool:
        return True

    async def setting(self, key: str, default=None):
        return default

    async def active_listings(self, limit: int = 6) -> list:
        # Адрес объекта для сообщения о решении (без него показывается резервный id)
        return [{"id": "L-1", "address": "ул. Пушкина, 10, кв. 42 (2-комн., 54 м², 7/9 эт.)",
                 "price": "8900000", "rooms": "2"}]

    async def listing_exists(self, listing_id: str) -> bool:
        return False

    async def busy_slots(self, candidates: list[str]) -> set[str]:
        return set()

    async def slot_taken(self, **kwargs) -> bool:
        return False

    async def load_conversation(self, **kwargs):
        return None

    async def count_active_listings(self) -> int:
        return len(self.listings)

    async def ai_allowed(self, *, platform: str = "tg", chat_id: str) -> bool:
        return True

    async def manager_row_exists(self, chat_id: str) -> bool:
        return True      # legacy-чат есть в таблице — роль менеджера сохраняется (M4)

    async def is_active_manager(self, chat_id: str) -> bool:
        return True

    async def save_conversation(self, **kwargs) -> None:
        return None


def transport(*, db=None, secret: str = SECRET) -> TelegramTransport:
    settings = Settings(
        ui_secret=secret,
        telegram_manager_chat_id=MANAGER,
        bridge_api_key="k" * 32,
        _env_file=None,
    )
    return TelegramTransport(settings=settings, db=db or FakeDb(), channels=None)


async def test_toast_is_answered_before_buttons_are_dropped():
    """Порядок критичен: edit() планирует свой auto-answer и глушит наш тост."""
    booking = {"id": 42, "chat_id": None, "platform": "web", "slot": "2026-10-02T10:00", "listing_id": "L-1"}
    t = transport(db=FakeDb(booking=booking))
    event = FakeEvent(sign_callback(SECRET, 42, "c", MANAGER))

    await t._on_callback(event)

    assert event.log[:2] == ["answer", "edit"], "тост обязан уйти ДО снятия кнопок (сообщение о решении — после, DD-6)"
    assert event.answers[0] == "Заявка #42 подтверждена", "номер заявки виден в тосте"


async def test_already_processed_booking_alerts_without_editing():
    t = transport(db=FakeDb(booking=None))
    event = FakeEvent(sign_callback(SECRET, 42, "r", MANAGER))

    await t._on_callback(event)

    assert event.log == ["answer"], "повторный тап: только alert, без правки сообщения"
    assert event.answers[0] == "Заявка уже обработана"


async def test_signed_callback_is_ignored_without_secret():
    t = transport(secret="")
    event = FakeEvent(sign_callback(SECRET, 1, "c", MANAGER))

    await t._on_callback(event)

    assert event.log == ["answer"], "без UI-секрета подписанные данные не принимаются"


async def test_policy_button_works_even_without_secret():
    """Кнопка политики безподписная и не трогает состояние — работает и в деградированном режиме."""
    t = transport(secret="")
    event = FakeEvent(render.POLICY_CALLBACK)

    await t._on_callback(event)

    assert event.log[:2] == ["answer", "respond"]
    assert event.sent == PRIVACY_TEXT


async def test_consent_tap_answers_saves_and_sends_client_entry():
    """После согласия агент НЕ вызывается: клиент сразу получает приветствие, инструкцию и каталог."""
    db = FakeDb()
    t = transport(db=db)
    event = FakeEvent(render.CONSENT_CALLBACK, chat_id="777")

    await t._on_callback(event)

    assert db.consent_saved, "согласие записано"
    assert event.log[0] == "answer", "спиннер гасим сразу"
    assert event.log[1] == "edit", "кнопка согласия снимается"
    assert t._channels is None or t._channels.calls == 0, "согласие больше не тратит LLM-ход (DC-1/DC-2)"
    assert len(event.sent_all) == 3, f"три сообщения входа клиента, а не {len(event.sent_all)}"
    assert handlers.CLIENT_GREETING in event.sent_all[0], "первое сообщение — приветствие"
    assert handlers.CLIENT_HOWTO in event.sent_all[1], "второе — короткая инструкция"
    assert handlers.OBJECTS_TITLE in event.sent_all[2], "третье — каталог объектов"


# ---------- номер заявки явно + объект адресом ----------

async def test_decision_message_has_number_and_address():
    """Менеджер видит «Заявка #N подтверждена» (не «#{id}») и АДРЕС объекта, а не «L-1»."""
    booking = {"id": 42, "chat_id": None, "platform": "web", "slot": "2026-10-02T10:00",
               "listing_id": "L-1", "status": "pending"}
    t = transport(db=FakeDb(booking=booking))
    event = FakeEvent(sign_callback(SECRET, 42, "c", MANAGER))

    await t._on_callback(event)

    body = event.sent_all[-1] or ""
    assert "Заявка #42 подтверждена" in body, body
    assert "{id}" not in body, "плейсхолдер не должен попадать в текст"
    assert "L-1" not in body, "объект показываем адресом, а не внутренним id"
    assert "ул. Пушкина" in body, body

