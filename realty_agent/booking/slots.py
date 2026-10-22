"""Расчёт свободных слотов — один источник для транспорта и MCP-инструмента.

**Почему отдельный модуль.** До эта логика жила методом транспорта
(`tg/bot.py:_free_slots`). MCP-инструмент `get_available_slots` читает то же самое, и
вторая копия расписания неизбежно разошлась бы с первой: клиент через бота и клиент
через инструмент увидели бы разное время. Правило проекта — «один источник на факт» —
здесь применяется буквально: транспорт и инструмент зовут одну функцию.

Правила берутся из настроек (`settings.schedule`), а не из литералов: рабочие часы,
горизонт и выходной день настраиваются без правки кода.
"""
from __future__ import annotations

import datetime as dt
import logging

log = logging.getLogger(__name__)

# Сколько кандидатов проверяем на занятость. Совпадает с прежним поведением транспорта:
# клиенту показываем ограниченный набор, а не весь горизонт.
OFFER_SLOTS = 12


async def free_slots(db, *, tz, listing_id: str | None = None, limit: int = OFFER_SLOTS) -> list[str]:
    """Свободные слоты на горизонте из настроек, в бизнес-таймзоне.

    Занятость проверяется ОДНИМ запросом и глобально (одно время — один просмотр):
    занятое время клиенту не показывается никогда, фолбэка «показать всё» нет.
    """
    schedule = await db.setting("schedule", {}) or {}
    start_hour = int(str(schedule.get("start", "10:00")).split(":")[0])
    end_hour = int(str(schedule.get("end", "19:00")).split(":")[0])
    horizon = int(schedule.get("horizon_days", 7))

    now = dt.datetime.now(tz)
    slots: list[str] = []
    for day in range(1, horizon + 1):
        date = now + dt.timedelta(days=day)
        if date.weekday() == 6:  # воскресенье — выходной
            continue
        for hour in range(start_hour, end_hour):
            slots.append(f"{date:%Y-%m-%d}T{hour:02d}:00")

    candidates = list(dict.fromkeys(slots[:limit]))
    busy = await db.busy_slots(candidates)
    free = [slot for slot in candidates if slot not in busy]
    log.info(
        "slots: listing=%s cand=%d free=%d busy=%d", listing_id, len(candidates), len(free), len(busy)
    )
    return free
