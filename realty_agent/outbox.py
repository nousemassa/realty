"""Outbox: доставка исходящих через очередь.

**Что это решает.** Раньше исходящие уходили напрямую из 17 мест, и ошибка отправки означала потерю
сообщения: ловили `NETWORK_ERRORS`, писали в журнал и возвращали `False`. Теперь у исходящего есть
строка в очереди, а у строки — состояние: она либо уйдёт, либо останется с причиной, но не исчезнет.

**Откуда взята семантика — и почему не выдумана заново.** Ту же задачу проект уже решил для хода
диалога (`realty_agent/turns.py`,), и там записаны причины каждого решения. Сюда перенесено главное:

* **`claim` переводит строку в `in_flight` ДО сетевого вызова.** Смерть процесса в любой момент после
  захвата оставляет строку в `in_flight` — то есть «отправка могла начаться». Это единственный способ
  обойтись без распределённой транзакции между нами и Telegram: «не знаю» нельзя выдавать за «не
  отправлено».
* **Залипшее `in_flight` НЕ переигрывается** (`Db.sweep_outbox`). Повтор создал бы второй ответ
  клиенту — ровно тот дефект, ради которого очередь и делается. Строка помечается `failed` с причиной
  «разбор вручную»: она не теряется, но и не отправляется вслепую.
* **Временная ошибка (лимит, таймаут) возвращает строку в очередь** с задержкой, а не отправляет её на
  разбор: лимит отпускает сам.

**Чего здесь нет.** Модуль не знает ни про Telegram, ни про формат карточек: он получает `repo` и
`sender` — вызов, который умеет доставить элемент. Поэтому всё поведение очереди проверяется тестом
без сети и без траншеек, а живой путь подставляется в бою.
"""
from __future__ import annotations

import asyncio
import logging
import random

log = logging.getLogger(__name__)

#: Сколько держим строку за собой, прежде чем считать её залипшей (процесс мог умереть).
LEASE_SECONDS = 120.0

#: Имя владельца lease — попадает в причину при разборе залипшей строки.
WORKER_NAME = "bridge-outbox"

#: Сколько раз пробуем временную ошибку, прежде чем признать её постоянной.
MAX_ATTEMPTS = 5

#: Пауза между проходами, когда работа ЕСТЬ (или только что была).
TICK_S = 2.0

#: Потолок паузы при простое. Пустая очередь — обычное состояние: стучаться в базу каждые две
#: секунды круглосуточно незачем, поэтому интервал растёт до этого предела и сбрасывается, как
#: только появилась работа.
IDLE_TICK_MAX_S = 30.0

#: Базовая задержка перед повтором временной ошибки: растёт с попытками, плюс джиттер, чтобы
#: несколько воркеров не били в сервер одновременно.
RETRY_BASE_S = 5.0


class RetryAfter(Exception):
    """Просят подождать и повторить: лимит запросов (Telegram `FloodWait`, HTTP 429).

    Отдельный тип, а не текст ошибки: от него зависит решение «повторить позже» против «признать
    постоянной». В проекте такого типа не было — `FloodWaitError` ловился по имени класса в двух
    местах, а в отправках лимит просто глотался как сетевая ошибка.
    """

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason or f"повторить через {seconds:.0f} с")
        self.seconds = float(seconds)


