"""Обработчики ставятся на КАЖДОМ подключении — иначе бот «на связи», но глухой.

Механизм подключения (`agent_core/tg/connection.py`) обязан ставить обработчики: без вызова
`_register_handlers` `/health` отвечает `telegram: true`, а `updates_processed` стоит на нуле —
клиенты пишут и не получают ответа. Проверка поэтому не про «соединение есть», а про то, что оно
ЗАМЕТНО для приёма сообщений.

Почему файл живёт у продукта: механизм подключения — общий слой (и проверяется в `core/tests`), а
здесь проверяется ПРОДУКТОВАЯ сторона — что `TelegramTransport` ставит обработчики и не ставит их
дважды. Обвязка (`built_client` — подмена сборки клиента, двойник с счётчиком `handlers`) взята из
общего слоя, а не скопирована: она одна на оба потребителя механизма.
"""
from __future__ import annotations

import pytest
from fakes import make_settings, make_transport

SECRET = "ee" + "a1b2" * 8 + "c3d4e5f6a7b8"      # синтетический, НЕ боевой


def proxy_settings():
    """Настройки механизма: один MT-прокси и НЕ задан адресат приветствия.

    ❗ Адресат не задан намеренно: `_on_telegram_connected` после подключения приветствует менеджера,
    и с заданным чатом проверка ушла бы в доставку, а её предмет здесь — ОБРАБОТЧИКИ. Так было и в
    исходной обвязке этого теста; приветствие сторожат отдельные проверки продукта.
    """
    return make_settings(telegram_bot_token="123:abc", telegram_api_id="2040",
                         telegram_api_hash="b" * 32, telegram_mt_proxies=f"10.0.0.1:443:{SECRET}",
                         telegram_manager_chat_id="", telegram_owner_chat_id="")


@pytest.mark.asyncio
async def test_handlers_are_registered_when_the_bot_connects(built_client):
    """Бот был «на связи», но глухой — обработчиков не было."""
    transport = make_transport(settings=proxy_settings())
    await transport._connection.connect()  # в бою колбэк вызывается после подключения

    await transport._on_telegram_connected(first=True)

    client = transport._connection.client
    assert client is not None
    assert client.handlers > 0, "на подключении обязаны быть поставлены обработчики"
    assert transport._handlers_client is client, "запоминаем клиента, чтобы не ставить дважды"


@pytest.mark.asyncio
async def test_handlers_are_not_registered_twice_on_the_same_client(built_client):
    """Повтор на ТОМ ЖЕ клиенте дал бы дубли обработчиков — то есть двойные ответы клиенту."""
    transport = make_transport(settings=proxy_settings())
    await transport._connection.connect()

    await transport._on_telegram_connected(first=True)
    once = transport._connection.client.handlers
    await transport._on_telegram_connected(first=False)

    assert transport._connection.client.handlers == once, "второй раз ставить нельзя"


@pytest.mark.asyncio
async def test_a_new_client_gets_handlers_again(built_client):
    """Переподключение создаёт НОВЫЙ клиент — на нём обработчиков ещё нет, и это надо сделать."""
    transport = make_transport(settings=proxy_settings())
    await transport._connection.connect()
    await transport._on_telegram_connected(first=True)

    await transport._connection.connect()  # новый объект клиента
    await transport._on_telegram_connected(first=False)

    assert transport._connection.client.handlers > 0, "новый клиент обязан получить своих обработчиков"
