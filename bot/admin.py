import asyncio
from datetime import UTC, datetime, timedelta
from html import escape
import logging
from pathlib import Path
import shutil
import socket

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import get_settings
from database import (
    ACTIVE_STATUS,
    DISABLED_STATUS,
    create_subscription,
    delete_user_cascade,
    extend_subscription,
    from_db_datetime,
    get_latest_subscription_by_telegram_id,
    get_recent_users,
    get_stats,
    get_user_by_telegram_id,
    mark_subscription_status,
    resolve_support_thread,
    update_latest_subscription_secret_by_telegram_id,
    utc_now,
)
from keyboards import (
    DISABLE_ACCESS_BUTTON,
    EXTEND_ACCESS_BUTTON,
    ISSUE_ACCESS_BUTTON,
    REBOOT_BUTTON,
    ROTATE_ACCESS_BUTTON,
    STATS_BUTTON,
    STATUS_BUTTON,
    USERS_BUTTON,
    admin_menu,
    client_menu,
    confirm_delete_keyboard,
    connect_keyboard,
    tariff_keyboard,
    user_card_keyboard,
)
from proxy_manager import (
    ClientNotFoundError,
    build_tls_proxy_link,
    create_secret,
    delete_secret,
    list_clients,
    mask_secret,
    rotate_secret,
)
import runtime
from tariffs import Tariff, get_tariff


router = Router()


PROXY_APPLY_NOTICE = "✅ Ключ применён в TeleMT мгновенно через API."
RESTART_REQUEST_PATH = Path("/app/data/restart.request")
RESTART_COOLDOWN_SEC = 60
SUPPORT_HEARTBEAT = Path("/app/data/heartbeats/support_bot.beat")
WATCHER_HEARTBEAT = Path("/app/data/heartbeats/watcher.beat")
RESTART_LOG = Path("/app/data/restart.log")


def access_disabled_text(note: str = "") -> str:
    return f"✅ <b>Доступ отключён</b>{note}\n\n" + PROXY_APPLY_NOTICE


def _format_uptime(delta_seconds: float) -> str:
    s = int(delta_seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def _format_ago(delta_seconds: float) -> str:
    s = int(delta_seconds)
    if s < 60:
        return f"{s} сек назад"
    if s < 3600:
        return f"{s // 60} мин назад"
    if s < 86400:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} д назад"


