"""Роли агентов продукта: диалог клиента и генератор объявлений.

Роль объявляет свои каналы, ревизии, файл промпта, цены и свойства для своего рантайма. Ядро имён
ролей не знает — оно получает `RoleSpec`.
"""
from __future__ import annotations

from agent_core.vendors.runtime.port import RoleSpec
from realty_agent.settings import settings

#: Сколько реплик истории читает свой рантайм диалога: клиентские уточнения живут в пределах пары
#: ходов, а каждое лишнее сообщение — деньги в следующем запросе.
DIALOG_HISTORY_LIMIT = 8


def dialog_role() -> RoleSpec:
    """Роль диалога клиента: запись на просмотр."""
    return RoleSpec(
        name="dialog",
        bot=settings.agent_bot_dialog,
        connection_slug=settings.dialog_connection_slug,
        connection_name="Realtor Bot",
        workflow_id=settings.dialog_workflow_id,
        revision_id=settings.dialog_revision_id,
        prompt_path=settings.dialog_prompt_path,
        price_in=settings.dialog_price_in_per_1k_usd,
        price_out=settings.dialog_price_out_per_1k_usd,
        own_supported=True,
        history_limit=DIALOG_HISTORY_LIMIT,
    )


def listing_role() -> RoleSpec:
    """Роль объявлений: каждый ход — новый тред, истории нет по смыслу."""
    return RoleSpec(
        name="listing",
        bot=settings.agent_bot_listing,
        connection_slug=settings.listing_connection_slug,
        connection_name="Listing Writer",
        workflow_id=settings.listing_workflow_id,
        revision_id=settings.listing_revision_id,
        prompt_path=settings.listing_prompt_path,
        price_in=settings.listing_price_in_per_1k_usd,
        price_out=settings.listing_price_out_per_1k_usd,
        own_supported=True,
        history_limit=0,
    )


def roles() -> dict[str, RoleSpec]:
    return {"dialog": dialog_role(), "listing": listing_role()}
