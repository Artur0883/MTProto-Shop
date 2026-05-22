import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from admin import router as admin_router
from client import router as client_router
from config import get_settings
from database import init_db
from proxy_manager import ensure_runtime_config
from subscriptions import subscription_worker


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    ensure_runtime_config(settings.proxy_config_path)

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    if settings.admin_id is None:
        logging.warning("ADMIN_ID is not set; /admin will be unavailable")

    await init_db(settings.database_path)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(client_router)

    worker_task: asyncio.Task | None = None

    async def on_startup() -> None:
        nonlocal worker_task
        worker_task = asyncio.create_task(subscription_worker(bot))
        logging.info("Telegram Bot MVP is running")

    async def on_shutdown() -> None:
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        await bot.session.close()
        logging.info("Telegram Bot MVP stopped")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logging.info("Proxy config path: %s", settings.proxy_config_path)
    logging.info("Server host: %s, proxy port: %s", settings.server_host, settings.proxy_port)
    logging.info("Database path: %s", settings.database_path)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