async def _check_proxy(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _read_mem_mb() -> tuple[int, int] | None:
    try:
        data = Path("/proc/meminfo").read_text()
        total_kb = avail_kb = None
        for line in data.splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
        if total_kb and avail_kb:
            used_mb = (total_kb - avail_kb) // 1024
            total_mb = total_kb // 1024
            return used_mb, total_mb
    except Exception:
        pass
    return None


def _read_last_restart() -> str | None:
    try:
        if not RESTART_LOG.exists():
            return None
        lines = [
            line
            for line in RESTART_LOG.read_text().splitlines()
            if "restart triggered" in line
        ]
        if not lines:
            return None
        ts = lines[-1].split("]")[0].lstrip("[")
        return datetime.fromisoformat(ts).strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        return None


class AdminStates(StatesGroup):
    issue_user_id = State()
    issue_tariff = State()
    extend_user_id = State()
    extend_tariff = State()
    rotate_user_id = State()
    disable_user_id = State()


def is_admin(telegram_id: int | None) -> bool:
    settings = get_settings()
    return settings.admin_id is not None and telegram_id == settings.admin_id


async def deny_if_not_admin(message: Message) -> bool:
    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.answer("Доступ запрещён.")
        return True
    return False


def client_id_for(telegram_id: int) -> str:
    return f"tg_{telegram_id}"


def ensure_secret(client_id: str, preferred_secret: str | None = None) -> str:
    users = list_clients()
    if client_id in users:
        return users[client_id]
    return create_secret(client_id, preferred_secret)


def subscription_link(secret: str) -> str:
    settings = get_settings()
    return build_tls_proxy_link(
        settings.server_host,
        settings.proxy_port,
        secret,
        settings.tls_domain,
    )


def parse_telegram_id(text: str | None) -> int:
    if text is None:
        raise ValueError("Введите telegram_id числом.")
    value = text.strip()
    if not value.isdigit():
        raise ValueError("telegram_id должен быть числом.")
    return int(value)


def has_active_subscription(subscription: dict | None) -> bool:
    return (
        subscription is not None
        and subscription["status"] == ACTIVE_STATUS
        and from_db_datetime(subscription["expires_at"]) > utc_now()
    )


def format_db_datetime(value: str | None) -> str:
    if not value:
        return "-"
    return from_db_datetime(value).strftime("%Y-%m-%d %H:%M UTC")


async def grant_access(telegram_id: int, tariff: Tariff, action: str) -> dict:
    settings = get_settings()
    client_id = client_id_for(telegram_id)
    now = utc_now()
    latest = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )

    if action == "auto":
        action = "extend" if latest else "issue"

    if action == "issue":
        secret = ensure_secret(client_id)
        return await create_subscription(
            settings.database_path,
            telegram_id,
            secret,
            tariff.days,
            now,
            now + timedelta(days=tariff.days),
        )

    if action == "extend":
        previous_secret = latest["secret"] if latest and latest["secret"] else None
        secret = ensure_secret(client_id, previous_secret)
        if latest and latest["status"] == ACTIVE_STATUS:
            base = max(from_db_datetime(latest["expires_at"]), now)
        else:
            base = now
        return await extend_subscription(
            settings.database_path,
            telegram_id,
            secret,
            tariff.days,
            now,
            base + timedelta(days=tariff.days),
        )

    raise ValueError("Неизвестное действие.")


def render_user_card(user: dict, subscription: dict | None) -> str:
    username = f"@{user['username']}" if user["username"] else "-"
    full_name = user["full_name"] or "-"

    lines = [
        "👤 Карточка пользователя",
        "",
        f"Telegram ID: <code>{user['telegram_id']}</code>",
        f"Username: {escape(username)}",
        f"Имя: {escape(full_name)}",
        f"Создан: {format_db_datetime(user['created_at'])}",
        "",
        "📦 Подписка:",
    ]

    if subscription is None:
        lines.append("Статус: нет подписки")
        return "\n".join(lines)

    secret = subscription["secret"] or ""
    secret_text = mask_secret(secret) if secret else "-"
    status = subscription["status"]
    if has_active_subscription(subscription):
        status = "active"
    elif status == ACTIVE_STATUS:
        status = "expired"

    lines.extend(
        [
            f"Статус: {escape(status)}",
            f"Тариф: {subscription['tariff_days']} дней",
            f"Срок до: {format_db_datetime(subscription['expires_at'])}",
            f"Secret: <code>{escape(secret_text)}</code>",
        ]
    )
    return "\n".join(lines)


async def edit_user_card(
    callback: CallbackQuery,
    telegram_id: int,
    answer_callback: bool = True,
) -> None:
    settings = get_settings()
    user = await get_user_by_telegram_id(settings.database_path, telegram_id)
    message = callback.message
    if user is None:
        if message is not None and hasattr(message, "answer"):
            await message.answer("Пользователь не найден.", reply_markup=admin_menu())
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    subscription = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )
    text = render_user_card(user, subscription)
    keyboard = user_card_keyboard(telegram_id, has_active_subscription(subscription))

    if message is not None and hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=keyboard)
    elif message is not None and hasattr(message, "answer"):
        await message.answer(text, reply_markup=keyboard)
    if answer_callback:
        await callback.answer()


def users_keyboard(rows: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Открыть {row['telegram_id']}",
                    callback_data=f"admin_user:{row['telegram_id']}",
                )
            ]
            for row in rows
        ]
    )


