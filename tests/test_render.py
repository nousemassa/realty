"""Тесты привязки UI-слоя к Telegram (`tg/render.py`) — раньше не покрывалась вообще.

Особенность Telethon 1.45 (проверено на месте): `Button.inline(label, data)` отдаёт
`tl.types.KeyboardInlineButton` (текст — `.text`, данные — `.type.data`), а `Button.text(label)` —
обёртку `tl.custom.button.Button` (текст — `.button.text`). Поэтому ниже есть аксессоры.
"""
from __future__ import annotations

import pytest
from telethon import Button
from telethon.tl.types import KeyboardInlineButton

from agent_core.ui import tokens
from agent_core.ui.reply import INLINE, REPLY, Action, Reply
from realty_agent.tg import render
from realty_agent.ui import keyboards as kb
from realty_agent.ui import menu
from realty_agent.ui.kinds import KIND_CONSENT, KIND_LISTING, KIND_MENU

SECRET = "ui-secret-for-tests-0123456789"
CHAT = "100000001"
TOKEN = "abc123"


def label_of(button) -> str:
    return button.button.text if hasattr(button, "button") else button.text


def data_of(button) -> str:
    return button.type.data.decode()


def test_reply_without_rows_has_no_keyboard():
    assert render.to_buttons(Reply(text="просто текст"), secret=SECRET, chat_id=CHAT, token=TOKEN) is None


def test_inline_actions_are_signed_and_within_limit():
    reply = Reply(text="объекты", rows=[[Action("1 · тест", KIND_LISTING, "0")]])
    button = render.to_buttons(reply, secret=SECRET, chat_id=CHAT, token=TOKEN)[0][0]
    assert isinstance(button, KeyboardInlineButton)
    data = data_of(button)
    assert data.startswith("c:l:0:")
    assert len(data.encode()) <= tokens.MAX_CALLBACK_BYTES
    assert tokens.verify(SECRET, data, chat_id=CHAT, token=TOKEN) == ("l", "0")


def test_menu_layout_builds_text_buttons():
    reply = Reply(text="меню", rows=kb.client_menu_kb(), kind=REPLY)
    rows = render.to_buttons(reply, secret=SECRET, chat_id=CHAT, token=TOKEN)
    labels = [label_of(button) for row in rows for button in row]
    assert labels[0] == "🏠 Объекты" and "📄 Политика" in labels


def test_owner_menu_counter_label_survives_rendering():
    reply = Reply(text="меню овнера", rows=kb.owner_menu_kb(pending=3, paused=False), kind=REPLY)
    rows = render.to_buttons(reply, secret=SECRET, chat_id=CHAT, token=TOKEN)
    assert label_of(rows[0][0]) == "📋 Заявки (3)"


def test_consent_button_is_unsigned_inline():
    reply = Reply(text="согласие", rows=[[Action("✅ Согласен(а)", KIND_CONSENT)]], kind=INLINE)
    button = render.to_buttons(reply, secret=SECRET, chat_id=CHAT, token=TOKEN)[0][0]
    assert data_of(button) == render.CONSENT_CALLBACK


def test_mixing_reply_and_inline_is_rejected_before_telegram():
    mixed = Reply(
        text="плохо",
        rows=[[Action("🏠 Объекты", KIND_MENU, "objects"), Action("1", KIND_LISTING, "0")]],
        kind=REPLY,
    )
    with pytest.raises(ValueError, match="reply-клавиатура"):
        render.to_buttons(mixed, secret=SECRET, chat_id=CHAT, token=TOKEN)


def test_consent_inside_reply_layout_is_rejected():
    mixed = Reply(text="плохо", rows=[[Action("✅ Согласен(а)", KIND_CONSENT)]], kind=REPLY)
    with pytest.raises(ValueError, match="reply-клавиатура"):
        render.to_buttons(mixed, secret=SECRET, chat_id=CHAT, token=TOKEN)


def test_signature_is_bound_to_chat_and_token():
    reply = Reply(text="слоты", rows=[[Action("10:00", "t", "0")]])
    data = data_of(render.to_buttons(reply, secret=SECRET, chat_id=CHAT, token=TOKEN)[0][0])
    assert tokens.verify(SECRET, data, chat_id="777", token=TOKEN) is None
    assert tokens.verify(SECRET, data, chat_id=CHAT, token="other") is None


def test_owner_bookings_label_resolves_back_to_menu_code():
    rows = [[Action(menu.label(menu.MENU_OWNER_BOOKINGS, 12), KIND_MENU, menu.MENU_OWNER_BOOKINGS)]]
    reply = Reply(text="меню", rows=rows, kind=REPLY)
    button = render.to_buttons(reply, secret=SECRET, chat_id=CHAT, token=TOKEN)[0][0]
    assert menu.resolve_menu(label_of(button)) == menu.MENU_OWNER_BOOKINGS


def test_reply_layout_returns_custom_button_wrapper():
    reply = Reply(text="меню", rows=kb.client_menu_kb(), kind=REPLY)
    button = render.to_buttons(reply, secret=SECRET, chat_id=CHAT, token=TOKEN)[0][0]
    assert isinstance(button, Button)
