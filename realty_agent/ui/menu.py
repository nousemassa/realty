"""Reply-меню: коды пунктов и разбор нажатия.

Reply-кнопка приходит обычным текстом, поэтому её надо распознать ДО диалога и НЕ отдавать агенту
(иначе он начнёт «отвечать» на «📋 Заявки»). Счётчик в лейбле динамический («Заявки (3)») —
при разборе он нормализуется.
"""
from __future__ import annotations

import re

# Клиент
MENU_OBJECTS = "objects"
MENU_BOOK = "book"
MENU_MANAGER = "manager"
MENU_POLICY = "policy"
MENU_APPLY = "apply"      # «👤 Я менеджер» — самозаявка на роль менеджера ()
# Овнер
MENU_OWNER_BOOKINGS = "o_bookings"
MENU_OWNER_PAUSE = "o_pause"
MENU_OWNER_MANAGERS = "o_managers"
MENU_OWNER_RESUME = "o_resume"
MENU_OWNER_REPORT = "o_report"
MENU_OWNER_STATUS = "o_status"
MENU_OWNER_REQUESTS = "o_requests"   # «📥 Заявки в менеджеры (N)» ()
MENU_OWNER_VIEWS = "o_views"         # «👁 Просмотры (N)» — подтверждённые заявки (DD-6)

LABELS: dict[str, str] = {
    MENU_OBJECTS: "🏠 Объекты",
    MENU_BOOK: "📅 Записаться",
    MENU_MANAGER: "📞 Запрос звонка от менеджера",
    MENU_POLICY: "📄 Политика",
    MENU_APPLY: "👤 Я менеджер",
    MENU_OWNER_BOOKINGS: "📋 Заявки",
    MENU_OWNER_PAUSE: "⏸ Пауза",
    MENU_OWNER_MANAGERS: "👥 Менеджеры",
    MENU_OWNER_RESUME: "▶️ Запуск",
    MENU_OWNER_REPORT: "📊 Отчёт",
    MENU_OWNER_STATUS: "ℹ️ Статус",
    MENU_OWNER_REQUESTS: "📥 Заявки в менеджеры",
    MENU_OWNER_VIEWS: "👁 Просмотры",
}

# Метка «повтор» — тот же пункт каталога под другим именем ().
REPEAT_LABEL = "🔄 Записаться ещё"

# Дополнительные подписи (кнопка называется иначе, но ведёт к тому же коду). Только РЕЗОЛВ:
# перехватывать алиасы по «in ALIASES» нельзя — иначе любой из них уходил бы в путь повтора ().
ALIASES: dict[str, str] = {
    REPEAT_LABEL: MENU_OBJECTS,
    "📞 Менеджер": MENU_MANAGER,             #: клавиатуры, отправленные ДО переименования
}

COUNTER_RE = re.compile(r"\s*\(\s*\d+\s*\)\s*$")


def label(code: str, count: int | None = None) -> str:
    """«📋 Заявки» → «📋 Заявки (3)» — счётчик как в образцах интерфейса."""
    base = LABELS.get(code, code)
    return f"{base} ({count})" if count is not None else base


def normalize(text: str) -> str:
    return COUNTER_RE.sub("", (text or "").strip())


def resolve_menu(text: str) -> str | None:
    """Текст нажатой reply-кнопки → код пункта, иначе None (обычный текст клиента)."""
    if not text:
        return None
    normalized = normalize(text)
    for code, base in LABELS.items():
        if normalized == base:
            return code
    for alias, code in ALIASES.items():
        if normalized == alias:
            return code
    return None
