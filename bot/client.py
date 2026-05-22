from datetime import UTC, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message, User

from config import get_settings
from database import (
    ACTIVE_STATUS,
    create_subscription,
    extend_subscription,
    from_db_datetime,
    get_active_subscription_by_telegram_id,
    get_latest_subscription_by_telegram_id,
    upsert_user,
    utc_now,
)
from keyboards import (
    BUY_BUTTON,
    DAYS_LEFT_BUTTON,
    MY_LINK_BUTTON,
    OLD_DAYS_LEFT_BUTTON,
    OLD_MY_LINK_BUTTON,
    SUPPORT_BUTTON,
    client_menu,
    client_tariff_keyboard,
    connect_keyboard,
    support_keyboard,
)
from proxy_manager import build_proxy_link, create_secret, list_clients
from tariffs import get_tariff


router = Router()


def format_datetime(value: str) -> str:
    return from_db_datetime(value).astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def days_left(expires_at: str) -> int:
    delta = from_db_datetime(expires_at) - utc_now()
    if delta.total_seconds() <= 0:
        return 0
    return max(1, delta.days + (1 if delta.seconds else 0))


def client_id_for(telegram_id: int) -> str:
    return f"tg_{telegram_id}"


def ensure_secret(client_id: str, preferred_secret: str | None = None) -> str:
    users = list_clients()
    if client_id in users:
        return users[client_id]
    return create_secret(client_id, preferred_secret)


def support_url(contact: str) -> str:
    contact = contact.strip()

    if contact.startswith("@"):
        return "https://t.me/" + contact[1:]
    if contact.startswith("https://t.me/"):
        return contact
    if contact.startswith("http://t.me/"):
        return "https://t.me/" + contact.split("t.me/", 1)[1]
    if contact.startswith("t.me/"):
        return "https://" + contact
    return ""


async def send_support_request(message: Message, user_override: User | None = None) -> None:
    settings = get_settings()
    user = user_override or message.from_user
    contact = settings.support_contact.strip()
    url = support_url(contact)

    if url:
        await message.answer(
            "<b>💬 Поддержка</b>\n\n"
            "Нажмите кнопку ниже, чтобы открыть чат с поддержкой.",
            reply_markup=support_keyboard(url),
        )
        return

    if settings.admin_id and user:
        username = f"@{user.username}" if user.username else "не указан"
        full_name = user.full_name or "не указано"

        await message.bot.send_message(
            settings.admin_id,
            "<b>💬 Новое обращение в поддержку</b>\n\n"
            f"👤 Клиент: {escape(full_name)}\n"
            f"🔗 Username: {escape(username)}\n"
            f"🆔 Telegram ID: <code>{user.id}</code>\n\n"
            "Клиент нажал кнопку поддержки.",
        )

        await message.answer(
            "<b>✅ Заявка отправлена</b>\n\n"
            "Администратор получил ваше обращение и скоро свяжется с вами.",
            reply_markup=client_menu(),
        )
        return

    await message.answer(
        "Поддержка временно не указана.",
        reply_markup=client_menu(),
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    if user is None:
        return

    await upsert_user(
        settings.database_path,
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )

    latest = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )
    full_name = escape(user.full_name)

    if (
        latest is not None
        and latest["status"] == ACTIVE_STATUS
        and from_db_datetime(latest["expires_at"]) > utc_now()
    ):
        text = (
            f"<b>👋 С возвращением, {full_name}!</b>\n\n"
            "✅ Доступ активен\n"
            f"⏳ Осталось дней: {days_left(latest['expires_at'])} "
            f"(до {format_datetime(latest['expires_at'])})\n\n"
            "Выберите действие:"
        )
    elif latest is not None:
        text = (
            f"<b>👋 С возвращением, {full_name}.</b>\n\n"
            "🔒 Ваш доступ неактивен.\n"
            "🚀 Нажмите Купить доступ, чтобы продлить.\n\n"
            "Выберите действие:"
        )
    else:
        text = (
            "<b>👋 Добро пожаловать в MTProto Shop</b>\n\n"
            "🔐 Личный MTProto-доступ через Telegram-прокси.\n"
            "🎁 1 день бесплатно — попробуйте сейчас.\n\n"
            "Доступные тарифы:\n"
            "• 🎁 1 день — бесплатно\n"
            "• 🗓 1 месяц — 50 ₽\n"
            "• 🗓 3/6/12 месяцев — скоро\n\n"
            "Нажмите 🚀 Купить доступ, чтобы начать."
        )

    await message.answer(text, reply_markup=client_menu())


@router.message(F.text == BUY_BUTTON)
async def buy_access(message: Message) -> None:
    await message.answer(
        "<b>🚀 Покупка доступа</b>\n\n"
        "Выберите тариф:\n\n"
        "🎁 1 день — бесплатно (пробный)\n"
        "🗓 1 месяц — 50 ₽\n"
        "🗓 3/6/12 месяцев — в разработке\n\n"
        "Цены указаны в рублях.",
        reply_markup=client_tariff_keyboard(),
    )


