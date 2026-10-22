"""Офлайн-тесты ACL (E13a): роль из БД, ограничение полей вместо отказа.

Что именно здесь доказано — по пункту DoD:
  * роль читается из `managers`, а не из заголовка запроса (заголовок с чужой ролью
    ничего не меняет);
  * неизвестный и неактивный заявитель отбиваются;
  * клиентский контекст получает ОГРАНИЧЕННЫЙ ответ: приватных полей в нём нет,
    но запрос **не отклоняется**;
  * владелец получает полный набор.

Чего здесь нет: тестов на выпуск/проверку/отзыв токена — в E13a токенов нет
(свой сервис аккаунтов и OIDC — это E13b, отложен).
"""
from __future__ import annotations

import io
from datetime import UTC

from fastapi import FastAPI
from fastapi.testclient import TestClient

import realty_agent.api.listings_router as router_module
from realty_agent.api.acl import (
    INTERNAL_RESPONSE_FIELDS,
    PUBLIC_RESPONSE_FIELDS,
    Identity,
    restrict_response,
)
from realty_agent.api.listings_router import router
from realty_agent.listing import ListingResult

API_KEY = "k" * 40
OWNER_ID = "111"
MANAGER_ID = "222"
INACTIVE_ID = "333"
UNKNOWN_ID = "999"

# Внутренние поля, которые не должны доходить до клиентского контекста.
PRIVATE_FIELDS = ("session_id", "photo_description", "duration_ms", "cost_usd")


class _Settings:
    bridge_api_key = API_KEY
    listings_max_photo_mb = 10
    listings_max_per_day = 20
    tz = UTC  # бизнес-таймзона: по ней считается день строки трассы
    # Ссылки на воркфлоу листинга читает РУЧКА при открытии спана роли: заглушка обязана их иметь,
    # иначе тест падал бы на неполном дублёре настроек, а не на поведении.
    listing_workflow_id = "wf-listing"


class _Db:
    """Фейк БД: роль отдаётся так же, как её отдал бы SQL — только для активных."""

    def __init__(self, roles: dict[str, str]) -> None:
        self._roles = roles
        self.inserted: list[dict] = []

    async def jobs_today(self) -> int:
        return 0

    # Трасса хода: путь генерации объявлений пишет и её — заглушка копит строки.
    async def save_turn_trace(self, **fields) -> None:
        rows = getattr(self, "traces", None)
        if rows is None:
            rows = self.traces = []
        rows.append(fields)

    async def insert_listing_job(self, **kwargs) -> int:
        self.inserted.append(kwargs)
        return 42

    async def manager_role(self, *, chat_id: str, platform: str = "tg") -> str | None:
        # Неактивных в словаре нет вовсе — как и в SQL с условием `AND active`.
        return self._roles.get(chat_id)


def make_client() -> tuple[TestClient, _Db]:
    app = FastAPI()
    app.include_router(router)
    db = _Db({OWNER_ID: "owner", MANAGER_ID: "manager"})  # INACTIVE_ID и UNKNOWN_ID отсутствуют
    app.state.settings = _Settings()
    app.state.db = db
    app.state.listing_channels = object()
    app.state.vision = ("http://vision", "key", "model")
    return TestClient(app), db


def _photo() -> dict:
    return {"photo": ("flat.jpg", io.BytesIO(b"x" * 1024), "image/jpeg")}


def _generate(monkeypatch, headers: dict | None = None) -> tuple[int, dict, _Db]:
    async def fake_generate(*, channels, provider, image_bytes, fields, **_role):
        return ListingResult(
            description="внутреннее описание фото",
            short="КОРОТКОЕ",
            long="ДЛИННОЕ",
            raw="raw",
            session_id="sess-secret",
            usage={"cost": 0.5},
        )

    monkeypatch.setattr(router_module, "generate_listing", fake_generate)
    client, db = make_client()
    all_headers = {"X-API-Key": API_KEY}
    all_headers.update(headers or {})
    response = client.post(
        "/api/listings/generate", files=_photo(), data={"address": "a"}, headers=all_headers
    )
    return response.status_code, response.json() if response.status_code == 200 else {}, db


