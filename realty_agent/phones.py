"""ТЕЛЕФОНЫ клиента и хеш идентификатора: маскирование и детектор утечки.

**Почему имя `phones`, а не `pii`.** Модуль назывался `pii.py` — «персональные данные», — а
закрывает ровно ОДИН их вид: телефон (`canonical_phone`, `mask_phone`, `mask_phones`,
`contains_unmasked_phone`) плюс `chat_hash` для трейсов. Имя обещало больше, чем делает, и читатель делал
вывод «ПДн закрыты» — а ФИО, адреса и согласия живут в других местах: согласие — в таблице `consents` и
гейте `_client_gate`, адреса объектов — в данных `listings`. Имя обязано говорить, ЧТО закрыто, иначе
молчаливая дыра выглядит как гарантия.

Правило плана: телефон клиента не уходит в LLM-контекст — перед отправкой агенту свободный текст
клиента прогоняется через `mask_phones` (см. `dialog.handle_message`); в логах/трейсах — только маска.
Маскирование показывается клиенту: «+7 912 •••-••-34».
"""
from __future__ import annotations

import re

# +7/8, 10–11 цифр с разделителями — детектор утечки (тесты/логи/трейсы)
PHONE_DETECT_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
# 10 цифр, начинающиеся с 9 (мобильный РФ) — частый промах основного детектора
PHONE_BARE_RE = re.compile(r"(?<!\d)9\d{9}(?!\d)")
PHONE_CLEAN_RE = re.compile(r"[^\d+]")
MASKED_LIKE_RE = re.compile(r"[•*]{2,}")


def canonical_phone(raw: str) -> str | None:
    """Нормализует в +7XXXXXXXXXX; None — если не похоже на телефон."""
    digits = PHONE_CLEAN_RE.sub("", raw or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "+7" + digits[1:]
    elif digits.startswith("7") and len(digits) == 11:
        digits = "+" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "+7" + digits[1:]
    if re.fullmatch(r"\+7\d{10}", digits):
        return digits
    return None


def mask_phone(raw: str) -> str:
    """Возвращает маску для показа клиенту: +7 912 •••-••-34 (или исходник, если не телефон)."""
    phone = canonical_phone(raw)
    if not phone:
        return raw
    return f"+7 {phone[2:5]} •••-••-{phone[-2:]}"


def contains_unmasked_phone(text: str) -> bool:
    """True, если в тексте есть телефон, который не является маской."""
    if not text:
        return False
    if PHONE_BARE_RE.search(text):
        return True
    for match in PHONE_DETECT_RE.finditer(text):
        fragment = match.group(0)
        # маска вида "+7 912 •••-••-34" не матчится детектором (нет 10 цифр) — сюда не попадёт
        if canonical_phone(fragment) and not MASKED_LIKE_RE.search(fragment):
            return True
    return False


def mask_phones(text: str) -> str:
    """Маскирует ВСЕ телефоны в тексте — для промпта агента и логов.

    Зачем: клиент может написать номер свободным текстом («мой номер +7 912…»), и без этой
    обработки телефон уходил бы в LLM открытым (нарушение инварианта «телефон уходит маской»).
    Идемпотентно: маски детектор не матчит.
    """
    if not text:
        return text

    def _mask(fragment: str) -> str:
        digits = PHONE_CLEAN_RE.sub("", fragment)
        if len(digits) == 10 and digits.startswith("9"):   # мобильный РФ без префикса
            digits = "+7" + digits
        canonical = canonical_phone(digits) or canonical_phone(fragment)
        return mask_phone(canonical) if canonical else fragment

    return PHONE_BARE_RE.sub(lambda m: _mask(m.group(0)),
                             PHONE_DETECT_RE.sub(lambda m: _mask(m.group(0)), text))
