"""Служебная ручка моста для внешнего сторожа (`bridge-health.timer` → `ops/health.sh`).

`POST /api/ops/alert` — доставить текст владельцу и активным менеджерам в Telegram. Без этой ручки
падение моста остаётся незамеченным: сторожу некуда сообщить, а очередь исходящих о добавляет запись
только в журнал.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from agent_core.api_auth import check_api_key

router = APIRouter(prefix="/api", tags=["ops"])

MAX_ALERT_CHARS = 500


class AlertBody(BaseModel):
    text: str


class PreviewBody(BaseModel):
    summary: str
    in_hours: float = 2.0
    location: str = ""


@router.post("/ops/alert")
async def alert(
    body: AlertBody,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    settings = request.app.state.settings
    if not check_api_key(x_api_key, settings.bridge_api_key):
        raise HTTPException(status_code=401, detail="invalid X-API-Key")

    telegram = getattr(request.app.state, "telegram", None)
    if telegram is None or not telegram.running:
        # Транспорт не поднят — доставить нечем: сторож увидит 503 и напишет предупреждение в лог/journal.
        raise HTTPException(status_code=503, detail="telegram transport is not running")

    return {"status": "ok", "recipients": await telegram.alert_ops(body.text[:MAX_ALERT_CHARS])}


@router.post("/ops/calendar/check")
async def calendar_check(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Ручной проход по календарю: тот же путь, что у фонового цикла (демо/проверка без ожидания)."""
    settings = request.app.state.settings
    if not check_api_key(x_api_key, settings.bridge_api_key):
        raise HTTPException(status_code=401, detail="invalid X-API-Key")

    telegram = getattr(request.app.state, "telegram", None)
    if telegram is None or not telegram.running:
        raise HTTPException(status_code=503, detail="telegram transport is not running")

    return {"status": "ok", **await telegram.sync_calendar()}


@router.post("/ops/calendar/preview")
async def calendar_preview(
    body: PreviewBody,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Собрать текст уведомления на синтетических фактах (проверка, ничего не отправляется)."""
    settings = request.app.state.settings
    if not check_api_key(x_api_key, settings.bridge_api_key):
        raise HTTPException(status_code=401, detail="invalid X-API-Key")

    telegram = getattr(request.app.state, "telegram", None)
    if telegram is None or not telegram.running:
        raise HTTPException(status_code=503, detail="telegram transport is not running")

    return {
        "status": "ok",
        **await telegram.preview_calendar_message(
            summary=body.summary, in_hours=body.in_hours, location=body.location
        ),
    }


@router.get("/ops/traces")
async def traces(
    request: Request,
    days: int = 7,
    limit: int = 200,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Трассы ходов и агрегаты за период.

    Отдельная ручка нужна не «на будущее»: агрегат стоимости читает отчёт владельцу, а тот же расчёт
    нужен сторожу и разбору происшествий. Один расчёт в `db.turn_report`, два входа.

    Персональных данных здесь нет по построению: в таблице только хеш идентификатора клиента.
    """
    settings = request.app.state.settings
    if not check_api_key(x_api_key, settings.bridge_api_key):
        raise HTTPException(status_code=401, detail="invalid X-API-Key")
    db = request.app.state.db
    return {
        "totals": await db.turn_report(days=days),
        "traces": await db.turn_traces(days=days, limit=limit),
    }
