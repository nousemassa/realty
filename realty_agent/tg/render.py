"""Рендер кнопок продукта: общий транспорт плюс политика видов кнопок.

Ядро знает, КАК отправлять; продукт объявляет, ЧТО означает каждый вид (`ui/kinds.py`).
"""
from __future__ import annotations

from agent_core.tg import render as core
from agent_core.ui.reply import Reply
from realty_agent.ui.kinds import BUTTON_POLICY, KIND_CONSENT, KIND_POLICY

#: Те же значения, что в политике: их читают обработчики нажатий и тесты.
CONSENT_CALLBACK = BUTTON_POLICY.callbacks[KIND_CONSENT]
POLICY_CALLBACK = BUTTON_POLICY.callbacks[KIND_POLICY]


def to_buttons(reply: Reply, *, secret: str, chat_id: str, token: str) -> list[list] | None:
    return core.to_buttons(reply, secret=secret, chat_id=chat_id, token=token, policy=BUTTON_POLICY)


async def send(event, reply: Reply, *, secret: str, chat_id: str, token: str = "") -> None:
    await core.send(event, reply, secret=secret, chat_id=chat_id, token=token, policy=BUTTON_POLICY)
