"""Контракты ответов агента как Pydantic-схемы.

Зачем схемы, если разбор уже есть. До разбор ответа писателя объявлений держался на
делении текста по меткам и эвристике «короче 40 знаков — заглушка». Это ломается ровно
там, где модель отдаёт шаблон, и — что хуже — отбрасывает короткое, но осмысленное
объявление. Схема заменяет догадку проверкой: у ответа есть форма, и её можно проверить
до того, как текст уйдёт клиенту.

**Три уровня контракта, и провайдер тут ни при чём.** Формат по-прежнему задаётся
инструкцией ревизии агента, потому что агент вызывается через канал, а канальный путь
только публикует событие (`{"project","bot","user","text"}`) и не принимает параметров
ответа — `response_format` здесь недостижим (см. заметку в плане, пункт). Поэтому
контракт держится на трёх уровнях:

  1. **инструкция** — требование формата в промпте (как было);
  2. **валидация схемой в мосте** — этот модуль: ответ разбирается и проверяется;
  3. **ретрай** — невалидный ответ отбрасывается, ход повторяется (в `realty_agent/listing.py`).

Схемы экспортируются (`export_schemas()`), чтобы документация промпта и валидация не
разъезжались: описание формата берётся из той же схемы, что и проверка.

Чего здесь намеренно НЕТ: ограничения длины как признака мусора. Контракт задаёт
верхнюю границу («до 400 / до 1200 знаков»), а не нижнюю: «Светлая двушка у метро» —
валидное короткое объявление, а не мусор.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, ValidationError, model_validator

SHORT_MARK = "КОРОТКОЕ:"
LONG_MARK = "ДЛИННОЕ:"

SHORT_MAX = 400
LONG_MAX = 1200

# Заглушка вместо текста: модель вернула шаблон, а не объявление.
PLACEHOLDER_RE = re.compile(r'^[<\[{(«"\']*(текст|text|кратко|…|\.\.\.)[>\]})»"\']*$', re.IGNORECASE)
# Незаполненный шаблон внутри текста.
INNER_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|<текст>|<text>|TODO|XXX", re.IGNORECASE)
# Служебный разделитель: его присутствие в объявлении ломает разбор блоков ниже по потоку.
SERVICE_PIPE = "|"


class ListingContractError(ValueError):
    """Ответ агента не соответствует контракту объявления."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


class ListingReply(BaseModel):
    """Контракт ответа писателя объявлений: две версии и ничего лишнего."""

    short: str = Field(description=f"Короткое объявление, до {SHORT_MAX} знаков")
    long: str = Field(description=f"Длинное объявление, до {LONG_MAX} знаков")

    @model_validator(mode="after")
    def _check_shape(self) -> ListingReply:
        violations: list[str] = []

        if not self.short.strip() and not self.long.strip():
            violations.append("нет ни одной версии объявления")

        for name, value in (("короткое", self.short), ("длинное", self.long)):
            if not value.strip():
                violations.append(f"{name} объявление пустое")
                continue
            if PLACEHOLDER_RE.match(value.strip()):
                violations.append(f"{name} объявление — заглушка вместо текста")
            if INNER_PLACEHOLDER_RE.search(value):
                violations.append(f"{name} объявление содержит незаполненный шаблон")
            if SERVICE_PIPE in value:
                violations.append(f"{name} объявление содержит служебный разделитель «|»")

        if self.short.strip() and self.long.strip():
            if self.short.strip() == self.long.strip():
                violations.append("обе версии совпали текстуально")
            elif len(self.long.strip()) < len(self.short.strip()):
                violations.append("длинная версия короче короткой")

        if violations:
            raise ListingContractError(violations)
        return self

    @classmethod
    def parse(cls, raw: str) -> ListingReply:
        """Разобрать сырой ответ агента по меткам контракта и проверить схемой.

        Бросает `ListingContractError`, если форма нарушена, — вызывающий код решает,
        повторять ход или отдавать ошибку.
        """
        text = raw or ""
        if SHORT_MARK not in text and LONG_MARK not in text:
            raise ListingContractError(["в ответе нет метки КОРОТКОЕ: или ДЛИННОЕ:"])

        short, long = "", ""
        if SHORT_MARK in text:
            after = text.split(SHORT_MARK, 1)[1]
            short = after.split(LONG_MARK, 1)[0].strip()
            if LONG_MARK in after:
                long = after.split(LONG_MARK, 1)[1].strip()

        try:
            return cls(short=short, long=long)
        except (ValidationError, ListingContractError) as error:
            if isinstance(error, ListingContractError):
                raise
            raise ListingContractError([f"схема не приняла ответ: {error}"]) from error


class BookingContract(BaseModel):
    """Контракт BOOKING-блока: то, что мост принимает от агента как заявку.

    Модель описательная: строгий разбор и валидацию делает существующий
    `realty_agent/booking/parser.py` (его не переписывает, а ставит рядом). Схема нужна,
    чтобы описание формата в промпте бралось из одного места с проверкой.
    """

    listing_id: str = Field(description="идентификатор объекта")
    slot: str = Field(description="дата и время просмотра, YYYY-MM-DDTHH:MM")
    name: str = Field(description="имя клиента")
    phone: str = Field(description="телефон: +7XXXXXXXXXX или маска")


def export_schemas() -> dict[str, dict]:
    """JSON-схемы контрактов — для документации промпта и для проверок."""
    return {
        "listing_reply": ListingReply.model_json_schema(),
        "booking": BookingContract.model_json_schema(),
    }


# Порядок контракта в виде текста — ровно то, что уходит в инструкцию ревизии агента.
# Держится рядом со схемой, чтобы требование формата и его проверка не разъезжались.
LISTING_FORMAT_INSTRUCTION = (
    f"Формат ответа строго:\n{SHORT_MARK}\n<текст>\n\n{LONG_MARK}\n<текст>"
)
