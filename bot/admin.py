from datetime import timedelta
from html import escape
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import get_settings
from database import (
    ACTIVE_STATUS,
    DISABLED_STATUS,
    create_subscription,
    extend_subscription,
    from_db_datetime,
    get_latest_subscription_by_telegram_id,
    get_recent_users,
    get_stats,
    get_user_by_telegram_id,
    mark_subscription_status,
    update_latest_subscription_secret_by_telegram_id,
    utc_now,
)
from keyboards import (
    DISABLE_ACCESS_BUTTON,
    EXTEND_ACCESS_BUTTON,
    ISSUE_ACCESS_BUTTON,
    ROTATE_ACCESS_BUTTON,
    STATS_BUTTON,
    USERS_BUTTON,
    admin_menu,
    connect_keyboard,
    tariff_keyboard,
    user_card_keyboard,
)
from proxy_manager import (
    ClientNotFoundError,
    build_proxy_link,
    create_secret,
    delete_secret,
    list_clients,
    mask_secret,
    rotate_secret,
)
from tariffs import get_tariff


router = Router()


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
    return build_proxy_link(settings.server_host, settings.proxy_port, secret)


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
            "Чтобы изменения вступили в силу на proxy, выполните на host-системе:\n"
            "docker compose kill -s SIGUSR2 mtproto\n"
            "fallback: docker compose restart mtproto"
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
    if subscription is None:
        await callback.answer("Подписка не найдена.", show_alert=True)
        await edit_user_card(callback, telegram_id, answer_callback=False)
        return

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

    await mark_subscription_status(
        settings.database_path,
        subscription["id"],
        DISABLED_STATUS,
    )

    if message is not None and hasattr(message, "answer"):
        await message.answer(
            "Доступ отключён.\n\n"
            "Чтобы изменения вступили в силу на proxy, выполните на host-системе:\n"
            "docker compose kill -s SIGUSR2 mtproto\n"
            "fallback: docker compose restart mtproto"
        )
    await edit_user_card(callback, telegram_id)


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

    settings = get_settings()
    client_id = client_id_for(telegram_id)
    now = utc_now()
    latest = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )

    try:
        if action == "issue":
            secret = ensure_secret(client_id)
            starts_at = now
            expires_at = starts_at + timedelta(days=tariff.days)
            subscription = await create_subscription(
                settings.database_path,
                telegram_id,
                secret,
                tariff.days,
                starts_at,
                expires_at,
            )
            admin_text = "Доступ выдан."
            client_text = "Администратор выдал вам доступ."
        elif action == "extend":
            previous_secret = latest["secret"] if latest and latest["secret"] else None
            secret = ensure_secret(client_id, previous_secret)
            if latest and latest["status"] == ACTIVE_STATUS:
                base = max(from_db_datetime(latest["expires_at"]), now)
            else:
                base = now
            starts_at = now
            expires_at = base + timedelta(days=tariff.days)
            subscription = await extend_subscription(
                settings.database_path,
                telegram_id,
                secret,
                tariff.days,
                starts_at,
                expires_at,
            )
            admin_text = "Доступ продлён."
            client_text = "Администратор продлил ваш доступ."
        else:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return
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
        "Чтобы изменения вступили в силу на proxy, выполните на host-системе:\n"
        "docker compose kill -s SIGUSR2 mtproto\n"
        "fallback: docker compose restart mtproto",
        reply_markup=admin_menu(),
    )
    await state.clear()
    await callback.answer()


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
        "Чтобы изменения вступили в силу на proxy, выполните на host-системе:\n"
        "docker compose kill -s SIGUSR2 mtproto\n"
        "fallback: docker compose restart mtproto",
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
    if subscription is None:
        await message.answer("Подписка не найдена.", reply_markup=admin_menu())
        await state.clear()
        return

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

    await mark_subscription_status(
        settings.database_path,
        subscription["id"],
        DISABLED_STATUS,
    )

    await message.answer(
        "Доступ отключён.\n\n"
        "Чтобы изменения вступили в силу на proxy, выполните на host-системе:\n"
        "docker compose kill -s SIGUSR2 mtproto\n"
        "fallback: docker compose restart mtproto",
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
