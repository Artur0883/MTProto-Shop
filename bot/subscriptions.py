import asyncio
import logging
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
from proxy_manager import ClientNotFoundError, delete_secret


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
            delete_secret(client_id)
        except ClientNotFoundError as exc:
            logging.warning(
                "Expired secret for %s is already absent in proxy config: %s",
                telegram_id,
                exc,
            )
        except Exception as exc:
            logging.exception(
                "Failed to delete expired secret for %s. "
                "Subscription remains active for retry: %s",
                telegram_id,
                exc,
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
                "🔁 Чтобы продлить — нажмите /start → 💳 Купить доступ.",
            )
        except Exception as exc:
            logging.warning("Failed to notify expired user %s: %s", telegram_id, exc)


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
                logging.warning("Failed to send 3d reminder to %s: %s", telegram_id, exc)
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
                logging.warning("Failed to send 1d reminder to %s: %s", telegram_id, exc)
            await mark_reminder_sent(settings.database_path, subscription["id"], 1)


async def check_subscriptions_once(bot: Bot) -> None:
    await expire_subscriptions(bot)
    await send_reminders(bot)


async def subscription_worker(bot: Bot) -> None:
    while True:
        try:
            await check_subscriptions_once(bot)
        except Exception as exc:
            logging.exception("Subscription worker failed: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