@router.callback_query(F.data == "client_back")
async def client_back(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await message.answer("🏠 Главное меню", reply_markup=client_menu())


@router.callback_query(F.data.startswith("client_tariff_disabled:"))
async def disabled_client_tariff(callback: CallbackQuery) -> None:
    await callback.answer(
        "Этот тариф пока в разработке. Сейчас доступны пробный период и 1 месяц.",
        show_alert=True,
    )


@router.callback_query(F.data == "client_support_inline")
async def support_inline(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await send_support_request(message, user_override=callback.from_user)


@router.callback_query(F.data.startswith("client_tariff:"))
async def choose_client_tariff(callback: CallbackQuery) -> None:
    settings = get_settings()
    user = callback.from_user
    message = callback.message

    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 2:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    try:
        tariff = get_tariff(int(parts[1]))
    except (TypeError, ValueError):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await upsert_user(
        settings.database_path,
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )

    if not settings.test_auto_issue_access:
        await callback.answer()
        await message.answer(
            "💳 Оплата пока подключается\n\n"
            "Нажмите кнопку поддержки, чтобы получить доступ через администратора.",
            reply_markup=client_menu(),
        )
        return

    latest = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )

    previous_secret = latest["secret"] if latest and latest["secret"] else None
    secret = ensure_secret(client_id_for(user.id), previous_secret)

    now = utc_now()

    if latest and latest["status"] == ACTIVE_STATUS:
        base = max(from_db_datetime(latest["expires_at"]), now)
        subscription = await extend_subscription(
            settings.database_path,
            user.id,
            secret,
            tariff.days,
            now,
            base + timedelta(days=tariff.days),
        )
    elif latest:
        subscription = await extend_subscription(
            settings.database_path,
            user.id,
            secret,
            tariff.days,
            now,
            now + timedelta(days=tariff.days),
        )
    else:
        subscription = await create_subscription(
            settings.database_path,
            user.id,
            secret,
            tariff.days,
            now,
            now + timedelta(days=tariff.days),
        )

    link = build_proxy_link(
        settings.server_host,
        settings.proxy_port,
        subscription["secret"],
    )

    await callback.answer("Доступ выдан")
    await message.answer(
        "<b>🎉 Доступ активирован!</b>\n\n"
        f"📦 Тариф: {escape(tariff.title)}\n"
        f"⏳ Действует до: {format_datetime(subscription['expires_at'])}\n\n"
        "Нажмите кнопку ниже, чтобы подключиться.",
        reply_markup=connect_keyboard(link),
    )


@router.message((F.text == MY_LINK_BUTTON) | (F.text == OLD_MY_LINK_BUTTON))
async def my_link(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    if user is None:
        return

    subscription = await get_active_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )

    if subscription is None or not subscription.get("secret"):
        await message.answer(
            "<b>🔒 Доступ пока не активен</b>\n\n"
            "🎁 Попробуйте 1 день бесплатно — нажмите 🚀 Купить доступ.",
            reply_markup=client_menu(),
        )
        return

    link = build_proxy_link(
        settings.server_host,
        settings.proxy_port,
        subscription["secret"],
    )

    await message.answer(
        "<b>🔐 Ваш доступ готов</b>\n\n"
        f"⏳ Действует до: {format_datetime(subscription['expires_at'])}\n"
        f"✅ Осталось дней: {days_left(subscription['expires_at'])}\n\n"
        "Нажмите кнопку ниже, чтобы подключиться.",
        reply_markup=connect_keyboard(link),
    )


@router.message((F.text == DAYS_LEFT_BUTTON) | (F.text == OLD_DAYS_LEFT_BUTTON))
async def show_days_left(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    if user is None:
        return

    subscription = await get_active_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )

    if subscription is None:
        await message.answer(
            "🔒 Доступ не активен\n\n"
            "Чтобы подключиться, нажмите «🚀 Купить доступ».",
            reply_markup=client_menu(),
        )
        return

    tariff_days = subscription["tariff_days"]
    dl = days_left(subscription["expires_at"])
    used = max(0, tariff_days - dl)
    filled = min(10, used * 10 // max(1, tariff_days))
    bar = "█" * filled + "░" * (10 - filled)
    percent = filled * 10

    await message.answer(
        "<b>⏳ Срок доступа</b>\n\n"
        f"📅 Истекает: {format_datetime(subscription['expires_at'])}\n"
        f"✅ Осталось дней: {dl}\n\n"
        f"[<code>{bar}</code>] {percent}% использовано\n\n"
        "Хотите продлить заранее? Нажмите 🚀 Купить доступ.",
        reply_markup=client_menu(),
    )


@router.message(F.text == SUPPORT_BUTTON)
async def support(message: Message) -> None:
    await send_support_request(message)
