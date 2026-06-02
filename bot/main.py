import asyncio
from datetime import UTC, datetime
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message, TelegramObject
import aiohttp

from admin import router as admin_router
from client import router as client_router
from config import get_settings
from database import init_db
from logging_setup import configure_logging, get_logger
import runtime
from subscriptions import subscription_worker
from nodes import get_nodes
from proxy_watchdog import proxy_watchdog_loop
from reconciler import reconciler_loop
from self_heal import self_heal_loop
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
    """Wait for TeleMT to come up at startup. Does not block forever.

    Logs `event=telemt_wait` every 5 attempts with elapsed seconds so that
    `mtp → 4) 📄 Логи бота` shows a clear diagnostic if TeleMT is slow or
    permanently down.
    """
    started = time.monotonic()
    for attempt in range(attempts):
        if await is_available():
            logger.info(
                "event=telemt_ready",
                attempts=attempt + 1,
                elapsed_seconds=round(time.monotonic() - started, 1),
            )
            return
        if attempt > 0 and attempt % 5 == 0:
            logger.warning(
                "event=telemt_wait",
                attempts_so_far=attempt,
                elapsed_seconds=round(time.monotonic() - started, 1),
                remaining_attempts=attempts - attempt,
            )
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
    self_heal_task: asyncio.Task | None = None
    reconciler_task: asyncio.Task | None = None
    proxy_watchdog_task: asyncio.Task | None = None
    picker = get_picker()

    async def healthcheck_ping() -> None:
        """Optional external dead-man's-switch (e.g. healthchecks.io).

        Pinged on each successful heartbeat. If the whole VPS or Docker dies,
        the external monitor stops receiving pings and alerts. No-op when unset.
        """
        if not settings.healthcheck_ping_url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await asyncio.wait_for(
                    session.get(settings.healthcheck_ping_url), timeout=10
                )
        except Exception:
            logger.warning("event=healthcheck_ping_failed")

    async def bot_heartbeat_loop() -> None:
        beat_path = settings.database_path.parent / "heartbeats" / "bot.beat"
        beat_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                # Probe Telegram for real: proves the token, network and bot
                # session are alive — not just that the event loop is running.
                # A stalled poller no longer keeps the healthcheck green.
                await asyncio.wait_for(bot.get_me(), timeout=10)
                beat_path.touch()
                await healthcheck_ping()
            except Exception:
                # Brief Telegram blip: skip the beat. The Docker healthcheck
                # max-age window tolerates a few misses; sustained failure goes
                # stale -> unhealthy -> autoheal restarts the container.
                logger.warning("event=bot_heartbeat_probe_failed")
            await asyncio.sleep(30)

    async def telemt_monitor_loop() -> None:
        failed_since: dict[str, float] = {}
        notified: dict[str, bool] = {}
        while True:
            try:
                now = time.monotonic()
                nodes = get_nodes()
                multi = len(nodes) > 1
                for node in nodes:
                    available = await is_available(node.api_url)
                    if available:
                        if notified.get(node.api_url) and settings.admin_id is not None:
                            try:
                                await bot.send_message(
                                    settings.admin_id,
                                    f"✅ Сервер «{node.name}» ({node.public_host}) снова доступен."
                                    if multi
                                    else "✅ TeleMT API снова доступен.",
                                )
                            except Exception:
                                logger.exception("event=telemt_recovery_notification_failed")
                            logger.info("event=telemt_api_recovered", node=node.name)
                        failed_since.pop(node.api_url, None)
                        notified[node.api_url] = False
                    else:
                        started = failed_since.setdefault(node.api_url, now)
                        if (
                            not notified.get(node.api_url)
                            and now - started >= 30
                            and settings.admin_id is not None
                        ):
                            try:
                                await bot.send_message(
                                    settings.admin_id,
                                    f"❌ Сервер «{node.name}» ({node.public_host}) не отвечает "
                                    "более 30 секунд.\nПроверьте этот VPS: mtp → 3 (статус) "
                                    "и mtp → 5 (логи TeleMT)."
                                    if multi
                                    else "❌ TeleMT API не отвечает более 30 секунд. "
                                    "Проверьте VPS: mtp → 3 (статус) и mtp → 5 (логи TeleMT).",
                                )
                            except Exception:
                                logger.exception("event=telemt_outage_notification_failed")
                            logger.error(
                                "event=telemt_api_unavailable",
                                node=node.name,
                                duration_seconds=30,
                            )
                            notified[node.api_url] = True
            except Exception:
                # Never let an unexpected error kill the monitor — that would
                # silently stop all TeleMT outage alerts to the admin.
                logger.exception("event=telemt_monitor_loop_error")
            await asyncio.sleep(10)

    async def on_startup() -> None:
        nonlocal heartbeat_task, telemt_monitor_task, worker_task, self_heal_task
        nonlocal reconciler_task, proxy_watchdog_task
        runtime.STARTED_AT = datetime.now(UTC)
        worker_task = asyncio.create_task(subscription_worker(bot))
        heartbeat_task = asyncio.create_task(bot_heartbeat_loop())
        telemt_monitor_task = asyncio.create_task(telemt_monitor_loop())
        if settings.self_heal_enabled:
            self_heal_task = asyncio.create_task(self_heal_loop(bot))
        if settings.proxy_serving_watchdog_enabled:
            proxy_watchdog_task = asyncio.create_task(proxy_watchdog_loop(bot))
        if len(get_nodes()) > 1:
            reconciler_task = asyncio.create_task(reconciler_loop(bot))
        picker.start(settings.tls_probe_interval)
        # Probe immediately so the first user request already has rankings.
        asyncio.create_task(picker.probe_all())
        logger.info(
            "event=startup",
            telemt_api=settings.telemt_api_url,
            server=f"{settings.server_host}:{settings.proxy_port}",
            tls_domains=settings.fallback_tls_domains,
        )

    async def on_shutdown() -> None:
        for task in (worker_task, heartbeat_task, telemt_monitor_task, self_heal_task, reconciler_task, proxy_watchdog_task):
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