def test_client_context_gets_limited_response_without_private_fields(monkeypatch):
    """Главный тест пункта: запрос НЕ отклонён, но приватных полей в ответе нет."""
    status, body, _ = _generate(monkeypatch, {"X-Caller-Id": MANAGER_ID})

    assert status == 200, "клиентский контекст не должен получать отказ — ACL это ограничение, а не 403"
    for field in PRIVATE_FIELDS:
        assert field not in body, f"приватное поле {field} утекло в клиентский контекст"
    assert body["job_id"] == 42 and body["listing_short"] == "КОРОТКОЕ"


def test_owner_gets_full_response(monkeypatch):
    status, body, _ = _generate(monkeypatch, {"X-Caller-Id": OWNER_ID})

    assert status == 200
    for field in PRIVATE_FIELDS:
        assert field in body, f"владелец должен получать {field}"
    assert body["session_id"] == "sess-secret" and body["cost_usd"] == 0.5


def test_role_is_read_from_db_not_from_header(monkeypatch):
    """Заголовок с чужой ролью ничего не меняет: роль приходит только из `managers`."""
    status, body, _ = _generate(
        monkeypatch,
        {
            "X-Caller-Id": MANAGER_ID,
            "X-Caller-Role": "owner",        # такого заголовка в коде нет — и он не работает
            "X-Role": "owner",
            "X-User-Role": "owner",
        },
    )

    assert status == 200
    assert "session_id" not in body and "cost_usd" not in body, "роль из заголовка не должна повышать права"


def test_unknown_caller_is_rejected(monkeypatch):
    status, _, db = _generate(monkeypatch, {"X-Caller-Id": UNKNOWN_ID})

    assert status == 403
    assert not db.inserted, "до генерации дело доходить не должно"


def test_inactive_caller_is_rejected(monkeypatch):
    """`active = false` роли не даёт: снятие прав действует сразу."""
    status, _, db = _generate(monkeypatch, {"X-Caller-Id": INACTIVE_ID})

    assert status == 403
    assert not db.inserted


def test_without_identity_behaviour_is_unchanged(monkeypatch):
    """Заявитель — сам сервис (сторож, скрипты владельца): поведение прежнее, полный ответ."""
    status, body, _ = _generate(monkeypatch)

    assert status == 200
    assert "session_id" in body and "cost_usd" in body


def test_wrong_api_key_still_401_even_with_identity(monkeypatch):
    """Ключ остаётся единственным машинным креденшелом: без него идентичность не помогает."""
    async def fake_generate(**_):
        raise AssertionError("генерация не должна вызываться")

    monkeypatch.setattr(router_module, "generate_listing", fake_generate)
    client, _ = make_client()
    response = client.post(
        "/api/listings/generate",
        files=_photo(),
        data={"address": "a"},
        headers={"X-API-Key": "bad", "X-Caller-Id": OWNER_ID},
    )
    assert response.status_code == 401


def test_restrict_response_drops_internal_keys_entirely():
    """Внутренние ключи не приходят пустыми — их в ответе нет вовсе."""
    payload = {"job_id": 1, "listing_short": "s", "listing_long": "l", **{f: "x" for f in PRIVATE_FIELDS}}

    limited = restrict_response(payload, Identity("tg", MANAGER_ID, "manager"))

    assert set(limited) == set(PUBLIC_RESPONSE_FIELDS)
    assert not set(limited) & set(INTERNAL_RESPONSE_FIELDS)


def test_restrict_response_keeps_everything_for_owner():
    payload = {"job_id": 1, "listing_short": "s", "listing_long": "l", "session_id": "x", "cost_usd": 0.1}

    assert restrict_response(payload, Identity("tg", OWNER_ID, "owner")) == payload
