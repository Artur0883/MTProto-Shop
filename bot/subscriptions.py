import asyncio
from datetime import timedelta

from aiogram import Bot

from config import get_settings
from database import (
    EXPIRED_STATUS,
    from_db_datetime,
    get_active_subscriptions_for_reminders,
    get_expired_active_subscriptions,
    mark_reminder_sent,
    mark_subscription_status,
    utc_now,
)
from logging_setup import get_logger
from proxy_manager import ClientNotFoundError, delete_secret


logger = get_logger(__name__)


CHECK_INTERVAL_SECONDS = 300


def client_id_for(telegram_id: int) -> str:
    return f"tg_{telegram_id}"


async def expire_subscriptions(bot: Bot) -> None:
    settings = get_settings()
    subscriptions = await get_expired_active_subscriptions(settings.database_path)

    for subscription in subscriptions:
        telegram_id = int(subscription["telegram_id"])
        client_id = client_id_for(telegram_id)
        try:
            await delete_secret(client_id)
        except ClientNotFoundError:
            logger.warning(
                "event=expire_secret_missing",
                telegram_id=telegram_id,
            )
        except Exception as exc:
            logger.exception(
                "event=expire_secret_failed",
                telegram_id=telegram_id,
                error=str(exc),
            )
            continue

        await mark_subscription_status(
            settings.database_path,
            subscription["id"],
            EXPIRED_STATUS,
        )

        try:
            await bot.send_message(
                telegram_id,
                "⏳ Срок подписки истёк, доступ отключён.\n\n"
                "🔁 Чтобы продлить — нажмите /start → 🚀 Получить доступ.",
            )
        except Exception as exc:
            logger.warning(
                "event=expire_notify_failed",
                telegram_id=telegram_id,
                error=str(exc),
            )


async def send_reminders(bot: Bot) -> None:
    settings = get_settings()
    now = utc_now()
    subscriptions = await get_active_subscriptions_for_reminders(settings.database_path)

    for subscription in subscriptions:
        expires_at = from_db_datetime(subscription["expires_at"])
        remaining = expires_at - now
        if remaining <= timedelta(0):
            continue

        telegram_id = int(subscription["telegram_id"])
        if (
            timedelta(days=2) < remaining <= timedelta(days=3)
            and not subscription["reminder_3d_sent"]
            and subscription["tariff_days"] > 1
        ):
            try:
                await bot.send_message(
                    telegram_id,
                    "Напоминание: до окончания доступа осталось около 3 дней.",
                )
            except Exception as exc:
                logger.warning(
                    "event=reminder_3d_failed",
                    telegram_id=telegram_id,
                    error=str(exc),
                )
            await mark_reminder_sent(settings.database_path, subscription["id"], 3)
        elif (
            remaining <= timedelta(days=1)
            and not subscription["reminder_1d_sent"]
            and subscription["tariff_days"] > 1
        ):
            try:
                await bot.send_message(
                    telegram_id,
                    "Напоминание: до окончания доступа осталось около 1 дня.",
                )
            except Exception as exc:
                logger.warning(
                    "event=reminder_1d_failed",
                    telegram_id=telegram_id,
                    error=str(exc),
                )
            await mark_reminder_sent(settings.database_path, subscription["id"], 1)


async def check_subscriptions_once(bot: Bot) -> None:
    await expire_subscriptions(bot)
    await send_reminders(bot)


async def subscription_worker(bot: Bot) -> None:
    while True:
        try:
            await check_subscriptions_once(bot)
        except Exception:
            logger.exception("event=subscription_worker_failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
