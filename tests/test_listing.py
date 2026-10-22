"""Офлайн-тесты ответа агента: контракт, сборка хода, транскодинг фото.

Разбор ответа проверяется по контракту: эвристика `split_variants` заменена схемой
`realty_agent/contracts.py`, а её поведение зафиксировано в `test_structured_output.py` —
вместе с ретраем.
"""
from __future__ import annotations

import io

import pytest

from realty_agent.contracts import ListingContractError, ListingReply
from realty_agent.listing import ListingFields, build_listing_turn, new_job_user
from realty_agent.vision import to_jpeg

SHORT = "2-комн. квартира 54 м², ул. Пушкина, 10. Ремонт 2023, балкон. 8 500 000 ₽."
LONG = "2-комнатная квартира, 54 м², ул. Пушкина, 10. " + "Подробное описание объекта и условий сделки. " * 3


def test_listing_reply_parses_labeled_answer():
    parsed = ListingReply.parse(f"КОРОТКОЕ:\n{SHORT}\n\nДЛИННОЕ:\n{LONG}")
    assert parsed.short.startswith("2-комн.") and parsed.long.startswith("2-комнатная")


def test_listing_reply_rejects_placeholder_echo():
    """Реальный дефект: модель эхнула шаблон — заглушка не должна пройти контракт."""
    for placeholder in ("<текст>", "текст", "…", "кратко", "<TEXT>"):
        with pytest.raises(ListingContractError):
            ListingReply.parse(f"КОРОТКОЕ:\n{placeholder}\n\nДЛИННОЕ:\n{placeholder}")


def test_listing_reply_rejects_unlabeled_reply():
    """Ответ без меток больше не принимается как «длинное»: это нарушение контракта."""
    with pytest.raises(ListingContractError):
        ListingReply.parse(LONG)


def test_build_listing_turn_contains_fields_and_description():
    turn = build_listing_turn(
        description="- синий диван, окно во двор",
        fields=ListingFields(address="ул. Пушкина, 10", price="8 500 000 ₽", rooms="2-комн., 54 м²", notes="без мебели"),
    )
    for needle in ("ул. Пушкина, 10", "8 500 000 ₽", "2-комн., 54 м²", "без мебели", "синий диван", "недоверенный"):
        assert needle in turn
    assert "КОРОТКОЕ" in turn and "ДЛИННОЕ" in turn


def test_new_job_user_is_unique():
    assert new_job_user() != new_job_user()


def test_to_jpeg_converts_png_to_jpeg():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 40, 40)).save(buf, format="PNG")
    jpeg = to_jpeg(buf.getvalue())
    assert jpeg[:2] == b"\xff\xd8"                     # SOI маркер JPEG
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG" and img.size == (300, 200)


def test_to_jpeg_downscales_large_images():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (10, 10, 10)).save(buf, format="JPEG")
    with Image.open(io.BytesIO(to_jpeg(buf.getvalue(), max_side=1600))) as img:
        assert max(img.size) <= 1600


def test_to_jpeg_refuses_too_many_pixels(monkeypatch):
    """Отказ по ПИКСЕЛЯМ, а не по байтам: 10 МБ JPEG бывает больше 25 Мп, а `convert("RGB")` держит
    полный кадр плюс копию — в мосте с `mem_limit: 512m` это OOM.

    Порог подменяем числом, а не строим 30-мегапиксельный кадр: тест обязан быть дешёвым.
    """
    from PIL import Image

    from realty_agent import vision

    monkeypatch.setattr(vision, "MAX_PIXELS", 100)
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (1, 2, 3)).save(buf, format="JPEG")
    with pytest.raises(vision.ImageTooLarge):
        vision.to_jpeg(buf.getvalue())


async def test_describe_photo_does_the_pixel_work_outside_the_event_loop(monkeypatch):
    """PIL + base64 — НЕ в потоке событийного цикла.

    Сотни миллисекунд без единого `await` прямо в цикле моста замораживают приём апдейтов Telegram и
    `/health`. Проверяем детерминированно — по потоку, а не по времени: порог в секундах флакует на
    машине, где рядом крутится стенд.
    """
    import threading

    import httpx
    from PIL import Image

    from realty_agent import vision

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buf, format="JPEG")
    photo = buf.getvalue()
    threads: list[str] = []
    real_to_jpeg = vision.to_jpeg

    def spy(data, **kwargs):
        threads.append(threading.current_thread().name)
        return real_to_jpeg(data, **kwargs)

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "описание фактами"}}]}

    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def post(self, *args, **kwargs) -> _Response:
            return _Response()

    monkeypatch.setattr(vision, "to_jpeg", spy)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    text = await vision.describe_photo(base_url="http://vision.local", api_key="k", model="m",
                                       image_bytes=photo)

    assert text == "описание фактами"
    assert threads and threads[0] != threading.current_thread().name, "PIL остался в потоке петли"