async def process_once(repo, sender, *, on_result=None) -> int:
    """Обработать ОДНУ строку очереди. Возвращает число обработанных строк (0 — работы нет).

    Порядок шагов существенен: захват (который и помечает «отправка могла начаться») → отправка →
    отметка результата. Между захватом и отметкой процесс может умереть — тогда строку разберёт
    `sweep_outbox`, и она НЕ будет отправлена второй раз.

    `on_result(outcome, item=…, error=…)` — необязательный получатель исхода (`sent`/`failed`/`retry`).
    Вынесен наружу, чтобы модуль не знал ни про метрики, ни про телеграм-специфику: очередь умеет
    доставлять, а что делать с исходом — дело процесса. `item` и `error` передаются потому, что часть
    реакций зависит от КОНКРЕТНОЙ ошибки: например «адресат недостижим» (блокировка, удаление аккаунта) —
    это решение по реальной попытке доставки, а не по моменту постановки.
    """
    item = await repo.claim_outbox(lease_seconds=LEASE_SECONDS, owner=WORKER_NAME)
    if item is None:
        return 0

    outbox_id = int(item["id"])
    kind = str(item.get("kind") or "")
    try:
        await sender(item)
    except RetryAfter as error:
        delay = error.seconds + random.uniform(0, 1.0)
        if int(item.get("attempts") or 0) >= MAX_ATTEMPTS:
            await repo.mark_outbox_failed(
                outbox_id, error=f"лимит не отпустил за {MAX_ATTEMPTS} попыток: {error}")
            log.warning("outbox: строка %s (%s) — лимит не отпустил, помечена failed", outbox_id, kind)
            await _report(on_result, "failed", item=item, error=error)
            return 1
        await repo.release_outbox(outbox_id, delay_seconds=delay, error=f"лимит: {error}")
        log.info("outbox: строка %s (%s) отложена на %.0f с (лимит)", outbox_id, kind, delay)
        await _report(on_result, "retry", item=item, error=error)
        return 1
    except Exception as error:  # noqa: BLE001 — очередь обязана пережить любую ошибку отправки
        await repo.mark_outbox_failed(outbox_id, error=f"{type(error).__name__}: {error}")
        log.warning("outbox: строка %s (%s) не отправлена (%s: %s)",
                    outbox_id, kind, type(error).__name__, error)
        await _report(on_result, "failed", item=item, error=error)
        return 1

    await repo.mark_outbox_sent(outbox_id)
    log.info("outbox: строка %s (%s) отправлена", outbox_id, kind)
    await _report(on_result, "sent", item=item)
    return 1


async def _report(on_result, outcome: str, *, item: dict | None = None, error: Exception | None = None) -> None:
    """Сообщить исход наружу. Ошибка счётчика не роняет доставку — она уже состоялась."""
    if on_result is None:
        return
    try:
        await on_result(outcome, item=item, error=error)
    except Exception as report_error:  # noqa: BLE001
        log.warning("outbox: исход %s не учтён (%s: %s)",
                    outcome, type(report_error).__name__, report_error)


async def run_forever(*, repo, sender, tick_s: float = TICK_S, on_tick=None, on_result=None,
                       wake=None) -> None:
    """Крутить очередь, пока задачу не отменят.

    Форма цикла — та же, что у остальных петель проекта (`graph/scheduler.py`, `bot.py`): ошибка
    одного прохода не убивает цикл, отмена пробрасывается наружу, чтобы остановка была быстрой.

    `wake` — событие, которым постановщик будит воркера. Без него интервал при простое растёт
    до `IDLE_TICK_MAX_S`, и только что поставленное сообщение ждало бы до полуминуты (замерено живьём:
    алерт доставлен через 29 с). Пробуждение убирает эту задержку, не заставляя цикл стучаться в базу
    каждые две секунды: спим долго, но просыпаемся сразу, как появилась работа.
    """
    log.info("outbox: воркер очереди запущен (владелец %s)", WORKER_NAME)
    pause = tick_s
    while True:
        try:
            await _pause(pause, wake)
            worked = 0
            while await process_once(repo, sender, on_result=on_result) > 0:
                # Разгребаем, пока есть работа: очередь обычно короткая, а ждать тика незачем.
                worked += 1
            if on_tick is not None:
                # Раз в проход: пик лага за день. Числа пишет вызывающий, не очередь.
                await on_tick()
            # Работа была — держим короткий интервал; очередь пуста — растягиваем паузу до потолка.
            pause = tick_s if worked else min(max(pause, tick_s) * 2, IDLE_TICK_MAX_S)
        except asyncio.CancelledError:
            log.info("outbox: воркер остановлен")
            raise
        except Exception:
            log.exception("outbox: проход очереди завершился ошибкой — продолжаю")


async def _pause(seconds: float, wake) -> None:
    """Пауза, которую можно прервать пробуждением: событие сработало — просыпаемся немедленно."""
    if wake is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(wake.wait(), timeout=seconds)
    except TimeoutError:
        pass
    wake.clear()
