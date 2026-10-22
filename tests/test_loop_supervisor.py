"""Фоновые петли бота: их смерть не молчит.

**Зачем это отдельный файл.** Непрочитанный `task.exception()` в трёх деревьях означал бы, что петля
календаря умирает на вычислении настройки, `/health` остаётся зелёным, а синхронизация просто
перестаёт происходить. Проверяются три вещи: (1) непригодная настройка паузы петлю не убивает,
(2) смерть видна в `/health.loops`, (3) смерть попадает в журнал.

**Без порогов времени.** Тест не ждёт 60 секунд: он отменяет задачу в момент паузы и утверждает
СОСТОЯНИЕ. Тайминговых утверждений в репозитории нет, и заводить их ради этого — флак по определению.
"""

from __future__ import annotations

import asyncio
import logging
import types

import fakes
import pytest

from realty_agent.tg.bot import TelegramTransport


def _transport() -> TelegramTransport:
    return fakes.make_transport()


async def test_calendar_loop_survives_a_bad_setting():
    """Настройка паузы читается ВНУТРИ `try`: иначе `AttributeError`/`TypeError` убивал петлю навсегда."""
    transport = _transport()
    passes: list[int] = []

    async def sync_calendar():
        passes.append(1)
        return {"enabled": False}

    transport.sync_calendar = sync_calendar
    transport._settings = types.SimpleNamespace()          # поля `calendar_poll_s` НЕТ

    task = asyncio.create_task(transport._calendar_loop())
    for _ in range(3):
        await asyncio.sleep(0)                              # дать пройти проход и упасть на настройке

    assert task.done() is False, "петля умерла от непригодной настройки — синхронизация исчезла бы молча"
    assert passes == [1], "проход по календарю должен был состояться до чтения настройки"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_loops_reports_absence_and_configuration_separately():
    """Четыре состояния, а не два: `not_configured` — норма (нет `turns`/базы), сторож краснеет на `dead`."""
    transport = _transport()

    assert transport.loops() == {
        "reminders": "not_started",
        "calendar": "not_started",
        "turn_sweep": "not_configured",
        "outbox": "not_configured",
    }


async def test_dead_loop_is_visible_in_health_and_in_the_log(caplog):
    """Смерть петли: `dead` в `/health` (через `stats()`, который мост разворачивает целиком) и `error` в журнале."""
    transport = _transport()

    async def boom():
        raise RuntimeError("календарь не настроен")

    with caplog.at_level(logging.ERROR):
        transport._calendar_task = transport._watch("calendar", asyncio.create_task(boom()))
        for _ in range(3):
            await asyncio.sleep(0)

    assert transport.loops()["calendar"] == "dead"
    #: Поле доезжает до `/health` без правки моста: он разворачивает `stats()` целиком.
    assert transport.stats()["loops"]["calendar"] == "dead"
    assert any("calendar" in record.message for record in caplog.records), "смерть петли обязана быть в журнале"
