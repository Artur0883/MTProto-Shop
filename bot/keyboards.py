from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from tariffs import TARIFFS


BUY_BUTTON = "🚀 Купить доступ"
MY_LINK_BUTTON = "🔐 Подключиться"
OLD_MY_LINK_BUTTON = "🔗 Моя ссылка"
DAYS_LEFT_BUTTON = "⏳ Срок доступа"
OLD_DAYS_LEFT_BUTTON = "📅 Осталось дней"
SUPPORT_BUTTON = "💬 Поддержка"

USERS_BUTTON = "👥 Пользователи"
ISSUE_ACCESS_BUTTON = "➕ Выдать доступ"
EXTEND_ACCESS_BUTTON = "🔁 Продлить доступ"
ROTATE_ACCESS_BUTTON = "🔄 Обновить ключ"
DISABLE_ACCESS_BUTTON = "❌ Отключить доступ"
STATS_BUTTON = "📊 Статистика"
BACK_BUTTON = "⬅️ Назад"


def client_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUY_BUTTON)],
            [KeyboardButton(text=MY_LINK_BUTTON), KeyboardButton(text=DAYS_LEFT_BUTTON)],
            [KeyboardButton(text=SUPPORT_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=USERS_BUTTON)],
            [KeyboardButton(text=ISSUE_ACCESS_BUTTON), KeyboardButton(text=EXTEND_ACCESS_BUTTON)],
            [KeyboardButton(text=ROTATE_ACCESS_BUTTON), KeyboardButton(text=DISABLE_ACCESS_BUTTON)],
            [KeyboardButton(text=STATS_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Админ-меню",
    )


def client_tariff_keyboard() -> InlineKeyboardMarkup:
    buttons = []

    for tariff in TARIFFS.values():
        callback_data = (
            f"client_tariff:{tariff.days}"
            if tariff.enabled
            else f"client_tariff_disabled:{tariff.days}"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=tariff.title,
                    callback_data=callback_data,
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=BACK_BUTTON, callback_data="client_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def connect_keyboard(link: str) -> InlineKeyboardMarkup:
    if link.startswith("tg://proxy?"):
        button_url = "https://t.me/proxy?" + link.split("?", 1)[1]
    else:
        button_url = link

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Подключиться к MTProto", url=button_url)],
            [InlineKeyboardButton(text="💬 Нужна помощь", callback_data="client_support_inline")],
        ]
    )


def support_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Открыть чат поддержки", url=url)]
        ]
    )


def tariff_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tariff.title,
                    callback_data=f"admin_tariff:{action}:{tariff.days}",
                )
            ]
            for tariff in TARIFFS.values()
            if tariff.enabled
        ]
    )
