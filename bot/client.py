from datetime import UTC, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, User

from config import get_settings
from database import (
    ACTIVE_STATUS,
    create_subscription,
    extend_subscription,
    from_db_datetime,
    get_active_subscription_by_telegram_id,
    get_last_secret_rotated_at,
    get_latest_subscription_by_telegram_id,
    has_used_trial,
    mark_secret_rotated,
    mark_trial_used,
    record_support_thread,
    update_latest_subscription_secret_by_telegram_id,
    upsert_user,
    utc_now,
)
from keyboards import (
    BUY_BUTTON,
    CLIENT_SUPPORT_BUTTON,
    CONNECT_BUTTON,
    DAYS_LEFT_BUTTON,
    HELP_BUTTON,
    INSTRUCTION_BUTTON,
    LEGACY_BUY_BUTTON,
    MY_LINK_BUTTON,
    MY_PROXY_BUTTON,
    MY_SUBSCRIPTION_BUTTON,
    OLD_CART_BUY_BUTTON,
    OLD_BUY_BUTTON,
    OLD_DAYS_LEFT_BUTTON,
    OLD_MY_LINK_BUTTON,
    OLD_MY_PROXIES_BUTTON,
    OLD_MY_PROXY_BUTTON,
    OLD_MY_SUBSCRIPTION_BUTTON,
    SUPPORT_BUTTON,
    TRY_FREE_BUTTON,
    admin_pay_request_keyboard,
    alt_links_keyboard,
    client_menu,
    client_tariff_keyboard,
    connect_keyboard,
    help_menu,
    instructions_menu,
    manual_pay_keyboard,
    my_subscription_keyboard,
    support_keyboard,
    try_or_buy_keyboard,
)
from logging_setup import get_logger
from proxy_manager import (
    CircuitOpenError,
    RotateCooldownError,
    TeleMTError,
    build_alternative_links,
    build_tls_proxy_link,
    create_secret,
    rotate_secret,
)
from rate_limit import check_action_rate, format_retry_after
from tariffs import Tariff, get_tariff
from tls_domains import get_picker


router = Router()
logger = get_logger(__name__)


class ClientSupportStates(StatesGroup):
    waiting_message = State()


PROXY_CLEANUP_NOTICE = (
    "⚠️ Перед подключением нового ключа удалите старые нерабочие прокси в Telegram.\n\n"
    "Где удалить:\n\n"
    "iPhone (iOS 17 и ниже):\n"
    "Telegram → Настройки → Данные и память → Прокси\n\n"
    "iPhone (iOS 18+):\n"
    "Настройки iPhone → Apps → Telegram → Прокси\n"
    "ИЛИ Telegram → Настройки → Данные и память → Прокси\n\n"
    "Android / Huawei:\n"
    "Telegram → Настройки → Данные и память → Прокси\n\n"
    "Windows / macOS:\n"
    "Telegram → Настройки → Продвинутые настройки → Тип соединения → Прокси\n\n"
    "После удаления старых прокси нажмите на новый ключ и выберите «Подключить прокси»."
)


