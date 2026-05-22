from datetime import UTC
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from config import get_settings
from database import (
    from_db_datetime,
    get_active_subscription_by_telegram_id,
    upsert_user,
    utc_now,
)
from keyboards import (
    BUY_BUTTON,
    DAYS_LEFT_BUTTON,
    MY_LINK_BUTTON,
    SUPPORT_BUTTON,
    client_menu,
)
from proxy_manager import build_proxy_link
from tariffs import format_tariffs


router = Router()


def format_datetime(value: str) -> str:
    return from_db_datetime(value).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def days_left(expires_at: str) -> int:
    delta = from_db_datetime(expires_at) - utc_now()
    if delta.total_seconds() <= 0:
        return 0
    return max(1, delta.days + (1 if delta.seconds else 0))


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
    settings = get_settings()
    await message.answer(
        "Доступные тарифы:\n"
        f"{format_tariffs()}\n\n"
        "Автоматической оплаты пока нет. После оплаты администратор вручную "
        "выдаст доступ и бот пришлёт личную ссылку.\n\n"
        f"Поддержка: {escape(settings.support_contact)}",
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
