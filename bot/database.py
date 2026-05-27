from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite


ACTIVE_STATUS = "active"
DISABLED_STATUS = "disabled"
EXPIRED_STATUS = "expired"


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_db_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def from_db_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def connect(database_path: Path) -> aiosqlite.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(database_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


@asynccontextmanager
async def open_db(database_path: Path):
    db = await connect(database_path)
    try:
        yield db
    finally:
        await db.close()


async def init_db(database_path: Path) -> None:
    async with open_db(database_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                full_name TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                secret TEXT,
                tariff_days INTEGER NOT NULL,
                starts_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                reminder_3d_sent INTEGER NOT NULL DEFAULT 0,
                reminder_1d_sent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER,
                currency TEXT,
                provider TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS support_threads (
                admin_message_id INTEGER PRIMARY KEY,
                client_telegram_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS support_bot_threads (
                admin_message_id INTEGER PRIMARY KEY,
                client_telegram_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_telegram_id
                ON users(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
                ON subscriptions(user_id);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_status_expires
                ON subscriptions(status, expires_at);
            """
        )
        for statement in (
            "ALTER TABLE users ADD COLUMN trial_used INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN trial_used_at TEXT",
            "ALTER TABLE subscriptions ADD COLUMN last_secret_rotated_at TEXT",
        ):
            try:
                await db.execute(statement)
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        await db.execute(
            """
            UPDATE users SET trial_used = 1 WHERE id IN
                (SELECT DISTINCT user_id FROM subscriptions WHERE tariff_days = 1)
            """
        )
        await db.commit()


async def record_support_thread(
    database_path: Path,
    admin_message_id: int,
    client_telegram_id: int,
) -> None:
    async with open_db(database_path) as db:
        await db.execute(
            """
            INSERT INTO support_threads (
                admin_message_id, client_telegram_id, created_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(admin_message_id) DO UPDATE SET
                client_telegram_id = excluded.client_telegram_id,
                created_at = excluded.created_at
            """,
            (admin_message_id, client_telegram_id, to_db_datetime(utc_now())),
        )
        await db.commit()


async def resolve_support_thread(
    database_path: Path,
    admin_message_id: int,
) -> int | None:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            "SELECT client_telegram_id FROM support_threads WHERE admin_message_id = ?",
            (admin_message_id,),
        )
        row = await cursor.fetchone()
        return int(row["client_telegram_id"]) if row is not None else None


async def record_support_bot_thread(
    database_path: Path,
    admin_message_id: int,
    client_telegram_id: int,
) -> None:
    async with open_db(database_path) as db:
        await db.execute(
            """
            INSERT INTO support_bot_threads (
                admin_message_id, client_telegram_id, created_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(admin_message_id) DO UPDATE SET
                client_telegram_id = excluded.client_telegram_id,
                created_at = excluded.created_at
            """,
            (admin_message_id, client_telegram_id, to_db_datetime(utc_now())),
        )
        await db.commit()


async def resolve_support_bot_thread(
    database_path: Path,
    admin_message_id: int,
) -> int | None:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            "SELECT client_telegram_id FROM support_bot_threads WHERE admin_message_id = ?",
            (admin_message_id,),
        )
        row = await cursor.fetchone()
        return int(row["client_telegram_id"]) if row is not None else None


def row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


async def upsert_user(
    database_path: Path,
    telegram_id: int,
    username: str | None,
    full_name: str,
) -> dict[str, Any]:
    now = to_db_datetime(utc_now())
    async with open_db(database_path) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (telegram_id, username, full_name, now),
        )
        await db.commit()
        user = await get_user_by_telegram_id(database_path, telegram_id)
        assert user is not None
        return user


async def get_user_by_telegram_id(
    database_path: Path,
    telegram_id: int,
) -> dict[str, Any] | None:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return row_to_dict(await cursor.fetchone())


async def has_used_trial(database_path: Path, telegram_id: int) -> bool:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            "SELECT trial_used FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        return bool(row is not None and row["trial_used"])


async def mark_trial_used(database_path: Path, telegram_id: int) -> None:
    async with open_db(database_path) as db:
        await db.execute(
            """
            UPDATE users
            SET trial_used = 1, trial_used_at = ?
            WHERE telegram_id = ?
            """,
            (to_db_datetime(utc_now()), telegram_id),
        )
        await db.commit()


async def get_recent_users(database_path: Path, limit: int = 10) -> list[dict[str, Any]]:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            """
            SELECT
                u.telegram_id,
                u.username,
                u.full_name,
                COALESCE(s.status, 'none') AS subscription_status,
                s.expires_at
            FROM users u
            LEFT JOIN subscriptions s ON s.id = (
                SELECT id FROM subscriptions
                WHERE user_id = u.id
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
            )
            ORDER BY datetime(u.created_at) DESC, u.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_latest_subscription_by_telegram_id(
    database_path: Path,
    telegram_id: int,
) -> dict[str, Any] | None:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            """
            SELECT s.*, u.telegram_id
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE u.telegram_id = ?
            ORDER BY datetime(s.created_at) DESC, s.id DESC
            LIMIT 1
            """,
            (telegram_id,),
        )
        return row_to_dict(await cursor.fetchone())


async def get_active_subscription_by_telegram_id(
    database_path: Path,
    telegram_id: int,
) -> dict[str, Any] | None:
    subscription = await get_latest_subscription_by_telegram_id(database_path, telegram_id)
    if subscription is None or subscription["status"] != ACTIVE_STATUS:
        return None
    if from_db_datetime(subscription["expires_at"]) <= utc_now():
        return None
    return subscription


async def create_subscription(
    database_path: Path,
    telegram_id: int,
    secret: str,
    tariff_days: int,
    starts_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    user = await get_user_by_telegram_id(database_path, telegram_id)
    if user is None:
        raise ValueError("Пользователь не найден. Сначала он должен нажать /start.")

    now = to_db_datetime(utc_now())
    async with open_db(database_path) as db:
        await db.execute(
            """
            UPDATE subscriptions
            SET status = ?
            WHERE user_id = ? AND status = ?
            """,
            (DISABLED_STATUS, user["id"], ACTIVE_STATUS),
        )
        await db.execute(
            """
            INSERT INTO subscriptions (
                user_id, secret, tariff_days, starts_at, expires_at, status,
                reminder_3d_sent, reminder_1d_sent, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                user["id"],
                secret,
                tariff_days,
                to_db_datetime(starts_at),
                to_db_datetime(expires_at),
                ACTIVE_STATUS,
                now,
                now,
            ),
        )
        await db.commit()

    subscription = await get_latest_subscription_by_telegram_id(database_path, telegram_id)
    assert subscription is not None
    return subscription


async def extend_subscription(
    database_path: Path,
    telegram_id: int,
    secret: str,
    tariff_days: int,
    starts_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    user = await get_user_by_telegram_id(database_path, telegram_id)
    if user is None:
        raise ValueError("Пользователь не найден. Сначала он должен нажать /start.")

    latest = await get_latest_subscription_by_telegram_id(database_path, telegram_id)
    if latest is None:
        return await create_subscription(
            database_path, telegram_id, secret, tariff_days, starts_at, expires_at
        )

    now = to_db_datetime(utc_now())
    async with open_db(database_path) as db:
        await db.execute(
            """
            UPDATE subscriptions
            SET
                secret = ?,
                tariff_days = ?,
                starts_at = ?,
                expires_at = ?,
                status = ?,
                reminder_3d_sent = 0,
                reminder_1d_sent = 0,
                updated_at = ?
            WHERE id = ?
            """,
            (
                secret,
                tariff_days,
                to_db_datetime(starts_at),
                to_db_datetime(expires_at),
                ACTIVE_STATUS,
                now,
                latest["id"],
            ),
        )
        await db.commit()

    subscription = await get_latest_subscription_by_telegram_id(database_path, telegram_id)
    assert subscription is not None
    return subscription


async def mark_subscription_status(
    database_path: Path,
    subscription_id: int,
    status: str,
) -> None:
    async with open_db(database_path) as db:
        await db.execute(
            """
            UPDATE subscriptions
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, to_db_datetime(utc_now()), subscription_id),
        )
        await db.commit()


async def delete_user_cascade(database_path: Path, telegram_id: int) -> None:
    async with open_db(database_path) as db:
        await db.execute(
            "DELETE FROM subscriptions WHERE user_id = "
            "(SELECT id FROM users WHERE telegram_id = ?)",
            (telegram_id,),
        )
        await db.execute(
            "DELETE FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()


async def disable_subscription_by_telegram_id(
    database_path: Path,
    telegram_id: int,
) -> dict[str, Any] | None:
    subscription = await get_latest_subscription_by_telegram_id(database_path, telegram_id)
    if subscription is None:
        return None
    await mark_subscription_status(database_path, subscription["id"], DISABLED_STATUS)
    return subscription


async def update_latest_subscription_secret_by_telegram_id(
    database_path: Path,
    telegram_id: int,
    secret: str,
) -> dict[str, Any] | None:
    subscription = await get_latest_subscription_by_telegram_id(database_path, telegram_id)
    if subscription is None:
        return None

    async with open_db(database_path) as db:
        await db.execute(
            """
            UPDATE subscriptions
            SET secret = ?, updated_at = ?
            WHERE id = ?
            """,
            (secret, to_db_datetime(utc_now()), subscription["id"]),
        )
        await db.commit()

    return await get_latest_subscription_by_telegram_id(database_path, telegram_id)


async def get_last_secret_rotated_at(
    database_path: Path,
    telegram_id: int,
) -> datetime | None:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            """
            SELECT s.last_secret_rotated_at
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE u.telegram_id = ?
            ORDER BY datetime(s.created_at) DESC, s.id DESC
            LIMIT 1
            """,
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if row is None or row["last_secret_rotated_at"] is None:
            return None
        return from_db_datetime(row["last_secret_rotated_at"])


async def mark_secret_rotated(database_path: Path, telegram_id: int) -> None:
    async with open_db(database_path) as db:
        await db.execute(
            """
            UPDATE subscriptions
            SET last_secret_rotated_at = ?
            WHERE id = (
                SELECT s.id
                FROM subscriptions s
                JOIN users u ON u.id = s.user_id
                WHERE u.telegram_id = ?
                ORDER BY datetime(s.created_at) DESC, s.id DESC
                LIMIT 1
            )
            """,
            (to_db_datetime(utc_now()), telegram_id),
        )
        await db.commit()


async def get_expired_active_subscriptions(
    database_path: Path,
) -> list[dict[str, Any]]:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            """
            SELECT s.*, u.telegram_id
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE s.status = ? AND s.expires_at <= ?
            """,
            (ACTIVE_STATUS, to_db_datetime(utc_now())),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_active_subscriptions_for_reminders(
    database_path: Path,
) -> list[dict[str, Any]]:
    async with open_db(database_path) as db:
        cursor = await db.execute(
            """
            SELECT s.*, u.telegram_id
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE s.status = ?
            """,
            (ACTIVE_STATUS,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def mark_reminder_sent(
    database_path: Path,
    subscription_id: int,
    days: int,
) -> None:
    column = "reminder_3d_sent" if days == 3 else "reminder_1d_sent"
    async with open_db(database_path) as db:
        await db.execute(
            f"UPDATE subscriptions SET {column} = 1, updated_at = ? WHERE id = ?",
            (to_db_datetime(utc_now()), subscription_id),
        )
        await db.commit()


async def get_stats(database_path: Path) -> dict[str, int]:
    now = to_db_datetime(utc_now())
    async with open_db(database_path) as db:
        total_users_row = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        active_row = await (
            await db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE status = ? AND expires_at > ?",
                (ACTIVE_STATUS, now),
            )
        ).fetchone()
        expired_row = await (
            await db.execute(
                """
                SELECT COUNT(*) FROM subscriptions
                WHERE status = ? OR (status = ? AND expires_at <= ?)
                """,
                (EXPIRED_STATUS, ACTIVE_STATUS, now),
            )
        ).fetchone()
        disabled_row = await (
            await db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE status = ?",
                (DISABLED_STATUS,),
            )
        ).fetchone()
        assert total_users_row is not None
        assert active_row is not None
        assert expired_row is not None
        assert disabled_row is not None
    return {
        "total_users": total_users_row[0],
        "active": active_row[0],
        "expired": expired_row[0],
        "disabled": disabled_row[0],
    }