HELP_CHECKLIST_TEXT = (
    "<b>🆘 Не подключается?</b>\n\n"
    "Проверьте 6 пунктов:\n"
    "1. Обновите Telegram до последней версии.\n"
    "2. Отключите другой VPN или proxy.\n"
    "3. Попробуйте другую сеть: Wi-Fi или мобильный интернет.\n"
    "4. Нажмите «🌐 Альтернативные ссылки» и попробуйте другой вариант.\n"
    "5. В разделе «📅 Моя подписка» нажмите «🔄 Обновить ключ».\n"
    "6. Если не помогло — напишите в поддержку."
)
INSTRUCTION_TEXTS = {
    "client_instr_iphone": (
        "<b>📱 iPhone (iOS 17 и ниже)</b>\n\n"
        "1. Обновите Telegram до последней версии в App Store.\n"
        "2. Нажмите «🔐 Подключиться» в этом боте.\n"
        "3. Telegram спросит «Включить прокси?» — нажмите ВКЛЮЧИТЬ.\n"
        "4. Готово. В правом верхнем углу появится значок щита 🛡.\n\n"
        "Если ссылка не открывается — нажмите «🌐 Альтернативные ссылки» "
        "и попробуйте другой домен TLS-маскировки."
    ),
    "client_instr_iphone18": (
        "<b>📱 iPhone (iOS 18 и новее)</b>\n\n"
        "В iOS 18 раздел Прокси переехал. Если вы не видите кнопку «Прокси» "
        "в Telegram:\n\n"
        "1. Настройки iPhone → Apps → Telegram → Прокси.\n"
        "2. ИЛИ откройте Telegram → Настройки → Данные и память → Прокси.\n\n"
        "Дальше как обычно:\n"
        "3. Нажмите «🔐 Подключиться» в этом боте.\n"
        "4. Telegram спросит «Включить прокси?» — нажмите ВКЛЮЧИТЬ.\n"
        "5. В правом верхнем углу появится значок щита 🛡."
    ),
    "client_instr_android": (
        "<b>🤖 Android / Huawei (без GMS)</b>\n\n"
        "1. Обновите Telegram из доступного вам магазина или с telegram.org.\n"
        "2. Нажмите «🔐 Подключиться» в этом боте.\n"
        "3. Telegram покажет настройки proxy — нажмите ВКЛЮЧИТЬ.\n"
        "4. Готово. В верхней части Telegram появится значок щита 🛡.\n\n"
        "Google Play Services для MTProto не требуются — подключение работает "
        "на любом Android и на Huawei без GMS.\n\n"
        "На Android 14+: если соединение рвётся в фоне, отключите оптимизацию "
        "батареи для Telegram (Настройки → Приложения → Telegram → Батарея → Без ограничений)."
    ),
    "client_instr_desktop": (
        "<b>💻 Windows / macOS / Linux</b>\n\n"
        "1. Обновите Telegram Desktop до последней версии.\n"
        "2. Нажмите «🔐 Подключиться» в этом боте.\n"
        "3. Telegram Desktop откроет окно proxy — подтвердите подключение.\n"
        "4. Готово. В приложении появится значок щита 🛡.\n\n"
        "Если кнопка не открывает Telegram автоматически — скопируйте ссылку "
        "из меню «🌐 Альтернативные ссылки» и вставьте в браузер."
    ),
    "client_instr_x": (
        "<b>📱 Telegram X</b>\n\n"
        "Нажмите «🔐 Подключиться» и подтвердите proxy, если приложение покажет "
        "такой диалог.\n\n"
        "Telegram X в 2026 году поддерживается ограниченно. Если соединение "
        "нестабильно — используйте официальный Telegram (он также бесплатный "
        "и обновляется чаще)."
    ),
    "client_instr_web": (
        "<b>🌐 Telegram Web</b>\n\n"
        "⚠️ Веб-версия Telegram <b>не поддерживает MTProto proxy</b>.\n\n"
        "Откройте эту же кнопку «🔐 Подключиться» в установленном Telegram:\n"
        "• iPhone — из App Store\n"
        "• Android — Google Play / RuStore / telegram.org\n"
        "• Windows / macOS / Linux — Telegram Desktop с telegram.org\n\n"
        "Для веб-версии MTProto proxy в принципе не нужен — она использует "
        "обычные HTTPS-соединения и блокируется/не блокируется по другим правилам."
    ),
}
SALES_DISABLED_TEXT = (
    "Продажа временно недоступна. Если у вас уже есть активный доступ — "
    "используйте кнопку 🔑 Мои ключи."
)
PROXY_TEMPORARILY_UNAVAILABLE_TEXT = (
    "Сервис подключения временно недоступен. Попробуйте ещё раз через 1–2 минуты "
    "или обратитесь в поддержку."
)


