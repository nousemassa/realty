"""Обработчики inline-кнопок менеджера (каркас, D7).

Инварианты:
  * callback_data ≤64B и подписан: `bk:{booking_id}:{c|r}:{mac16}`; HMAC покрывает ЧАТ, заявку и
    действие — кнопка одного чата не принимается в другом (кроме того, есть `can_confirm`);
  * HMAC — ОТДЕЛЬНЫЙ UI-секрет, не HTTP-ключ; пустой секрет ⇒ любые подписи отвергаются (не «секрет = ""»);
  * тег 16 hex (64 бита): 32 бита перебираемы, а лимита на callback нет;
  * обязателен `answer_callback_query` (иначе спиннер и повторные тапы);
  * после обработки снимаем кнопки у ТАПНУТОГО сообщения; копии у других адресатов гасятся CAS-ом
    («Заявка уже обработана») — `booking_notifications` вне объёма ( C-16);
  * атомарный CAS: UPDATE bookings SET status=… WHERE id=? AND status='pending' RETURNING id.
"""
from __future__ import annotations

import hashlib
import hmac

TAG_HEX = 16


def _tag(secret: str, booking_id: int | str, action: str, chat_id: str) -> str:
    message = f"{chat_id}|{booking_id}|{action}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:TAG_HEX]


def sign_callback(secret: str, booking_id: int, action: str, chat_id: str) -> str:
    return f"bk:{booking_id}:{action}:{_tag(secret, booking_id, action, chat_id)}"


def verify_callback(secret: str, data: str, chat_id: str) -> tuple[int, str] | None:
    if not secret:
        return None
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != "bk":
        return None
    _, booking_id, action, mac = parts
    if not hmac.compare_digest(mac, _tag(secret, booking_id, action, chat_id)):
        return None
    try:
        return int(booking_id), action
    except ValueError:
        return None
