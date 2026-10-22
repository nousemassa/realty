"""Офлайн-тесты HTTP-эндпоинта генерации: auth, размер, дневной лимит, успешный путь + запись в БД."""
from __future__ import annotations

import io
from datetime import UTC

from fastapi import FastAPI
from fastapi.testclient import TestClient

import realty_agent.api.listings_router as router_module
from realty_agent.api.listings_router import router
from realty_agent.listing import ListingResult

API_KEY = "k" * 40


class _Settings:
    bridge_api_key = API_KEY
    listings_max_photo_mb = 10
    listings_max_per_day = 20
    tz = UTC  # бизнес-таймзона: по ней считается день строки трассы
    # Ссылки на воркфлоу листинга читает РУЧКА при открытии спана роли: заглушка обязана их иметь,
    # иначе тест падал бы на неполном дублёре настроек, а не на поведении.
    listing_workflow_id = "wf-listing"


class _Db:
    def __init__(self, jobs_today: int = 0) -> None:
        self._jobs_today = jobs_today
        self.inserted: list[dict] = []

    async def jobs_today(self) -> int:
        return self._jobs_today

    # Трасса хода: путь генерации объявлений пишет и её — заглушка копит строки.
    async def save_turn_trace(self, **fields) -> None:
        rows = getattr(self, "traces", None)
        if rows is None:
            rows = self.traces = []
        rows.append(fields)

    async def insert_listing_job(self, **kwargs) -> int:
        self.inserted.append(kwargs)
        return 42


def make_client(*, jobs_today: int = 0) -> tuple[TestClient, _Db]:
    app = FastAPI()
    app.include_router(router)
    db = _Db(jobs_today)
    app.state.settings = _Settings()
    app.state.db = db
    app.state.listing_channels = object()
    app.state.vision = ("http://vision", "key", "model")
    return TestClient(app), db


def _photo(size: int = 1024) -> dict:
    return {"photo": ("flat.jpg", io.BytesIO(b"x" * size), "image/jpeg")}


def test_missing_or_wrong_api_key_is_401():
    client, _ = make_client()
    assert client.post("/api/listings/generate", files=_photo(), data={"address": "a"}).status_code == 401
    assert client.post(
        "/api/listings/generate", files=_photo(), data={"address": "a"}, headers={"X-API-Key": "bad"}
    ).status_code == 401


def test_oversized_photo_is_413():
    client, _ = make_client()
    response = client.post(
        "/api/listings/generate",
        files={"photo": ("big.jpg", io.BytesIO(b"x" * (10 * 1024 * 1024 + 1)), "image/jpeg")},
        data={"address": "a"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 413


def test_too_many_pixels_is_413_not_500(monkeypatch):
    """Байты уложились, а пиксели — нет: 10 МБ JPEG бывает больше 25 Мп.

    Декодирование такого кадра в мосте с `mem_limit: 512m` — это OOM, а не «медленно», поэтому доменное
    `ImageTooLarge` обязано стать 413: клиенту видно, что фото слишком велико, а не «сервер сломался».
    """
    from realty_agent.vision import ImageTooLarge

    async def fake_generate(*, channels, provider, image_bytes, fields, **_role):
        raise ImageTooLarge(pixels=40_000_000, max_pixels=30_000_000)

    monkeypatch.setattr(router_module, "generate_listing", fake_generate)
    client, _ = make_client()
    response = client.post(
        "/api/listings/generate", files=_photo(), data={"address": "a"}, headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 413
    assert "пиксел" in response.json()["detail"]


def test_daily_limit_is_429():
    client, _ = make_client(jobs_today=20)
    response = client.post(
        "/api/listings/generate", files=_photo(), data={"address": "a"}, headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 429


def test_success_stores_job_and_returns_versions(monkeypatch):
    async def fake_generate(*, channels, provider, image_bytes, fields, **_role):
        return ListingResult(
            description="- синий диван", short="КОРОТКОЕ-текст", long="ДЛИННОЕ-текст",
            raw="КОРОТКОЕ:\nКОРОТКОЕ-текст\n\nДЛИННОЕ:\nДЛИННОЕ-текст",
            session_id="sess-1", usage={"input": 10, "output": 20, "cost": 0.5},
        )

    monkeypatch.setattr(router_module, "generate_listing", fake_generate)
    client, db = make_client()
    response = client.post(
        "/api/listings/generate",
        files=_photo(),
        data={"address": "ул. Садовая, 12", "price": "9 300 000 ₽"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == 42 and body["listing_short"] == "КОРОТКОЕ-текст" and body["cost_usd"] == 0.5
    assert db.inserted and db.inserted[0]["session_id"] == "sess-1"
    assert db.inserted[0]["fields"]["address"] == "ул. Садовая, 12"
    assert db.inserted[0]["duration_ms"] >= 0


def test_agent_empty_reply_is_502_but_job_recorded(monkeypatch):
    async def fake_generate(*, channels, provider, image_bytes, fields, **_role):
        return ListingResult(description="нет", short="", long="", raw="<текст>", session_id="s2", usage=None)

    monkeypatch.setattr(router_module, "generate_listing", fake_generate)
    client, db = make_client()
    response = client.post(
        "/api/listings/generate", files=_photo(), data={"address": "a"}, headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 502
    assert db.inserted, "черновик задания должен сохраниться для разбора"
