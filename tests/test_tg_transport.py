"""Офлайн-тесты Telegram-транспорта: лимитер, HMAC-кнопки, статусы, тексты, разбор команд."""
from __future__ import annotations

import pytest

from realty_agent.tg.callbacks import sign_callback, verify_callback
from realty_agent.tg.handlers import parse_command
from realty_agent.tg.limits import RateLimiter, can_confirm, client_notification_text, status_for_action

SECRET = "s" * 40
MANAGER_CHAT = "100000001"


def test_rate_limiter_allows_ten_then_blocks():
    limiter = RateLimiter(per_minute=10)
    now = 1_000.0
    assert all(limiter.allow("chat-1", now=now + i * 0.1) for i in range(10))
    assert not limiter.allow("chat-1", now=now + 2.0), "11-е сообщение в минуту должно блокироваться"
    assert not limiter.allow("chat-1", now=now + 50.0)
    assert limiter.allow("chat-1", now=now + 61.0), "через минуту окно очищается"


def test_rate_limiter_is_per_chat():
    limiter = RateLimiter(per_minute=1)
    assert limiter.allow("a", now=0)
    assert not limiter.allow("a", now=1)
    assert limiter.allow("b", now=1)


def test_callback_signature_round_trip_and_tamper():
    data = sign_callback(SECRET, 42, "c", MANAGER_CHAT)
    assert len(data.encode()) <= 64, "callback_data обязан влезать в 64 байта"
    assert len(data.rsplit(":", 1)[1]) == 16, "тег 64 бита, а не 32"
    assert verify_callback(SECRET, data, MANAGER_CHAT) == (42, "c")
    assert verify_callback(SECRET, data.replace(":c:", ":r:"), MANAGER_CHAT) is None, "подмена действия ломает HMAC"
    assert verify_callback(SECRET, data, "777") is None, "кнопка чужого чата не принимается"
    assert verify_callback(SECRET, "bk:42:c:deadbeef", MANAGER_CHAT) is None, "неверная подпись отбивается"
    assert verify_callback("other-secret", data, MANAGER_CHAT) is None, "чужой секрет отбивается"
    assert verify_callback("", data, MANAGER_CHAT) is None, "пустой секрет — не секрет"


def test_status_mapping_and_manager_authorization():
    assert status_for_action("c") == "confirmed"
    assert status_for_action("r") == "rejected"
    assert status_for_action("x") is None
    assert can_confirm(manager_chat_id="100000001", chat_id="100000001")
    assert not can_confirm(manager_chat_id="100000001", chat_id="777")
    assert not can_confirm(manager_chat_id="", chat_id="777")


def test_client_notification_texts():
    """Объект показывается АДРЕСОМ (внутренний id клиенту не уходит)."""
    confirmed = client_notification_text(status="confirmed", slot="01.10 11:00",
                                         address="ул. Пушкина, 10, кв. 42")
    rejected = client_notification_text(status="rejected", slot="01.10 11:00",
                                        address="ул. Пушкина, 10, кв. 42")
    assert "01.10 11:00" in confirmed and "подтверждена" in confirmed
    assert "ул. Пушкина, 10, кв. 42" in confirmed
    assert "L-1" not in confirmed, "внутренний id объекта в тексте клиенту не показываем"
    assert "другой удобный слот" in rejected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", "start"),
        ("/Start@MyBot привет", "start"),
        ("  /pending  ", "pending"),
        ("привет, /start", None),   # команда не в начале — это обычный текст
        ("/", None),
        ("", None),
        ("сколько стоит?", None),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


def test_format_slot_shows_business_timezone():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from realty_agent.tg.limits import format_slot

    msk = ZoneInfo("Europe/Moscow")
    stored = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)  # 07:00 UTC == 10:00 МСК
    assert format_slot(stored, msk) == "01.10 10:00", "менеджер должен видеть время МСК, а не UTC"
    assert format_slot("2026-10-01T10:00", msk) == "2026-10-01T10:00", "неожиданный тип отдаём как есть"


def test_format_slot_iso_converts_bridge_state_slot():
    from zoneinfo import ZoneInfo

    from realty_agent.tg.limits import format_slot_iso

    tz = ZoneInfo("Europe/Moscow")
    assert format_slot_iso("2026-10-01T10:00", tz) == "01.10 10:00"
    assert format_slot_iso("2026-10-01 10:00", tz) == "01.10 10:00"
    assert format_slot_iso("без времени", tz) == "без времени", "неожиданный формат не роняем"
