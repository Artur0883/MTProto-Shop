from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from tariffs import TARIFFS


TRY_FREE_BUTTON = "🎁 Попробовать бесплатно"
BUY_BUTTON = "🚀 Получить доступ"
OLD_CART_BUY_BUTTON = "🛒 Купить доступ"
OLD_BUY_BUTTON = "💳 Купить доступ"
LEGACY_BUY_BUTTON = "🚀 Купить доступ"
CONNECT_BUTTON = "🔐 Подключиться"
MY_PROXY_BUTTON = "🔑 Мои ключи"
OLD_MY_PROXY_BUTTON = "🔑 Мой прокси"
OLD_MY_PROXIES_BUTTON = "🔑 Мои прокси"
MY_SUBSCRIPTION_BUTTON = "📅 Моя подписка"
OLD_MY_SUBSCRIPTION_BUTTON = "⏳ Моя подписка"
INSTRUCTION_BUTTON = "📖 Инструкция"
HELP_BUTTON = "🆘 Помощь"
MY_LINK_BUTTON = "🔐 Подключиться"
OLD_MY_LINK_BUTTON = "🔗 Моя ссылка"
DAYS_LEFT_BUTTON = "⏳ Срок доступа"
OLD_DAYS_LEFT_BUTTON = "📅 Осталось дней"
SUPPORT_BUTTON = "💬 Поддержка"
CLIENT_SUPPORT_BUTTON = "🆘 Поддержка"

USERS_BUTTON = "👥 Пользователи"
ISSUE_ACCESS_BUTTON = "➕ Выдать доступ"
EXTEND_ACCESS_BUTTON = "🔁 Продлить доступ"
ROTATE_ACCESS_BUTTON = "🔄 Обновить ключ"
DISABLE_ACCESS_BUTTON = "❌ Отключить доступ"
STATS_BUTTON = "📊 Статистика"
STATUS_BUTTON = "🛡 Состояние системы"
REBOOT_BUTTON = "🔁 Перезагрузка системы"
BACK_BUTTON = "⬅️ Назад"


def client_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUY_BUTTON)],
            [KeyboardButton(text=MY_PROXY_BUTTON), KeyboardButton(text=MY_SUBSCRIPTION_BUTTON)],
            [KeyboardButton(text=CLIENT_SUPPORT_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=USERS_BUTTON), KeyboardButton(text=STATS_BUTTON)],
            [KeyboardButton(text=ISSUE_ACCESS_BUTTON), KeyboardButton(text=EXTEND_ACCESS_BUTTON)],
            [KeyboardButton(text=ROTATE_ACCESS_BUTTON), KeyboardButton(text=DISABLE_ACCESS_BUTTON)],
            [KeyboardButton(text=STATUS_BUTTON), KeyboardButton(text=REBOOT_BUTTON)],
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


def manual_pay_keyboard(tariff_days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить через оператора",
                    callback_data=f"pay_request:{tariff_days}",
                )
            ],
            [InlineKeyboardButton(text=BACK_BUTTON, callback_data="client_back")],
        ]
    )


def admin_pay_request_keyboard(telegram_id: int, days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить — выдать доступ",
                    callback_data=f"admin_payapprove:{telegram_id}:{days}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"admin_payreject:{telegram_id}:{days}",
                )
            ],
        ]
    )


def connect_keyboard(link: str, *, show_alt_links: bool = True) -> InlineKeyboardMarkup:
    if link.startswith("tg://proxy?"):
        button_url = "https://t.me/proxy?" + link.split("?", 1)[1]
    else:
        button_url = link

    rows = [
        [InlineKeyboardButton(text="🔐 Подключиться к MTProto", url=button_url)],
    ]
    if show_alt_links:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🌐 Альтернативные ссылки",
                    callback_data="client_alt_links",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="📖 Инструкция", callback_data="client_instruction"),
            InlineKeyboardButton(
                text="🆘 Не подключается?",
                callback_data="client_troubleshoot",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alt_links_keyboard(links: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Build a keyboard listing alternate tg://proxy links via different TLS domains."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, (domain, link) in enumerate(links, 1):
        if link.startswith("tg://proxy?"):
            url = "https://t.me/proxy?" + link.split("?", 1)[1]
        else:
            url = link
        rows.append(
            [InlineKeyboardButton(text=f"🌐 Вариант {idx}: {domain}", url=url)]
        )
    rows.append([InlineKeyboardButton(text=BACK_BUTTON, callback_data="client_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def instructions_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Инструкция для моей платформы",
                    callback_data="client_instr_smart",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📱 iPhone (iOS 18+)",
                    callback_data="client_instr_iphone18",
                ),
                InlineKeyboardButton(
                    text="📱 iPhone (iOS 17-)",
                    callback_data="client_instr_iphone",
                ),
            ],
            [
                InlineKeyboardButton(text="📱 iPad", callback_data="client_instr_ipad"),
                InlineKeyboardButton(
                    text="🤖 Android / Huawei",
                    callback_data="client_instr_android",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🍎 macOS (native)",
                    callback_data="client_instr_macos",
                ),
                InlineKeyboardButton(
                    text="💻 Desktop (Win/Linux)",
                    callback_data="client_instr_desktop",
                ),
            ],
            [
                InlineKeyboardButton(text="📱 Telegram X", callback_data="client_instr_x"),
                InlineKeyboardButton(text="🌐 Telegram Web", callback_data="client_instr_web"),
            ],
            [InlineKeyboardButton(text=BACK_BUTTON, callback_data="client_back")],
        ]
    )


def help_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Написать в поддержку",
                    callback_data="client_support_inline",
                )
            ],
            [InlineKeyboardButton(text=BACK_BUTTON, callback_data="client_back")],
        ]
    )


def my_subscription_keyboard(has_active: bool) -> InlineKeyboardMarkup:
    if not has_active:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=TRY_FREE_BUTTON,
                        callback_data="client_try_free",
                    ),
                    InlineKeyboardButton(text=BUY_BUTTON, callback_data="client_buy"),
                ],
                [InlineKeyboardButton(text=BACK_BUTTON, callback_data="client_back")],
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=CONNECT_BUTTON, callback_data="client_connect"),
                InlineKeyboardButton(text="🔁 Продлить", callback_data="client_buy"),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить ключ",
                    callback_data="client_rotate_key",
                )
            ],
            [InlineKeyboardButton(text=BACK_BUTTON, callback_data="client_back")],
        ]
    )


def try_or_buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=TRY_FREE_BUTTON,
                    callback_data="client_try_free",
                ),
                InlineKeyboardButton(text=BUY_BUTTON, callback_data="client_buy"),
            ]
        ]
    )


def support_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Открыть чат поддержки", url=url)]
        ]
    )


def user_card_keyboard(telegram_id: int, has_active: bool) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="🔁 Продлить",
                callback_data=f"admin_card_extend:{telegram_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 Обновить ключ",
                callback_data=f"admin_card_rotate:{telegram_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Отключить и удалить ключ",
                callback_data=f"admin_card_disable:{telegram_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🗑 Удалить полностью",
                callback_data=f"admin_card_delete:{telegram_id}",
            )
        ],
    ]

    buttons.append(
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_users_back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_delete_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Да, удалить навсегда",
                    callback_data=f"admin_card_delete_yes:{telegram_id}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin_user:{telegram_id}")],
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
