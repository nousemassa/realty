"""F2 vision: фото → описание (bridge-side).

Почему в bridge, а не в агенте (план v14): песочница агента не должна получать пиксели;
bridge держит ключ провайдера, сам вызывает vision-модель и передаёт агенту ТЕКСТ-описание.
OpenAI-совместимый вызов (RouterAI): POST {base}/chat/completions.
"""
from __future__ import annotations

import asyncio
import base64
import io
import threading

VISION_PROMPT = (
    "Опиши фотографию квартиры фактами, без оценок и без выдумок. "
    "Перечисли только то, что видно: тип помещения, планировку (если читается), состояние отделки, "
    "мебель/технику, окна и свет, санузел, видимые размеры/подписи на фото. "
    "Если чего-то не видно — не додумывай. Ответ — 5–10 коротких пунктов на русском."
)

#: Сколько пикселей готовы декодировать. Вход ограничен БАЙТАМИ (10 МБ в ручке), а байты о пикселях не
#: говорят: 10 МБ JPEG — это легко больше 25 Мп. `convert("RGB")` держит полный кадр (3 байта на
#: пиксель) плюс копию, а мост живёт в `mem_limit: 512m` — поэтому отказ по ПИКСЕЛЯМ, а не по байтам.
MAX_PIXELS = 30_000_000

#: Замок на время CPU-работы. PIL отпускает GIL не везде (декод/энкод — да, `convert` — не всегда),
#: поэтому два одновременных фото могли бы держать по полному кадру: с `to_thread` это уже не «задержка
#: цикла», а пик памяти. `threading.Lock`, а не `asyncio.Semaphore`: замок не привязан к петле и не
#: ломает тесты, где каждая проверка идёт своим `asyncio.run`.
_DECODE_LOCK = threading.Lock()


class ImageTooLarge(ValueError):
    """Фото слишком большое по ПИКСЕЛЯМ. Домен: ручка переводит это в 413, а не в 500.

    ❗ Намеренно не `Image.DecompressionBombError`: то же условие через `MAX_IMAGE_PIXELS` PIL бросает
    своё исключение ИЗ `Image.open`, и ручка не смогла бы отличить «большое фото» от поломки.
    """

    def __init__(self, *, pixels: int, max_pixels: int) -> None:
        super().__init__(f"фото {pixels} пикселей — больше допустимых {max_pixels}")
        self.pixels = pixels
        self.max_pixels = max_pixels


def to_jpeg(image_bytes: bytes, *, max_side: int = 1600, quality: int = 85) -> bytes:
    """HEIC/PNG/… → JPEG (IMAGE-нимб платформы: gif/jpeg/png/webp; HEIC клиенты присылают).

    Синхронная и CPU-ёмкая: из асинхронного кода вызывать только через `asyncio.to_thread`.
    """
    from PIL import Image  # локальный импорт: не нужен, если vision не используется

    try:  # HEIC/HEIF требует регистрации opener-а
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    with _DECODE_LOCK, Image.open(io.BytesIO(image_bytes)) as img:
        # Отсев ДО конверсии: `Image.open` читает только заголовки, поэтому размер известен дёшево.
        pixels = img.size[0] * img.size[1]
        if pixels > MAX_PIXELS:
            raise ImageTooLarge(pixels=pixels, max_pixels=MAX_PIXELS)
        # `draft` уменьшает кадр ПРИ ДЕКОДИРОВАНИИ, но делает это только JPEG (для HEIC/PNG — тихий
        # no-op), поэтому на него не полагаемся: отсев по пикселям выше — вот защита.
        img.draft("RGB", (max_side, max_side))
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        # `convert` после уменьшения: полная RGB-копия кадра стоит 3 байта на пиксель.
        img.convert("RGB").save(out, format="JPEG", quality=quality)
        return out.getvalue()


async def describe_photo(
    *,
    base_url: str,
    api_key: str,
    model: str,
    image_bytes: bytes,
    extra_hint: str = "",
    timeout_s: float = 90.0,
) -> str:
    """Возвращает текстовое описание фото (для последующей генерации объявления агентом)."""
    import httpx

    # ❗ PIL + base64 — синхронная работа, и до она исполнялась прямо в событийном цикле моста:
    # на телефонном кадре это сотни миллисекунд без единого `await`, то есть заморозка приёма апдейтов
    # и `/health`. Поток берём через `to_thread` (GIL отпускается в декодере/энкодере), а пик памяти
    # держит замок внутри `to_jpeg`.
    jpeg = await asyncio.to_thread(to_jpeg, image_bytes)
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT + (f"\n\nДополнительно от менеджера: {extra_hint}" if extra_hint else "")},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 400,
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(
            base_url.rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
    return (body["choices"][0]["message"]["content"] or "").strip()
