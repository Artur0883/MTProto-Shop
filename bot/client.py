from datetime import UTC, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

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
    SUPPORT_BUTTON,
    client_menu,
    client_tariff_keyboard,
)
from proxy_manager import build_proxy_link, create_secret, list_clients
from tariffs import get_tariff


router = Router()


def format_datetime(value: str) -> str:
    return from_db_datetime(value).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


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

    await message.answer(
        "Добро пожаловать в MTProto Shop!\n\n"
        "Здесь можно получить личный доступ к MTProto proxy. "
        "У каждого клиента свой secret, поэтому доступ можно отключать отдельно.",
        reply_markup=client_menu(),
    )


@router.message(F.text == BUY_BUTTON)
async def buy_access(message: Message) -> None:
    await message.answer(
        "Выберите срок доступа:",
        reply_markup=client_tariff_keyboard(),
    )


@router.callback_query(F.data == "client_back")
async def client_back(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await message.answer("Главное меню", reply_markup=client_menu())


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
            "Автоматическая оплата пока не подключена. "
            "Напишите администратору для оплаты.\n\n"
            f"Поддержка: {escape(settings.support_contact)}",
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
        f"Тариф: {escape(tariff.title)}\n"
        f"Доступ до: {format_datetime(subscription['expires_at'])}\n\n"
        "Ваша личная ссылка:\n"
        f"{escape(link)}\n\n"
        "Доступ может активироваться в течение 1 минуты. "
        "Если не подключилось — напишите в поддержку.",
        reply_markup=client_menu(),
    )


@router.message(F.text == MY_LINK_BUTTON)
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
            "Доступ пока не активен. Нажмите «🚀 Купить доступ» и свяжитесь с поддержкой.",
            reply_markup=client_menu(),
        )
        return

    link = build_proxy_link(
        settings.server_host,
        settings.proxy_port,
        subscription["secret"],
    )
    await message.answer(
        "Ваша личная ссылка для подключения:\n\n"
        f"{escape(link)}\n\n"
        "Не передавайте её другим людям: доступ привязан к вашему личному secret.",
        reply_markup=client_menu(),
    )


@router.message(F.text == DAYS_LEFT_BUTTON)
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
        await message.answer("Доступ не активен.", reply_markup=client_menu())
        return

    await message.answer(
        "Ваша подписка активна.\n\n"
        f"Дата окончания: {format_datetime(subscription['expires_at'])}\n"
        f"Осталось дней: {days_left(subscription['expires_at'])}",
        reply_markup=client_menu(),
    )


@router.message(F.text == SUPPORT_BUTTON)
async def support(message: Message) -> None:
    settings = get_settings()
    await message.answer(settings.support_contact, reply_markup=client_menu())
