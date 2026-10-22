"""Разбор нажатий reply-меню: метки, динамический счётчик, обычный текст клиента."""
from __future__ import annotations

from realty_agent.ui import menu


def test_resolves_client_and_owner_labels():
    assert menu.resolve_menu("🏠 Объекты") == menu.MENU_OBJECTS
    assert menu.resolve_menu("📅 Записаться") == menu.MENU_BOOK
    assert menu.resolve_menu("📞 Менеджер") == menu.MENU_MANAGER
    assert menu.resolve_menu("📄 Политика") == menu.MENU_POLICY
    assert menu.resolve_menu("⏸ Пауза") == menu.MENU_OWNER_PAUSE
    assert menu.resolve_menu("▶️ Запуск") == menu.MENU_OWNER_RESUME
    assert menu.resolve_menu("📊 Отчёт") == menu.MENU_OWNER_REPORT
    assert menu.resolve_menu("ℹ️ Статус") == menu.MENU_OWNER_STATUS


def test_counter_in_label_is_normalized():
    for count in (0, 8, 12):
        assert menu.resolve_menu(f"📋 Заявки ({count})") == menu.MENU_OWNER_BOOKINGS
    assert menu.resolve_menu("📋 Заявки") == menu.MENU_OWNER_BOOKINGS


def test_whitespace_tolerated():
    assert menu.resolve_menu("  🏠 Объекты  ") == menu.MENU_OBJECTS


def test_plain_client_text_is_not_a_menu_tap():
    for text in ("хочу посмотреть квартиру", "1", "второй", "10:00", "Иван Петров", ""):
        assert menu.resolve_menu(text) is None


def test_label_helper_renders_counter():
    assert menu.label(menu.MENU_OWNER_BOOKINGS, 3) == "📋 Заявки (3)"
    assert menu.label(menu.MENU_OWNER_BOOKINGS) == "📋 Заявки"
