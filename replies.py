"""
Тексты бота зеркала A→B (только админка и подсказки).
"""

from __future__ import annotations

from typing import Optional

BTN_BACK = "Назад"
BTN_CANCEL = "Отмена"
MSG_PROMPT_CANCELLED = "Ввод отменён."

START_HINT = (
    "Бот пересылает посты из группы A в группу B и подменяет заданные фрагменты текста.\n"
    "Настройка — команда /admin (только для администраторов из .env)."
)

MASTER_ONLY_ADMIN_CMD = "Команда /admin только для администраторов из .env."

WEBHOOK_GET_DETAIL = (
    "Webhook MAX: события приходят POST с телом Update. "
    "Бот работает в чатах MAX, не в браузере."
)

ADMIN_MENU_TITLE = "Админка: зеркало A → B"

ADMIN_STATUS_HEADER = "Текущие настройки"
ADMIN_SOURCE_LINE = "Группа A (источник): {label}"
ADMIN_TARGET_LINE = "Группа B (приёмник): {label}"
ADMIN_REPLACEMENTS_COUNT = "Правил подмены: {n}"
ADMIN_NOT_SET = "не задано"

BTN_SET_SOURCE = "Группа A"
BTN_SET_TARGET = "Группа B"
BTN_REPLACEMENTS = "Подмены"
BTN_ADD_REPLACEMENT = "Добавить подмену"

PROMPT_SOURCE_CHAT = (
    "Отправьте chat_id группы A или ссылку-приглашение.\n"
    "Бот должен состоять в этой группе."
)
PROMPT_TARGET_CHAT = (
    "Отправьте chat_id группы B или ссылку-приглашение.\n"
    "Бот должен иметь право писать в этой группе."
)
PROMPT_REPLACE_SEARCH = "Введите текст, который нужно искать (например vk.com):"
PROMPT_REPLACE_REPLACE = "Введите текст, на который заменить (например ya.ru):"

SOURCE_SAVED = "Группа A сохранена: {cid}"
TARGET_SAVED = "Группа B сохранена: {cid}"
REPLACEMENT_ADDED = "Подмена добавлена: «{search}» → «{replace}»"
REPLACEMENT_REMOVED = "Подмена удалена."
REPLACEMENT_TOGGLED = "Подмена {state}."

REPLACEMENTS_HEADER = "Правила подмены (сверху вниз по порядку):"
REPLACEMENTS_EMPTY = "Правил пока нет."
REPLACEMENT_LINE = "{n}. [{state}] «{search}» → «{replace}»"

EMPTY_INPUT = "Пустой ввод."
CHAT_NOT_IN_BOT_LIST = (
    "Чат не найден среди чатов бота. Добавьте бота в группу по ссылке, затем повторите."
)


def chat_not_found_by_id(chat_id: int) -> str:
    return f"Чат с id={chat_id} не найден или бот не состоит в нём."


def chat_list_fetch_error(exc: str) -> str:
    return f"Не удалось получить список чатов: {exc}"


def chat_label(chat_id: Optional[int], title: Optional[str] = None) -> str:
    if chat_id is None:
        return ADMIN_NOT_SET
    t = (title or "").strip()
    if t:
        return f"{t} ({chat_id})"
    return str(chat_id)


def replacement_state_word(enabled: bool) -> str:
    return "включена" if enabled else "выключена"
