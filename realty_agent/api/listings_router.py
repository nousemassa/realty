"""HTTP-эндпоинт генерации объявления: фото+поля → КОРОТКОЕ/ДЛИННОЕ + запись в listing_jobs.

Защита и лимиты (план v14): X-API-Key (≥32 байта, constant-time), фото ≤10 МБ, ≤20 заданий/день.

Разграничение доступа (a): вызывающая сторона может **заявить идентичность**
(`X-Caller-Id`, платформа по умолчанию `tg`) — тогда роль читается из таблицы `managers`,
неизвестный или неактивный заявитель отбивается, а ответ **ограничивается полями**:
внутренние (идентификатор сессии агента, описание фото, стоимость, латентность) видит
только владелец. Без заявленной идентичности вызывающая сторона — это сам сервис
(сторож, скрипты владельца), и поведение остаётся прежним: ключ остаётся общим
машинным креденшелом, своего креденшела у менеджера появится только вb.
"""
from __future__ import annotations

import datetime as dt
import time

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile

from agent_core import telemetry
from agent_core.api_auth import check_api_key
from agent_core.ids import chat_hash
from realty_agent.api.acl import DEFAULT_PLATFORM, resolve_identity, restrict_response
from realty_agent.listing import ListingFields, generate_listing
from realty_agent.vision import ImageTooLarge

#: Слаг роли в атрибутах спана: так эта роль называется в наших же документах (`listing-writer`),
#: а не «listing» — тому, кто смотрит трейс, нужно то же имя, что в контракте агента.
LISTING_ROLE_SLUG = "listing-writer"

router = APIRouter(prefix="/api", tags=["listings"])


@router.post("/listings/generate")
async def generate(
    request: Request,
    photo: UploadFile = File(...),
    address: str = Form(""),
    price: str = Form(""),
    rooms: str = Form(""),
    notes: str = Form(""),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_caller_id: str | None = Header(default=None, alias="X-Caller-Id"),
    x_caller_platform: str | None = Header(default=None, alias="X-Caller-Platform"),
) -> dict:
    settings = request.app.state.settings
    db = request.app.state.db
    channels = request.app.state.listing_channels
    provider = request.app.state.vision

    if not check_api_key(x_api_key, settings.bridge_api_key):
        raise HTTPException(status_code=401, detail="invalid X-API-Key")

    # Идентичность заявлена — значит роль обязана подтвердиться строкой в `managers`.
    # Заголовка с ролью в запросе нет намеренно: роль приходит только из БД.
    identity = None
    if x_caller_id:
        identity = await resolve_identity(
            db, platform=x_caller_platform or DEFAULT_PLATFORM, chat_id=x_caller_id
        )
        if identity is None:
            raise HTTPException(status_code=403, detail="unknown or inactive caller")

    data = await photo.read()
    if len(data) > settings.listings_max_photo_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"фото больше {settings.listings_max_photo_mb} МБ")

    if await db.jobs_today() >= settings.listings_max_per_day:
        raise HTTPException(status_code=429, detail=f"дневной лимит генераций ({settings.listings_max_per_day}) исчерпан")

    fields = ListingFields(address=address, price=price, rooms=rooms, notes=notes)
    started = time.monotonic()
    try:
        result = await generate_listing(
            channels=channels, provider=provider, image_bytes=data, fields=fields,
            # Трейсер РОЛИ листинга (не моста): в приёмнике это отдельный агент `agent-listing`.
            tracer=getattr(request.app.state, "listing_tracer", None),
            # Ссылки на воркфлоу платформы — НАСТОЯЩИЕ (из настроек), а не литерал: иначе в трейсе был бы
            # выдуманный идентификатор, и «привязка к агенту» стала бы ложью.
            span_attributes=telemetry.span_attributes(
                workflow_id=settings.listing_workflow_id,
                workflow_slug=LISTING_ROLE_SLUG,
                step="listing",
            ),
        )
    except ImageTooLarge as error:
        # ❗ Байты уложились в лимит, а пиксели — нет: 10 МБ JPEG бывает больше 25 Мп, и декодирование
        # такого кадра в мосте с `mem_limit: 512m` — это OOM, а не «медленно». Отказ клиенту, а не 500.
        raise HTTPException(status_code=413, detail=str(error)) from error
    duration_ms = int((time.monotonic() - started) * 1000)
    cost = None
    if isinstance(result.usage, dict):
        cost = result.usage.get("cost")

    job_id = await db.insert_listing_job(
        session_id=result.session_id,
        fields={"address": address, "price": price, "rooms": rooms, "notes": notes},
        result=result.raw,
        duration_ms=duration_ms,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )

    # Трасса и у этого пути: без неё «$/ход» и «$/диалог» считались бы только по диалогу, а
    # расход генерации объявлений не попадал бы в отчёт владельцу вовсе.
    await db.save_turn_trace(
        day=dt.datetime.now(settings.tz).date(), platform=x_caller_platform or DEFAULT_PLATFORM,
        chat_hash=chat_hash(x_caller_id or ""), step="ads", prompt_hash="", context_items=0,
        agent_called=True, outcome="ok" if result.ok else "error",
        parser_verdict="ok" if result.ok else "no_block", session_id=result.session_id or "",
        usage=result.usage if isinstance(result.usage, dict) else None, latency_ms=duration_ms,
    )

    if not result.ok:
        detail = "агент не вернул объявление по контракту (см. listing_jobs.result)"
        if result.violations:
            detail += "; нарушения: " + "; ".join(result.violations)
        raise HTTPException(status_code=502, detail=detail)

    return restrict_response(
        {
            "job_id": job_id,
            "session_id": result.session_id,
            "listing_short": result.short,
            "listing_long": result.long,
            "photo_description": result.description,
            "duration_ms": duration_ms,
            "cost_usd": cost,
        },
        identity,
    )
