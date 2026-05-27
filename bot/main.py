import asyncio
from datetime import UTC, datetime
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message, TelegramObject

from admin import router as admin_router
from client import router as client_router
from config import get_settings
from database import init_db
from logging_setup import configure_logging, get_logger
import runtime
from subscriptions import subscription_worker
from telemt_client import close_telemt, is_available
from tls_domains import get_picker


logger = get_logger(__name__)


class UserRateLimitMiddleware(BaseMiddleware):
    def __init__(self, min_interval_seconds: float = 0.8) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.last_event_at: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        previous = self.last_event_at.get(user.id, 0.0)
        if now - previous < self.min_interval_seconds:
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком много запросов. Повторите через секунду.")
            elif isinstance(event, Message):
                await event.answer("Слишком много запросов. Повторите через секунду.")
            return None

        self.last_event_at[user.id] = now
        if len(self.last_event_at) > 10000:
            self.last_event_at = {
                key: timestamp
                for key, timestamp in self.last_event_at.items()
                if now - timestamp < 300
            }
        return await handler(event, data)


async def wait_for_telemt(attempts: int = 30, delay: float = 2.0) -> None:
    """Wait for TeleMT to come up at startup. Does not block forever."""
    for attempt in range(attempts):
        if await is_available():
            logger.info("event=telemt_ready", attempts=attempt + 1)
            return
        await asyncio.sleep(delay)
    raise RuntimeError(
        f"TeleMT API still not responding after {attempts * delay:.0f}s"
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    if settings.admin_id is None:
        logger.warning("event=admin_id_unset")

    await init_db(settings.database_path)
    await wait_for_telemt()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    rate_limit = UserRateLimitMiddleware()
    dp.message.outer_middleware(rate_limit)
    dp.callback_query.outer_middleware(rate_limit)
    dp.include_router(admin_router)
    dp.include_router(client_router)

    worker_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None
    telemt_monitor_task: asyncio.Task | None = None
    picker = get_picker()

    async def bot_heartbeat_loop() -> None:
        beat_path = settings.database_path.parent / "heartbeats" / "bot.beat"
        beat_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                beat_path.touch()
            except Exception:
                logger.warning("event=bot_heartbeat_write_failed")
            await asyncio.sleep(30)

    async def telemt_monitor_loop() -> None:
        failed_since: float | None = None
        notified = False
        while True:
            available = await is_available()
            now = time.monotonic()
            if available:
                if notified and settings.admin_id is not None:
                    try:
                        await bot.send_message(
                            settings.admin_id,
                            "✅ TeleMT API снова доступен.",
                        )
                    except Exception:
                        logger.exception("event=telemt_recovery_notification_failed")
                    logger.info("event=telemt_api_recovered")
                failed_since = None
                notified = False
            else:
                if failed_since is None:
                    failed_since = now
                if (
                    not notified
                    and now - failed_since >= 30
                    and settings.admin_id is not None
                ):
                    try:
                        await bot.send_message(
                            settings.admin_id,
                            "❌ TeleMT API не отвечает более 30 секунд. "
                            "Проверьте VPS: mtp → 3 (статус) и mtp → 5 (логи TeleMT).",
                        )
                    except Exception:
                        logger.exception("event=telemt_outage_notification_failed")
                    logger.error("event=telemt_api_unavailable", duration_seconds=30)
                    notified = True
            await asyncio.sleep(10)

    async def on_startup() -> None:
        nonlocal heartbeat_task, telemt_monitor_task, worker_task
        runtime.STARTED_AT = datetime.now(UTC)
        worker_task = asyncio.create_task(subscription_worker(bot))
        heartbeat_task = asyncio.create_task(bot_heartbeat_loop())
        telemt_monitor_task = asyncio.create_task(telemt_monitor_loop())
        picker.start()
        # Probe immediately so the first user request already has rankings.
        asyncio.create_task(picker.probe_all())
        logger.info(
            "event=startup",
            telemt_api=settings.telemt_api_url,
            server=f"{settings.server_host}:{settings.proxy_port}",
            tls_domains=settings.fallback_tls_domains,
        )

    async def on_shutdown() -> None:
        for task in (worker_task, heartbeat_task, telemt_monitor_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("event=shutdown_task_error")
        await picker.stop()
        await close_telemt()
        await bot.session.close()
        logger.info("event=shutdown_complete")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
