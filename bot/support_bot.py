import asyncio
from html import escape
import logging
from pathlib import Path
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import Message

from config import get_settings
from database import init_db, record_support_bot_thread, resolve_support_bot_thread


router = Router()
HEARTBEAT_PATH = Path("/app/data/heartbeats/support_bot.beat")


def is_admin(telegram_id: int | None) -> bool:
    settings = get_settings()
    return settings.admin_id is not None and telegram_id == settings.admin_id


async def heartbeat_loop() -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            HEARTBEAT_PATH.touch()
        except Exception:
            logging.exception("support_bot heartbeat failed")
        await asyncio.sleep(30)


async def relay_client_to_admin(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    if settings.admin_id is None or user is None or is_admin(user.id):
        return
    if message.text and message.text.startswith("/"):
        return

    try:
        username = f"@{escape(user.username)}" if user.username else "без username"
        header_msg = await message.bot.send_message(
            settings.admin_id,
            f"💬 От клиента {escape(user.full_name)} {username} · ID <code>{user.id}</code>",
        )
        await record_support_bot_thread(
            settings.database_path,
            header_msg.message_id,
            user.id,
        )
        copied_message = await message.bot.copy_message(
            chat_id=settings.admin_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await record_support_bot_thread(
            settings.database_path,
            copied_message.message_id,
            user.id,
        )
    except Exception:
        logging.exception("Failed to relay message from support bot client to admin")
        await message.answer("Поддержка временно недоступна, попробуйте позже.")


@router.message(CommandStart())
async def start(message: Message) -> None:
    user = message.from_user
    if user is not None and is_admin(user.id):
        await message.answer(
            "✅ Бот поддержки готов.\n\n"
            "Отвечайте через reply на скопированное сообщение клиента."
        )
        return

    await message.answer(
        "<b>💬 Поддержка</b>\n\n"
        "Напишите ваш вопрос прямо в этот чат — оператор ответит здесь же."
    )


@router.message(F.reply_to_message)
async def relay_reply(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    if not is_admin(user.id):
        await relay_client_to_admin(message)
        return

    try:
        settings = get_settings()
        reply_id = message.reply_to_message.message_id
        client_id = await resolve_support_bot_thread(settings.database_path, reply_id)
        if client_id is None:
            await message.answer(
                "⚠️ Не нашёл диалог с клиентом для этого reply.\n\n"
                "Ответьте свайпом на сообщение клиента (с username и Telegram ID). "
                "Старые сообщения могли быть забыты после рестарта бота."
            )
            return

        logging.info("admin reply resolved client_id=%s reply_to=%s", client_id, reply_id)
        await bot.copy_message(
            chat_id=client_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramForbiddenError:
        logging.exception("Client blocked support bot while relaying admin reply")
        await message.answer("❌ Клиент заблокировал бота")
    except Exception:
        logging.exception("Failed to relay support bot admin reply to client")


@router.message(F.text)
async def relay_text(message: Message) -> None:
    await relay_client_to_admin(message)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if not settings.support_bot_token:
        logging.warning("SUPPORT_BOT_TOKEN is not set; support bot is waiting for configuration")
        await asyncio.Event().wait()
        return
    if settings.admin_id is None:
        logging.warning("ADMIN_ID is not set; support relay is unavailable")

    await init_db(settings.database_path)
    asyncio.create_task(heartbeat_loop())
    bot = Bot(
        token=settings.support_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        logging.info("Support bot is running")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