def format_datetime(value: str) -> str:
    return from_db_datetime(value).astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def days_left(expires_at: str) -> int:
    delta = from_db_datetime(expires_at) - utc_now()
    if delta.total_seconds() <= 0:
        return 0
    return max(1, delta.days + (1 if delta.seconds else 0))


def client_id_for(telegram_id: int) -> str:
    return f"tg_{telegram_id}"


def primary_tls_domain() -> str:
    """Best-known TLS domain at this moment, falling back to settings if no probe yet."""
    settings = get_settings()
    picker = get_picker()
    try:
        return picker.pick_best() or settings.tls_domain
    except Exception:
        return settings.tls_domain


def build_primary_link(secret: str) -> str:
    settings = get_settings()
    return build_tls_proxy_link(
        settings.server_host,
        settings.proxy_port,
        secret,
        primary_tls_domain(),
    )


def support_url(contact: str) -> str:
    contact = contact.strip()

    if contact.startswith("@"):
        return "https://t.me/" + contact[1:]
    if contact.startswith("https://t.me/"):
        return contact
    if contact.startswith("http://t.me/"):
        return "https://t.me/" + contact.split("t.me/", 1)[1]
    if contact.startswith("t.me/"):
        return "https://" + contact
    return ""


async def issue_access(telegram_id: int, tariff: Tariff) -> dict:
    settings = get_settings()
    latest = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )
    previous_secret = latest["secret"] if latest and latest["secret"] else None
    client_id = client_id_for(telegram_id)
    try:
        secret = await create_secret(client_id, previous_secret)
    except (CircuitOpenError, TeleMTError) as exc:
        if previous_secret:
            logger.warning(
                "event=issue_access_degraded_using_previous_secret",
                telegram_id=telegram_id,
                error=str(exc),
            )
            secret = previous_secret
        else:
            raise

    now = utc_now()

    if latest and latest["status"] == ACTIVE_STATUS:
        base = max(from_db_datetime(latest["expires_at"]), now)
        return await extend_subscription(
            settings.database_path,
            telegram_id,
            secret,
            tariff.days,
            now,
            base + timedelta(days=tariff.days),
        )
    if latest:
        return await extend_subscription(
            settings.database_path,
            telegram_id,
            secret,
            tariff.days,
            now,
            now + timedelta(days=tariff.days),
        )
    return await create_subscription(
        settings.database_path,
        telegram_id,
        secret,
        tariff.days,
        now,
        now + timedelta(days=tariff.days),
    )


async def send_granted_access(message: Message, tariff: Tariff, subscription: dict) -> None:
    link = build_primary_link(subscription["secret"])
    await message.answer(
        "<b>🎉 Доступ активирован!</b>\n\n"
        f"📦 Тариф: {escape(tariff.title)}\n"
        f"📅 Действует до: {format_datetime(subscription['expires_at'])}\n\n"
        f"{PROXY_CLEANUP_NOTICE}",
        reply_markup=connect_keyboard(link),
    )


