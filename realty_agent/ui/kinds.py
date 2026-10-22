"""Виды кнопок продукта и политика их рендера для Telegram.

`kind` кодируется в `callback_data` (см. `agent_core.ui.tokens`) и диспатчится при нажатии. Ядро
видов не знает: рендер получает политику — какие виды подписываются, какие имеют фиксированный
callback, какие требуют reply-раскладки.
"""
from __future__ import annotations

from agent_core.tg.render import ButtonPolicy

# Виды действий. `kind` кодируется в callback_data (см. `ui/tokens.py`) и диспатчится при нажатии.
KIND_LISTING = "l"      # выбор объекта (arg — индекс в предложенном списке)
KIND_DAY = "d"          # выбор дня (arg — индекс дня в предложенной сетке)
KIND_TIME = "t"         # выбор времени (arg — индекс слота в предложенном списке)
KIND_CONFIRM = "ok"     # «Подтвердить запись»
KIND_EDIT = "ed"        # «Изменить» (arg — что меняем: listing|slot|name|phone)
KIND_CANCEL = "cn"      # «Отменить»
KIND_BACK = "b"         # «‹ Все дни» — вернуть сетку дней
# «📱 Отправить мой номер» — REPLY-кнопка запроса телефона (Telethon: Button.request_phone).
# Inline-кнопкой номер запросить нельзя — это ограничение Telegram/MTProto.
KIND_PHONE_REQUEST = "ph"
# «✖ Сбросить фильтр» — inline, без состояния: снимает ограничение клиента (DD-8).
KIND_FILTER_RESET = "fx"
KIND_MENU = "m"         # reply-пункт меню (arg — код пункта, см. `ui/menu.py`)
# Овнер-меню управления менеджерамиb): тап меняет состояние ⇒ кнопки ПОДПИСАНЫ.
KIND_OWNER = "own"      # arg — действие: add|list|del
# Самозаявка «👤 Я менеджер»c): подписаны UI-секретом, arg — id заявки.
KIND_APPLY = "ap"       # arg — действие заявителя: check (перепроверить профиль)
KIND_APPLY_PICK = "apk"  # arg — request_id: открыть карточку заявки (решение — на карточке)
KIND_APPLY_OK = "ay"    # arg — request_id: утвердить
KIND_APPLY_NO = "an"    # arg — request_id: отклонить
KIND_OWNER_PICK = "opk"  # arg — chat_id менеджера (выбор для удаления)
KIND_OWNER_OFF = "oof"   # arg — chat_id менеджера (подтверждённое снятие)
KIND_CONSENT = "consent"  # «Согласен(а)» — без подписи, как было до кнопочного UI
KIND_POLICY = "policy"    # «📄 Политика» — тоже без подписи: только статичный текст, без побочных действий

#: Политика рендера кнопок продукта.
#: * согласие и политика — без подписи: побочных действий нет, только запись согласия и статичный текст;
#: * запрос телефона — только reply-кнопка (Telegram отдаёт контакт лишь из неё);
#: * reply-раскладка допускает пункты меню и запрос телефона.
BUTTON_POLICY = ButtonPolicy(
    callbacks={KIND_CONSENT: "consent:yes", KIND_POLICY: "policy:show"},
    phone_kinds=frozenset({KIND_PHONE_REQUEST}),
    reply_kinds=frozenset({KIND_MENU, KIND_PHONE_REQUEST}),
)