@router.message(F.reply_to_message, F.text)
async def admin_reply_to_client(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or not is_admin(user.id):
        return

    try:
        settings = get_settings()
        reply_id = message.reply_to_message.message_id
        client_id = await resolve_support_thread(settings.database_path, reply_id)
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
        logging.exception("Client blocked bot while relaying support reply")
        await message.answer("❌ Клиент заблокировал бота")
    except Exception:
        logging.exception("Failed to relay admin support reply to client")


@router.message(Command("admin"))
async def admin_start(message: Message) -> None:
    if await deny_if_not_admin(message):
        return

    settings = get_settings()
    result = await get_stats(settings.database_path)
    await message.answer(
        "🛠 Админ-панель\n\n"
        "📊 Сейчас:\n"
        f"👥 Всего: {result['total_users']}\n"
        f"✅ Активные: {result['active']}\n"
        f"⏰ Истекшие: {result['expired']}\n"
        f"🚫 Отключённые: {result['disabled']}",
        reply_markup=admin_menu(),
    )


@router.message(F.text == REBOOT_BUTTON)
async def reboot_request(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Подтвердить рестарт",
                    callback_data="admin_reboot_confirm",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_reboot_cancel")],
        ]
    )
    await message.answer(
        "<b>🔁 Перезагрузка системы</b>\n\n"
        "Будут пересозданы все три контейнера: основной бот, бот поддержки и MTProto-прокси.\n"
        "Все клиенты потеряют соединение примерно на 30 секунд.\n\n"
        "Продолжить?",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin_reboot_cancel")
async def reboot_cancel(callback: CallbackQuery) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.callback_query(F.data == "admin_reboot_confirm")
async def reboot_confirm(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    message = callback.message
    if RESTART_REQUEST_PATH.exists():
        age = datetime.now(UTC).timestamp() - RESTART_REQUEST_PATH.stat().st_mtime
        if age < RESTART_COOLDOWN_SEC:
            await callback.answer("⏳ Уже идёт перезагрузка, подождите.", show_alert=True)
            return
    if message and hasattr(message, "edit_text"):
        await message.edit_text(
            "🔁 Перезагрузка запущена.\n\n"
            "Бот вернётся через ~30 секунд. Если через минуту бот не отвечает — "
            "проверьте на VPS: mtp → пункт 20 (установка watcher)."
        )
    RESTART_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESTART_REQUEST_PATH.write_text(datetime.now(UTC).isoformat())
    await callback.answer("Запущено")

    async def check_watcher() -> None:
        await asyncio.sleep(10)
        if RESTART_REQUEST_PATH.exists():
            try:
                await bot.send_message(
                    callback.from_user.id,
                    "⚠️ Watcher автоперезагрузки не отвечает.\n\n"
                    "Установите его один раз на VPS: mtp → пункт 20.",
                )
            except Exception:
                logging.exception("Failed to notify admin about missing watcher")

    asyncio.create_task(check_watcher())


@router.message(F.text == STATUS_BUTTON)
async def system_status(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    settings = get_settings()
    now = datetime.now(UTC)

    if runtime.STARTED_AT:
        bot_up = (
            f"✅ работает (включён "
            f"{_format_uptime((now - runtime.STARTED_AT).total_seconds())})"
        )
    else:
        bot_up = "✅ работает"

    if SUPPORT_HEARTBEAT.exists():
        age = now.timestamp() - SUPPORT_HEARTBEAT.stat().st_mtime
        support_line = (
            "✅ работает"
            if age < 90
            else f"❌ не отвечает (последний сигнал {_format_ago(age)})"
        )
    else:
        support_line = "⚠️ ни разу не запускался"

    proxy_ok = await _check_proxy("mtproto", settings.proxy_port)
    proxy_line = (
        f"✅ работает на порту {settings.proxy_port}"
        if proxy_ok
        else f"❌ не отвечает на порту {settings.proxy_port}"
    )

    if WATCHER_HEARTBEAT.exists():
        age = now.timestamp() - WATCHER_HEARTBEAT.stat().st_mtime
        if age < 30:
            watcher_line = "✅ установлен"
        else:
            watcher_line = (
                f"⚠️ не отвечает (последний сигнал {_format_ago(age)}). "
                "Перезапустите его: systemctl restart mtproto-restart-watcher"
            )
    else:
        watcher_line = (
            "⚠️ не установлен — кнопка «Перезагрузка системы» работать не будет.\n"
            "   Откройте на VPS: mtp → пункт 20"
        )

    stats = await get_stats(settings.database_path)

    try:
        keys_count = len(list_clients())
    except Exception:
        keys_count = -1
    keys_line = f"{keys_count}" if keys_count >= 0 else "недоступно"

    try:
        usage = shutil.disk_usage("/app/data")
        free_gb = usage.free / 1024**3
        total_gb = usage.total / 1024**3
        disk_line = f"{free_gb:.1f} ГБ свободно из {total_gb:.1f} ГБ"
    except Exception:
        disk_line = "недоступно"

    mem = _read_mem_mb()
    mem_line = f"{mem[0]} МБ занято из {mem[1]} МБ" if mem else "недоступно (не Linux)"

    last_restart = _read_last_restart() or "через бота ещё не запускали"

    text = (
        "<b>🛠 Состояние сервера</b>\n\n"
        f"🤖 Главный бот: {bot_up}\n"
        f"💬 Бот поддержки: {support_line}\n"
        f"🛰 MTProto-прокси: {proxy_line}\n"
        f"🛡 Авто-перезагрузка (watcher): {watcher_line}\n\n"
        "<b>📊 Клиенты и подписки</b>\n"
        f"   👥 Всего клиентов: {stats['total_users']}\n"
        f"   ✅ Активных подписок: {stats['active']}\n"
        f"   ⏰ Истекших: {stats['expired']}\n"
        f"   🚫 Отключённых: {stats['disabled']}\n"
        f"   🔑 Ключей в proxy-конфиге: {keys_line}\n\n"
        f"💾 Диск VPS: {disk_line}\n"
        f"🧠 Память VPS: {mem_line}\n"
        f"📡 Адрес сервера: {settings.server_host}:{settings.proxy_port}\n\n"
        "⚠️ Изменения .env применяются только после рестарта контейнеров "
        "(кнопка «🔁 Перезагрузка системы»).\n"
        f"🕐 Последний рестарт через бота: {last_restart}"
    )
    await message.answer(text, reply_markup=admin_menu())


@router.message(F.text == USERS_BUTTON)
async def users(message: Message) -> None:
    if await deny_if_not_admin(message):
        return

    settings = get_settings()
    rows = await get_recent_users(settings.database_path, limit=10)
    if not rows:
        await message.answer("Пользователей пока нет.", reply_markup=admin_menu())
        return

    lines = ["Последние пользователи:"]
    for row in rows:
        username = f"@{row['username']}" if row["username"] else "-"
        status = row["subscription_status"]
        if status == ACTIVE_STATUS and row["expires_at"]:
            status = f"active до {from_db_datetime(row['expires_at']).strftime('%Y-%m-%d')}"
        lines.append(
            f"{row['telegram_id']} | {escape(username)} | "
            f"{escape(row['full_name'] or '-')} | {escape(status)}"
        )
    await message.answer("\n".join(lines), reply_markup=users_keyboard(rows))


@router.callback_query(F.data == "admin_users_back")
async def users_back(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer(
            "Сообщение устарело. Откройте список заново.",
            show_alert=True,
        )
        return

    settings = get_settings()
    rows = await get_recent_users(settings.database_path, limit=10)
    if not rows:
        await message.answer("Пользователей пока нет.", reply_markup=admin_menu())
        await callback.answer()
        return

    lines = ["Последние пользователи:"]
    for row in rows:
        username = f"@{row['username']}" if row["username"] else "-"
        status = row["subscription_status"]
        if status == ACTIVE_STATUS and row["expires_at"]:
            status = f"active до {from_db_datetime(row['expires_at']).strftime('%Y-%m-%d')}"
        lines.append(
            f"{row['telegram_id']} | {escape(username)} | "
            f"{escape(row['full_name'] or '-')} | {escape(status)}"
        )

    if hasattr(message, "edit_text"):
        await message.edit_text("\n".join(lines), reply_markup=users_keyboard(rows))
    else:
        await message.answer("\n".join(lines), reply_markup=users_keyboard(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user:"))
async def admin_user_card(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return

    await edit_user_card(callback, int(parts[1]))


@router.callback_query(F.data.startswith("admin_card_extend:"))
async def admin_card_extend(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer(
            "Сообщение устарело. Откройте карточку заново.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return

    telegram_id = int(parts[1])
    await state.update_data(telegram_id=telegram_id)
    await message.answer(
        f"Выберите срок продления для пользователя <code>{telegram_id}</code>:",
        reply_markup=tariff_keyboard("extend"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_card_rotate:"))
async def admin_card_rotate(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    telegram_id = int(parts[1])

    settings = get_settings()
    subscription = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )
    if subscription is None:
        await callback.answer("Подписка не найдена.", show_alert=True)
        await edit_user_card(callback, telegram_id, answer_callback=False)
        return

    try:
        secret = rotate_secret(client_id_for(telegram_id))
        subscription = await update_latest_subscription_secret_by_telegram_id(
            settings.database_path,
            telegram_id,
            secret,
        )
        if subscription is None:
            raise ValueError("Подписка не найдена после обновления ключа.")
    except ClientNotFoundError:
        await callback.answer("Ключ не найден в proxy config.", show_alert=True)
        return
    except Exception as exc:
        logging.exception("Failed to rotate secret from card for telegram_id=%s", telegram_id)
        if message is not None and hasattr(message, "answer"):
            await message.answer(
                "Не удалось обновить ключ. Старый ключ мог остаться активным.\n\n"
                f"Ошибка: {escape(str(exc))}"
            )
        await callback.answer()
        return

    link = subscription_link(subscription["secret"])
    expires_at_text = from_db_datetime(subscription["expires_at"]).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    try:
        await bot.send_message(
            telegram_id,
            "🔄 Администратор обновил ваш ключ доступа.\n\n"
            f"⏳ Действует до: {expires_at_text}",
            reply_markup=connect_keyboard(link),
        )
    except Exception as exc:
        logging.warning("Failed to notify rotated user %s: %s", telegram_id, exc)

    if message is not None and hasattr(message, "answer"):
        await message.answer(
            "Ключ обновлён.\n\n"
            f"Пользователь: {telegram_id}\n"
            f"Срок до: {expires_at_text}\n\n"
            "✅ Ключ применится автоматически в течение ~5 секунд."
        )
    await edit_user_card(callback, telegram_id)


@router.callback_query(F.data.startswith("admin_card_disable:"))
async def admin_card_disable(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    telegram_id = int(parts[1])

    settings = get_settings()
    subscription = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )

    try:
        delete_secret(client_id_for(telegram_id))
    except ClientNotFoundError:
        pass
    except Exception as exc:
        logging.exception(
            "Failed to delete secret for telegram_id=%s during card disable",
            telegram_id,
        )
        if message is not None and hasattr(message, "answer"):
            await message.answer(
                "Secret не удалось удалить из proxy config. "
                "Статус в базе не изменён, попробуйте повторить позже.\n\n"
                f"Ошибка: {escape(str(exc))}"
            )
        await callback.answer()
        return

    if subscription is not None:
        await mark_subscription_status(
            settings.database_path,
            subscription["id"],
            DISABLED_STATUS,
        )

    if message is not None and hasattr(message, "answer"):
        await message.answer(access_disabled_text())
    await edit_user_card(callback, telegram_id)


@router.callback_query(F.data.startswith("admin_card_delete:"))
async def admin_card_delete(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    if message is None or not hasattr(message, "edit_text"):
        await callback.answer(
            "Сообщение устарело. Откройте карточку заново.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    telegram_id = int(parts[1])

    await message.edit_text(
        f"<b>⚠️ Удалить пользователя {telegram_id} полностью?</b>\n\n"
        "Будут удалены:\n"
        "• ключ пользователя в TeleMT\n"
        "• подписки в БД\n"
        "• запись пользователя в БД\n\n"
        "Действие необратимо.",
        reply_markup=confirm_delete_keyboard(telegram_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_card_delete_yes:"))
async def admin_card_delete_yes(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer(
            "Сообщение устарело. Откройте список заново.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    telegram_id = int(parts[1])

    settings = get_settings()
    try:
        delete_secret(client_id_for(telegram_id))
    except ClientNotFoundError:
        pass
    except Exception as exc:
        logging.exception(
            "Failed to delete secret for telegram_id=%s during full delete",
            telegram_id,
        )
        await message.answer(
            "Secret не удалось удалить из proxy config. "
            "Пользователь в БД не удалён, попробуйте повторить позже.\n\n"
            f"Ошибка: {escape(str(exc))}"
        )
        await callback.answer()
        return

    await delete_user_cascade(settings.database_path, telegram_id)
    await message.answer(
        f"🗑 <b>Пользователь {telegram_id} удалён полностью.</b>\n\n"
        + PROXY_APPLY_NOTICE
    )
    await callback.answer()

    rows = await get_recent_users(settings.database_path, limit=10)
    if not rows:
        await message.answer("Пользователей пока нет.", reply_markup=admin_menu())
        return

    lines = ["Последние пользователи:"]
    for row in rows:
        username = f"@{row['username']}" if row["username"] else "-"
        status = row["subscription_status"]
        if status == ACTIVE_STATUS and row["expires_at"]:
            status = f"active до {from_db_datetime(row['expires_at']).strftime('%Y-%m-%d')}"
        lines.append(
            f"{row['telegram_id']} | {escape(username)} | "
            f"{escape(row['full_name'] or '-')} | {escape(status)}"
        )
    await message.answer("\n".join(lines), reply_markup=users_keyboard(rows))


@router.message(F.text == ISSUE_ACCESS_BUTTON)
async def issue_access(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    await state.set_state(AdminStates.issue_user_id)
    await message.answer("Введите telegram_id пользователя.")


@router.message(AdminStates.issue_user_id)
async def issue_user_id(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return

    settings = get_settings()
    try:
        telegram_id = parse_telegram_id(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    user = await get_user_by_telegram_id(settings.database_path, telegram_id)
    if user is None:
        await message.answer("Пользователь не найден. Сначала он должен нажать /start.")
        return

    await state.update_data(telegram_id=telegram_id)
    await state.set_state(AdminStates.issue_tariff)
    await message.answer("Выберите тариф:", reply_markup=tariff_keyboard("issue"))


@router.message(F.text == EXTEND_ACCESS_BUTTON)
async def extend_access(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    await state.set_state(AdminStates.extend_user_id)
    await message.answer("Введите telegram_id пользователя для продления.")


@router.message(AdminStates.extend_user_id)
async def extend_user_id(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return

    settings = get_settings()
    try:
        telegram_id = parse_telegram_id(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    user = await get_user_by_telegram_id(settings.database_path, telegram_id)
    if user is None:
        await message.answer("Пользователь не найден. Сначала он должен нажать /start.")
        return

    await state.update_data(telegram_id=telegram_id)
    await state.set_state(AdminStates.extend_tariff)
    await message.answer("Выберите срок продления:", reply_markup=tariff_keyboard("extend"))


@router.callback_query(F.data.startswith("admin_tariff:"))
async def choose_tariff(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer(
            "Сообщение устарело. Начните действие заново через /admin.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    action = parts[1]
    try:
        tariff = get_tariff(int(parts[2]))
    except (TypeError, ValueError):
        await callback.answer(
            "Тариф не найден. Начните действие заново через /admin.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    telegram_id_raw = data.get("telegram_id")
    if telegram_id_raw is None:
        await message.answer(
            "Сессия устарела. Начните действие заново через /admin.",
            reply_markup=admin_menu(),
        )
        await callback.answer()
        return
    telegram_id = int(telegram_id_raw)

    if action == "issue":
        admin_text = "Доступ выдан."
        client_text = "Администратор выдал вам доступ."
    elif action == "extend":
        admin_text = "Доступ продлён."
        client_text = "Администратор продлил ваш доступ."
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    try:
        subscription = await grant_access(telegram_id, tariff, action)
    except Exception as exc:
        await message.answer(f"Ошибка: {escape(str(exc))}")
        await callback.answer()
        return

    link = subscription_link(subscription["secret"])
    expires_at_text = from_db_datetime(subscription["expires_at"]).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    await bot.send_message(
        telegram_id,
        f"{client_text}\n\n"
        f"⏳ Действует до: {expires_at_text}",
        reply_markup=connect_keyboard(link),
    )
    await message.answer(
        f"{admin_text}\n\n"
        f"Пользователь: {telegram_id}\n"
        f"Тариф: {tariff.title}\n"
        f"Срок до: {expires_at_text}\n\n"
        "✅ Ключ применится автоматически в течение ~5 секунд.",
        reply_markup=admin_menu(),
    )
    await state.clear()
    await callback.answer()


@router.callback_query(F.data.startswith("admin_payapprove:"))
async def approve_payment_request(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await callback.answer("Некорректная заявка.", show_alert=True)
        return

    telegram_id = int(parts[1])
    days = int(parts[2])
    try:
        tariff = get_tariff(days)
        subscription = await grant_access(telegram_id, tariff, "auto")
    except Exception as exc:
        if message is not None and hasattr(message, "answer"):
            await message.answer(f"Ошибка: {escape(str(exc))}")
        await callback.answer("Не удалось выдать доступ.", show_alert=True)
        return

    link = subscription_link(subscription["secret"])
    expires_at_text = from_db_datetime(subscription["expires_at"]).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    await bot.send_message(
        telegram_id,
        "<b>🎉 Доступ активирован!</b>\n\n"
        f"📦 Тариф: {escape(tariff.title)}\n"
        f"⏳ Действует до: {expires_at_text}\n\n"
        "Нажмите кнопку ниже, чтобы подключиться.",
        reply_markup=connect_keyboard(link),
    )

    admin_text = (
        "✅ Доступ выдан\n\n"
        f"Пользователь: {telegram_id}\n"
        f"Тариф: {tariff.title}\n"
        f"Срок до: {expires_at_text}\n\n"
        "✅ Ключ применится автоматически в течение ~5 секунд."
    )
    if message is not None and hasattr(message, "edit_text"):
        await message.edit_text(admin_text)
    elif message is not None and hasattr(message, "answer"):
        await message.answer(admin_text, reply_markup=admin_menu())
    await callback.answer("Доступ выдан")


@router.callback_query(F.data.startswith("admin_payreject:"))
async def reject_payment_request(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user is None or not is_admin(user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    message = callback.message
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await callback.answer("Некорректная заявка.", show_alert=True)
        return

    telegram_id = int(parts[1])
    await bot.send_message(
        telegram_id,
        "К сожалению, заявка отклонена. Свяжитесь с поддержкой",
        reply_markup=client_menu(),
    )

    if message is not None and hasattr(message, "edit_text"):
        await message.edit_text("❌ Заявка отклонена")
    elif message is not None and hasattr(message, "answer"):
        await message.answer("❌ Заявка отклонена", reply_markup=admin_menu())
    await callback.answer("Заявка отклонена")


@router.message(F.text == ROTATE_ACCESS_BUTTON)
async def rotate_access(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    await state.set_state(AdminStates.rotate_user_id)
    await message.answer(
        "Введите telegram_id пользователя, которому нужно обновить ключ."
    )


@router.message(AdminStates.rotate_user_id)
async def rotate_user_id(message: Message, state: FSMContext, bot: Bot) -> None:
    if await deny_if_not_admin(message):
        return

    settings = get_settings()
    try:
        telegram_id = parse_telegram_id(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    subscription = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )
    if subscription is None:
        await message.answer("Подписка не найдена.", reply_markup=admin_menu())
        await state.clear()
        return

    client_id = client_id_for(telegram_id)
    try:
        secret = rotate_secret(client_id)
        subscription = await update_latest_subscription_secret_by_telegram_id(
            settings.database_path,
            telegram_id,
            secret,
        )
        if subscription is None:
            raise ValueError("Подписка не найдена после обновления ключа.")
    except ClientNotFoundError:
        await message.answer(
            "Ключ не найден в proxy config. "
            "Сначала выдайте или продлите доступ этому пользователю.",
            reply_markup=admin_menu(),
        )
        await state.clear()
        return
    except Exception as exc:
        logging.exception(
            "Failed to rotate secret for telegram_id=%s",
            telegram_id,
        )
        await message.answer(
            "Не удалось обновить ключ. Старый ключ мог остаться активным.\n\n"
            f"Ошибка: {escape(str(exc))}",
            reply_markup=admin_menu(),
        )
        await state.clear()
        return

    link = subscription_link(subscription["secret"])
    expires_at_text = from_db_datetime(subscription["expires_at"]).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    try:
        await bot.send_message(
            telegram_id,
            "🔄 Администратор обновил ваш ключ доступа.\n\n"
            f"⏳ Действует до: {expires_at_text}",
            reply_markup=connect_keyboard(link),
        )
    except Exception as exc:
        logging.warning("Failed to notify rotated user %s: %s", telegram_id, exc)

    await message.answer(
        "Ключ обновлён.\n\n"
        f"Пользователь: {telegram_id}\n"
        f"Срок до: {expires_at_text}\n\n"
        "✅ Ключ применится автоматически в течение ~5 секунд.",
        reply_markup=admin_menu(),
    )
    await state.clear()


@router.message(F.text == DISABLE_ACCESS_BUTTON)
async def disable_access(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    await state.set_state(AdminStates.disable_user_id)
    await message.answer("Введите telegram_id пользователя для отключения.")


@router.message(AdminStates.disable_user_id)
async def disable_user_id(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return

    settings = get_settings()
    try:
        telegram_id = parse_telegram_id(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    subscription = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )

    try:
        delete_secret(client_id_for(telegram_id))
    except ClientNotFoundError:
        pass
    except Exception as exc:
        logging.exception(
            "Failed to delete secret for telegram_id=%s during admin disable",
            telegram_id,
        )
        await message.answer(
            "Secret не удалось удалить из proxy config. "
            "Статус в базе не изменён, попробуйте повторить позже.\n\n"
            f"Ошибка: {escape(str(exc))}"
        )
        await state.clear()
        return

    if subscription is not None:
        await mark_subscription_status(
            settings.database_path,
            subscription["id"],
            DISABLED_STATUS,
        )

    await message.answer(
        access_disabled_text(
            " (подписки в БД не было)" if subscription is None else ""
        ),
        reply_markup=admin_menu(),
    )
    await state.clear()


@router.message(F.text == STATS_BUTTON)
async def stats(message: Message) -> None:
    if await deny_if_not_admin(message):
        return

    settings = get_settings()
    result = await get_stats(settings.database_path)
    await message.answer(
        "Статистика:\n\n"
        f"Всего пользователей: {result['total_users']}\n"
        f"Активные подписки: {result['active']}\n"
        f"Истекшие подписки: {result['expired']}\n"
        f"Отключенные подписки: {result['disabled']}",
        reply_markup=admin_menu(),
    )
