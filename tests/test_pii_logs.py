"""PII-автотест (обязательный DoD): телефон не появляется немаркированным в логах/наших строках."""
from __future__ import annotations

import logging

from realty_agent.phones import canonical_phone, contains_unmasked_phone, mask_phone

PHONE = "+7 912 345-67-89"
MASK = "+7 912 •••-••-89"


def test_canonical_phone_variants():
    assert canonical_phone("8 912 345 67 89") == "+79123456789"
    assert canonical_phone("+7(912)345-67-89") == "+79123456789"
    assert canonical_phone("79123456789") == "+79123456789"
    assert canonical_phone("123") is None


def test_mask_hides_middle_digits():
    assert mask_phone(PHONE) == MASK
    assert "345" not in mask_phone(PHONE)
    assert mask_phone("не телефон") == "не телефон"


def test_unmasked_phone_detected():
    assert contains_unmasked_phone(f"клиент оставил {PHONE}")
    assert contains_unmasked_phone("звоните 8-912-345-67-89")


def test_mask_not_flagged():
    assert not contains_unmasked_phone(f"Подтвердите: {MASK}")


def test_no_unmasked_phone_in_logs(caplog):
    """Негативный контур: если код залогирует телефон — тест падает."""
    with caplog.at_level(logging.INFO):
        # корректный код логирует только маску
        logging.getLogger("realty_agent").info("booking pending: %s", mask_phone(PHONE))
        leaked = [r.message for r in caplog.records if contains_unmasked_phone(str(r.message))]
        assert leaked == [], f"утечка телефона в лог: {leaked}"

    caplog.clear()
    with caplog.at_level(logging.INFO):
        logging.getLogger("realty_agent").info("booking pending: %s", PHONE)  # намеренная утечка
        leaked = [r.message for r in caplog.records if contains_unmasked_phone(str(r.message))]
        assert leaked, "детектор обязан поймать немаркированный телефон"
