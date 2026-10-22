"""Офлайн-тесты служебного алерта: auth, поведение без транспорта, доставка адресатам."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from realty_agent.api.ops_router import MAX_ALERT_CHARS, router

API_KEY = "k" * 40


class _Settings:
    bridge_api_key = API_KEY


class _Telegram:
    def __init__(self, *, running: bool = True, sent: int = 2) -> None:
        self.running = running
        self.sent = sent
        self.texts: list[str] = []

    async def alert_ops(self, text: str) -> int:
        self.texts.append(text)
        return self.sent


def make_client(telegram: _Telegram | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = _Settings()
    if telegram is not None:
        app.state.telegram = telegram
    return TestClient(app)


def test_alert_requires_api_key():
    client = make_client(_Telegram())
    assert client.post("/api/ops/alert", json={"text": "x"}).status_code == 401
    assert client.post("/api/ops/alert", json={"text": "x"}, headers={"X-API-Key": "bad"}).status_code == 401


def test_alert_without_transport_is_503():
    """Мост ещё не поднял транспорт — доставлять нечем, сторож увидит 503."""
    client = make_client()
    assert client.post("/api/ops/alert", json={"text": "бот недоступен"}, headers={"X-API-Key": API_KEY}).status_code == 503


def test_alert_with_stopped_transport_is_503():
    client = make_client(_Telegram(running=False))
    assert client.post("/api/ops/alert", json={"text": "бот недоступен"}, headers={"X-API-Key": API_KEY}).status_code == 503


def test_alert_delivers_and_truncates_long_text():
    telegram = _Telegram()
    client = make_client(telegram)
    response = client.post("/api/ops/alert", json={"text": "б" * 900}, headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "recipients": 2}
    assert len(telegram.texts[0]) == MAX_ALERT_CHARS