async def grant_free_trial(message: Message, user: User) -> bool | None:
    settings = get_settings()
    await upsert_user(
        settings.database_path,
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    if await has_used_trial(settings.database_path, user.id):
        await message.answer(
            "🎁 Пробный день уже использован.\n\n"
            "Чтобы пользоваться дальше — выберите тариф.",
            reply_markup=try_or_buy_keyboard(),
        )
        return False

    tariff = get_tariff(1)
    try:
        subscription = await issue_access(user.id, tariff)
    except Exception:
        logger.exception(
            "event=client_issue_access_failed",
            telegram_id=user.id,
        )
        await message.answer(
            PROXY_TEMPORARILY_UNAVAILABLE_TEXT,
            reply_markup=client_menu(),
        )
        return None
    await mark_trial_used(settings.database_path, user.id)
    await send_granted_access(message, tariff, subscription)
    return True


async def show_tariffs(message: Message) -> None:
    await message.answer(
        "<b>🚀 Получение доступа</b>\n\n"
        "Выберите вариант доступа:\n\n"
        "🎁 Пробный — 1 день — бесплатно\n"
        "🗓 1 месяц — 50 ₽\n"
        "🗓 3 месяца — 130 ₽\n"
        "🗓 6 месяцев — 240 ₽\n"
        "🗓 12 месяцев — 450 ₽\n\n"
        "Цены указаны в рублях.",
        reply_markup=client_tariff_keyboard(),
    )


async def send_support_request(
    message: Message,
    state: FSMContext | None = None,
    user_override: User | None = None,
) -> None:
    settings = get_settings()
    user = user_override or message.from_user
    contact = settings.support_contact.strip()
    url = support_url(contact)

    if state is not None:
        await state.clear()

    if url:
        await message.answer(
            "<b>💬 Поддержка</b>\n\n"
            f"Контакт: {escape(contact)}\n\n"
            "Нажмите кнопку ниже, чтобы открыть чат с поддержкой.",
            reply_markup=support_keyboard(url),
        )
        return

    if settings.admin_id and user:
        username = f"@{user.username}" if user.username else "не указан"
        full_name = user.full_name or "не указано"

        try:
            await message.bot.send_message(
                settings.admin_id,
                "<b>💬 Новое обращение в поддержку</b>\n\n"
                f"👤 Клиент: {escape(full_name)}\n"
                f"🔗 Username: {escape(username)}\n"
                f"🆔 Telegram ID: <code>{user.id}</code>\n\n"
                "Клиент нажал кнопку поддержки.",
            )
        except Exception:
            logger.exception(
                "event=support_admin_notify_failed",
                telegram_id=user.id,
            )
            await message.answer(
                "Поддержка временно недоступна, попробуйте позже",
                reply_markup=client_menu(),
            )
            return

        await message.answer(
            "<b>✅ Заявка отправлена</b>\n\n"
            "📝 Напишите ваш вопрос прямо в этот чат — оператор ответит здесь же.",
            reply_markup=client_menu(),
        )
        if state is not None:
            await state.set_state(ClientSupportStates.waiting_message)
        return

    await message.answer(
        "Поддержка временно не указана.",
        reply_markup=client_menu(),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    settings = get_settings()
    user = message.from_user
    if user is None:
        return

    await state.clear()
    await upsert_user(
        settings.database_path,
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )

    latest = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )
    full_name = escape(user.full_name)

    if (
        latest is not None
        and latest["status"] == ACTIVE_STATUS
        and from_db_datetime(latest["expires_at"]) > utc_now()
    ):
        text = (
            f"<b>👋 С возвращением, {full_name}!</b>\n\n"
            "✅ Доступ активен\n"
            f"⏳ Осталось дней: {days_left(latest['expires_at'])} "
            f"(до {format_datetime(latest['expires_at'])})\n\n"
            "Выберите действие:"
        )
    elif latest is not None:
        text = (
            f"<b>👋 С возвращением, {full_name}.</b>\n\n"
            "🔒 Ваш доступ неактивен.\n"
            "🔁 Выберите тариф, чтобы продлить доступ.\n\n"
            "Выберите действие:"
        )
    else:
        text = (
            "<b>👋 Добро пожаловать в Van24 MTProto</b>\n\n"
            "Здесь вы можете:\n\n"
            "• 🚀 получить пробный доступ или выбрать тариф\n"
            "• 🔑 получить свои ключи подключения\n"
            "• 📅 проверить срок действия подписки\n"
            "• 🆘 обратиться в поддержку\n\n"
            "Нажмите /start или используйте меню ниже."
        )

    await message.answer(text, reply_markup=client_menu())


@router.message(F.text == TRY_FREE_BUTTON)
async def try_free(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    await state.clear()
    await grant_free_trial(message, user)


@router.message(
    (F.text == BUY_BUTTON)
    | (F.text == OLD_CART_BUY_BUTTON)
    | (F.text == OLD_BUY_BUTTON)
    | (F.text == LEGACY_BUY_BUTTON)
)
async def buy_access(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_tariffs(message)


@router.callback_query(F.data == "client_back")
async def client_back(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await message.answer("🏠 Главное меню", reply_markup=client_menu())


@router.callback_query(F.data == "client_try_free")
async def try_free_inline(callback: CallbackQuery) -> None:
    message = callback.message
    user = callback.from_user
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    granted = await grant_free_trial(message, user)
    if granted is None:
        await callback.answer("Сервис временно недоступен.", show_alert=True)
        return
    await callback.answer(
        "Доступ выдан" if granted else "Пробный день уже использован.",
        show_alert=not granted,
    )


@router.callback_query(F.data == "client_buy")
async def buy_access_inline(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await show_tariffs(message)


@router.callback_query(F.data.startswith("client_tariff_disabled:"))
async def disabled_client_tariff(callback: CallbackQuery) -> None:
    await callback.answer(
        "Этот тариф пока в разработке. Сейчас доступны пробный период и 1 месяц.",
        show_alert=True,
    )


@router.callback_query(F.data == "client_support_inline")
async def support_inline(callback: CallbackQuery, state: FSMContext) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await send_support_request(message, state=state, user_override=callback.from_user)


@router.message(F.text == INSTRUCTION_BUTTON)
async def instruction(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выберите устройство:", reply_markup=instructions_menu())


@router.callback_query(F.data == "client_instruction")
async def instruction_inline(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await message.answer("Выберите устройство:", reply_markup=instructions_menu())


@router.callback_query(F.data.in_(set(INSTRUCTION_TEXTS.keys())))
async def instruction_device(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await message.answer(INSTRUCTION_TEXTS[callback.data or ""])


@router.message((F.text == HELP_BUTTON) | (F.text == SUPPORT_BUTTON))
async def help_section(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        HELP_CHECKLIST_TEXT
        + "\n\n<b>Связаться с поддержкой:</b>\n"
        "Нажмите кнопку ниже.",
        reply_markup=help_menu(),
    )


@router.message(F.text == CLIENT_SUPPORT_BUTTON)
async def client_support(message: Message, state: FSMContext) -> None:
    await send_support_request(message, state=state)


@router.callback_query(F.data == "client_troubleshoot")
async def troubleshoot(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await message.answer(HELP_CHECKLIST_TEXT, reply_markup=help_menu())


@router.callback_query(F.data.startswith("pay_request:"))
async def pay_request(callback: CallbackQuery) -> None:
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
    try:
        tariff = get_tariff(int(parts[1])) if len(parts) == 2 else None
    except (TypeError, ValueError):
        tariff = None

    if tariff is None or tariff.price == 0:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    if not settings.payments_enabled:
        await callback.answer("Продажа временно недоступна.", show_alert=True)
        await message.answer(SALES_DISABLED_TEXT, reply_markup=client_menu())
        return

    allowed, retry_after = check_action_rate(
        user.id, "pay_request", limit=5, window_seconds=3600
    )
    if not allowed:
        await callback.answer(
            f"Слишком много заявок. Подождите {format_retry_after(retry_after)}.",
            show_alert=True,
        )
        return

    if settings.admin_id is None:
        await callback.answer("Оператор временно недоступен.", show_alert=True)
        await message.answer(
            "Не удалось отправить заявку. Пожалуйста, свяжитесь с поддержкой.",
            reply_markup=client_menu(),
        )
        return

    username = f"@{user.username}" if user.username else "-"
    price_text = f"{tariff.price} ₽" if tariff.price is not None else "-"
    await message.bot.send_message(
        settings.admin_id,
        "<b>💳 Новая заявка на оплату</b>\n\n"
        f"👤 Клиент: {escape(user.full_name or '-')}\n"
        f"🔗 Username: {escape(username)}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"📦 Тариф: {escape(tariff.title)}\n"
        f"💵 Цена: {price_text}",
        reply_markup=admin_pay_request_keyboard(user.id, tariff.days),
    )

    if hasattr(message, "edit_reply_markup"):
        await message.edit_reply_markup(reply_markup=None)
    await callback.answer("Заявка отправлена")
    await message.answer(
        "<b>✅ Заявка отправлена оператору</b>\n\n"
        "Оператор проверит оплату и ответит в течение 5–10 минут.",
        reply_markup=client_menu(),
    )


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

    is_free_trial = tariff.days == 1 and tariff.price == 0
    if not is_free_trial and not settings.payments_enabled:
        await callback.answer("Продажа временно недоступна.", show_alert=True)
        await message.answer(SALES_DISABLED_TEXT, reply_markup=client_menu())
        return

    auto_free_enabled = (
        settings.PAYMENT_MODE == "auto_free" and settings.test_auto_issue_access
    )

    if not is_free_trial and not auto_free_enabled:
        payment_notice = ""
        if settings.PAYMENT_MODE in {"stars", "crypto"}:
            payment_notice = (
                "💳 Этот способ оплаты пока в разработке — временно через оператора.\n\n"
            )

        await callback.answer()
        await message.answer(
            payment_notice
            + f"<b>📦 Тариф: {escape(tariff.title)}</b>\n\n"
            f"Что вы получаете: личный приватный MTProto-ключ на {tariff.days} дней, "
            "отдельный от других клиентов.\n"
            "Оплата: через оператора, ответ 5–10 минут.\n\n"
            "Нажмите кнопку ниже, чтобы оформить.",
            reply_markup=manual_pay_keyboard(tariff.days),
        )
        return

    if is_free_trial and await has_used_trial(settings.database_path, user.id):
        await callback.answer("Пробный день уже использован.", show_alert=True)
        await message.answer(
            "🎁 Пробный день уже использован.\n\n"
            "Чтобы пользоваться дальше — выберите тариф.",
            reply_markup=try_or_buy_keyboard(),
        )
        return

    try:
        subscription = await issue_access(user.id, tariff)
    except Exception:
        logger.exception(
            "event=client_issue_access_failed",
            telegram_id=user.id,
        )
        await callback.answer("Сервис временно недоступен.", show_alert=True)
        await message.answer(
            PROXY_TEMPORARILY_UNAVAILABLE_TEXT,
            reply_markup=client_menu(),
        )
        return
    if is_free_trial:
        await mark_trial_used(settings.database_path, user.id)

    await callback.answer("Доступ выдан")
    await send_granted_access(message, tariff, subscription)


async def send_my_link(message: Message, telegram_id: int) -> None:
    settings = get_settings()
    subscription = await get_active_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )

    if subscription is None or not subscription.get("secret"):
        await message.answer(
            "<b>🔒 Доступ пока не активен</b>\n\n"
            "🎁 Попробуйте 1 день бесплатно или выберите тариф.",
            reply_markup=try_or_buy_keyboard(),
        )
        return

    link = build_primary_link(subscription["secret"])

    await message.answer(
        "<b>🔑 Мои ключи</b>\n\n"
        f"⏳ Действует до: {format_datetime(subscription['expires_at'])}\n"
        f"✅ Осталось дней: {days_left(subscription['expires_at'])}\n\n"
        f"{PROXY_CLEANUP_NOTICE}",
        reply_markup=connect_keyboard(link),
    )


@router.message(
    (F.text == CONNECT_BUTTON)
    | (F.text == MY_PROXY_BUTTON)
    | (F.text == OLD_MY_PROXY_BUTTON)
    | (F.text == OLD_MY_PROXIES_BUTTON)
    | (F.text == MY_LINK_BUTTON)
    | (F.text == OLD_MY_LINK_BUTTON)
)
async def my_link(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    await state.clear()
    await send_my_link(message, user.id)


@router.callback_query(F.data == "client_connect")
async def my_link_inline(callback: CallbackQuery) -> None:
    message = callback.message
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    await callback.answer()
    await send_my_link(message, callback.from_user.id)


@router.message(
    (F.text == MY_SUBSCRIPTION_BUTTON)
    | (F.text == OLD_MY_SUBSCRIPTION_BUTTON)
    | (F.text == DAYS_LEFT_BUTTON)
    | (F.text == OLD_DAYS_LEFT_BUTTON)
)
async def show_days_left(message: Message, state: FSMContext) -> None:
    settings = get_settings()
    user = message.from_user
    if user is None:
        return

    await state.clear()
    subscription = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )

    if subscription is None:
        await message.answer(
            "<b>🔒 Доступ пока не активен.</b>\n\n"
            "🎁 Попробуйте 1 день бесплатно или выберите тариф.",
            reply_markup=my_subscription_keyboard(False),
        )
        return

    tariff_days = subscription["tariff_days"]
    has_active = (
        subscription["status"] == ACTIVE_STATUS
        and from_db_datetime(subscription["expires_at"]) > utc_now()
    )
    dl = days_left(subscription["expires_at"]) if has_active else 0
    used = max(0, tariff_days - dl)
    filled = min(10, used * 10 // max(1, tariff_days))
    bar = "█" * filled + "░" * (10 - filled)
    percent = filled * 10
    try:
        tariff_title = get_tariff(tariff_days).title
    except ValueError:
        tariff_title = f"{tariff_days} дней"
    status_text = "✅ Статус: активна" if has_active else "🔒 Статус: доступ неактивен"

    await message.answer(
        "<b>📅 Моя подписка</b>\n\n"
        f"{status_text}\n"
        f"📦 Тариф: {escape(tariff_title)}\n"
        f"📅 Истекает: {format_datetime(subscription['expires_at'])}\n"
        f"✅ Осталось дней: {dl}\n\n"
        f"[<code>{bar}</code>] {percent}% использовано",
        reply_markup=my_subscription_keyboard(has_active),
    )


@router.callback_query(F.data == "client_alt_links")
async def send_alt_links(callback: CallbackQuery) -> None:
    message = callback.message
    user = callback.from_user
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    settings = get_settings()
    subscription = await get_active_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )
    if subscription is None or not subscription.get("secret"):
        await callback.answer("Сначала оформите доступ.", show_alert=True)
        return

    picker = get_picker()
    ordered = picker.pick_alternatives(n=3) or settings.fallback_tls_domains[:3]
    secret = subscription["secret"]
    links = [
        (d, build_tls_proxy_link(settings.server_host, settings.proxy_port, secret, d))
        for d in ordered
    ]
    if not links:
        links = build_alternative_links(secret, max_count=3)

    text_lines = [
        "<b>🌐 Альтернативные ссылки</b>",
        "",
        "Попробуйте варианты по очереди — каждая использует другой домен TLS-маскировки. "
        "Если один заблокирован вашим оператором, другой обычно работает.",
        "",
    ]
    for idx, (domain, link) in enumerate(links, 1):
        text_lines.append(f"<b>Вариант {idx}</b> (домен {escape(domain)}):")
        text_lines.append(f"<code>{escape(link)}</code>")
        text_lines.append("")
    text_lines.append("💡 Удерживайте ссылку, чтобы скопировать её вручную.")

    await callback.answer()
    await message.answer(
        "\n".join(text_lines).rstrip(),
        reply_markup=alt_links_keyboard(links),
    )


@router.callback_query(F.data == "client_rotate_key")
async def rotate_client_key(callback: CallbackQuery) -> None:
    settings = get_settings()
    message = callback.message
    user = callback.from_user
    if message is None or not hasattr(message, "answer"):
        await callback.answer("Откройте меню командой /start.", show_alert=True)
        return

    subscription = await get_active_subscription_by_telegram_id(
        settings.database_path,
        user.id,
    )
    if subscription is None:
        await callback.answer("Сначала оформите доступ.", show_alert=True)
        return

    last_rotated_at = await get_last_secret_rotated_at(settings.database_path, user.id)
    if last_rotated_at is not None:
        seconds_left = 600 - (utc_now() - last_rotated_at).total_seconds()
        if seconds_left > 0:
            minutes_left = max(1, int((seconds_left + 59) // 60))
            await callback.answer(
                "Ключ уже обновлялся недавно. "
                f"Попробуйте через {minutes_left} минут.",
                show_alert=True,
            )
            return

    allowed, retry_after = check_action_rate(
        user.id, "rotate_secret", limit=5, window_seconds=86400
    )
    if not allowed:
        await callback.answer(
            f"Лимит обновлений ключа на сегодня исчерпан. Попробуйте через {format_retry_after(retry_after)}.",
            show_alert=True,
        )
        return

    try:
        secret = await rotate_secret(client_id_for(user.id))
    except RotateCooldownError as exc:
        await callback.answer(
            f"Подождите {int(exc.retry_after) + 1} сек перед повторным обновлением.",
            show_alert=True,
        )
        return
    except CircuitOpenError:
        await callback.answer(
            "Сервис временно недоступен, попробуйте через минуту.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception(
            "event=rotate_secret_failed",
            telegram_id=user.id,
        )
        await callback.answer("Не удалось обновить ключ.", show_alert=True)
        return

    subscription = await update_latest_subscription_secret_by_telegram_id(
        settings.database_path,
        user.id,
        secret,
    )
    if subscription is None:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await mark_secret_rotated(settings.database_path, user.id)

    link = build_primary_link(secret)
    await callback.answer("Ключ обновлён")
    await message.answer(
        "🔄 Ключ обновлён. Старый перестанет работать в течение ~5 секунд.\n\n"
        f"{PROXY_CLEANUP_NOTICE}\n\n"
        "💡 Если новая ссылка тоже не работает — нажмите «🌐 Альтернативные ссылки» "
        "и попробуйте другой домен TLS-маскировки.",
        reply_markup=connect_keyboard(link),
    )


@router.message(ClientSupportStates.waiting_message, F.text)
async def relay_client_to_admin(message: Message, state: FSMContext) -> None:
    try:
        settings = get_settings()
        user = message.from_user
        if settings.admin_id is None or user is None or user.id == settings.admin_id:
            await state.clear()
            return
        if message.text.startswith("/"):
            await state.clear()
            await message.answer("Выберите действие в меню ниже.", reply_markup=client_menu())
            return

        allowed, retry_after = check_action_rate(
            user.id, "support_relay", limit=10, window_seconds=3600
        )
        if not allowed:
            await message.answer(
                "Вы отправили слишком много сообщений подряд. "
                f"Подождите {format_retry_after(retry_after)} и продолжите диалог."
            )
            return

        username = f"@{escape(user.username)}" if user.username else "без username"
        header_msg = await message.bot.send_message(
            settings.admin_id,
            f"💬 От клиента {escape(user.full_name)} {username} · ID <code>{user.id}</code>",
        )
        await record_support_thread(
            settings.database_path,
            header_msg.message_id,
            user.id,
        )
        copied_message = await message.bot.copy_message(
            chat_id=settings.admin_id,
            from_chat_id=user.id,
            message_id=message.message_id,
        )
        await record_support_thread(
            settings.database_path,
            copied_message.message_id,
            user.id,
        )
        await state.clear()
        await message.answer(
            "✅ Сообщение отправлено оператору.\n\nВыберите действие в меню ниже.",
            reply_markup=client_menu(),
        )
    except Exception:
        logger.exception("event=relay_client_to_admin_failed")
        await state.clear()
        try:
            await message.answer(
                "❌ Не получилось передать сообщение оператору. "
                "Попробуйте позже.",
                reply_markup=client_menu(),
            )
        except Exception:
            pass


@router.message(StateFilter(None), F.text)
async def unknown_client_text(message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    if user is None or user.id == settings.admin_id or message.text.startswith("/"):
        return
    await message.answer("Выберите действие в меню ниже.", reply_markup=client_menu())
