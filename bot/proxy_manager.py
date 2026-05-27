"""Business-level proxy management on top of TeleMT HTTP API.

Public API is async. CLI (`python proxy_manager.py …`) wraps it via asyncio.run
so manage.sh and ad-hoc admin commands keep working unchanged.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from config import get_settings
from logging_setup import get_logger
from telemt_client import (
    CircuitOpenError,
    ClientNotFoundError,
    TeleMTError,
    api_call,
    close_telemt,
    get_circuit_state,
    is_available,
)


logger = get_logger(__name__)


SECRET_RE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# Re-export for callers (admin.py, client.py, subscriptions.py).
__all__ = [
    "CircuitOpenError",
    "ClientNotFoundError",
    "TeleMTError",
    "RotateCooldownError",
    "build_alternative_links",
    "build_tls_proxy_link",
    "create_secret",
    "delete_secret",
    "generate_secret",
    "get_circuit_state",
    "get_link",
    "is_available",
    "list_clients",
    "mask_secret",
    "pick_primary_tls_domain",
    "rotate_secret",
    "rotate_telegram_secret",
    "validate_client_id",
    "validate_secret",
]


class RotateCooldownError(TeleMTError):
    """Raised when a rotate is requested before the per-client cooldown elapses."""

    def __init__(self, retry_after: float, client_id: str) -> None:
        super().__init__(
            f"rotate cooldown active for {client_id}: retry in {retry_after:.1f}s"
        )
        self.retry_after = retry_after
        self.client_id = client_id


_rotate_last: dict[str, float] = {}
_rotate_lock: asyncio.Lock | None = None


def _get_rotate_lock() -> asyncio.Lock:
    global _rotate_lock
    if _rotate_lock is None:
        _rotate_lock = asyncio.Lock()
    return _rotate_lock


def mask_secret(secret: str) -> str:
    if not secret or len(secret) <= 10:
        return "***"
    return f"{secret[:6]}...{secret[-4:]}"


def generate_secret() -> str:
    return secrets.token_hex(16)


def validate_client_id(client_id: str) -> None:
    if not client_id or Path(client_id).name != client_id:
        raise ValueError("client_id должен быть валидным именем")
    if not CLIENT_RE.fullmatch(client_id):
        raise ValueError(
            "client_id должен содержать только буквы, цифры, _, ., - (1-64 символа)"
        )


def validate_secret(secret: str) -> None:
    if not SECRET_RE.fullmatch(secret.lower()):
        raise ValueError("secret должен быть 32 символа в hex (0-9, a-f)")


def build_tls_proxy_link(
    server_host: str, proxy_port: int, secret: str, tls_domain: str
) -> str:
    validate_secret(secret)
    normalized_domain = tls_domain.strip().lower().encode("idna").decode("ascii")
    if not normalized_domain or "/" in normalized_domain or " " in normalized_domain:
        raise ValueError("tls_domain должен быть валидным доменным именем")
    # Fake-TLS public secret format is ee + user secret + SNI domain encoded as hex.
    public_secret = f"ee{secret.lower()}{normalized_domain.encode('ascii').hex()}"
    params = urlencode(
        {
            "server": server_host,
            "port": str(proxy_port),
            "secret": public_secret,
        }
    )
    return f"tg://proxy?{params}"


def pick_primary_tls_domain() -> str:
    """Best-known TLS domain via the picker; falls back to configured primary.

    Safe to call from any context — never raises.
    """
    settings = get_settings()
    try:
        from tls_domains import get_picker

        chosen = get_picker().pick_best()
        if chosen:
            return chosen
    except Exception as exc:
        logger.debug("event=picker_pick_failed", error=str(exc))
    return settings.tls_domain


def build_alternative_links(
    secret: str,
    *,
    max_count: int = 3,
    preferred_domain: str | None = None,
    prefer_picker: bool = True,
) -> list[tuple[str, str]]:
    """Return ordered (domain, link) pairs across all known TLS domains.

    Ordering:
      1. `preferred_domain` (if given and valid) is placed first.
      2. Else if `prefer_picker=True`, picker's ranking is used.
      3. Else configured order from `settings.fallback_tls_domains`.

    The result always contains at least one entry (the primary TLS domain),
    even if probing has not run yet or the picker is unavailable.
    """
    settings = get_settings()
    max_count = max(1, max_count)
    configured = list(settings.fallback_tls_domains)
    ordered: list[str] = []

    if prefer_picker:
        try:
            from tls_domains import get_picker

            ranked = get_picker().pick_alternatives(n=max(max_count, len(configured)))
            ordered = [d for d in ranked if d in configured]
        except Exception as exc:
            logger.debug("event=picker_alternatives_failed", error=str(exc))
            ordered = []

    if not ordered:
        ordered = configured

    for d in configured:
        if d not in ordered:
            ordered.append(d)

    if preferred_domain and preferred_domain in ordered:
        ordered.remove(preferred_domain)
        ordered.insert(0, preferred_domain)

    ordered = ordered[:max_count] or [settings.tls_domain]
    return [
        (
            d,
            build_tls_proxy_link(settings.server_host, settings.proxy_port, secret, d),
        )
        for d in ordered
    ]


def _normalize_secret_str(raw: str) -> str | None:
    """Return the private 32-hex secret from raw or public DD/EE value."""
    s = raw.strip().lower()
    if s.startswith(("ee", "dd")):
        return s[2:34] if SECRET_RE.fullmatch(s[2:34]) else None
    return s if SECRET_RE.fullmatch(s) else None


def _iter_api_values(data: Any, *, max_nodes: int = 1024) -> Iterator[Any]:
    """Breadth-first walk through JSON-like API data with a hard safety limit."""
    pending: deque[Any] = deque([data])
    visited: set[int] = set()
    processed = 0
    while pending and processed < max_nodes:
        value = pending.popleft()
        processed += 1
        yield value
        if isinstance(value, (dict, list, tuple)):
            object_id = id(value)
            if object_id in visited:
                continue
            visited.add(object_id)
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)


def _tls_link_secret(raw: str) -> str | None:
    """Extract a private secret only from a valid Fake-TLS proxy URL."""
    value = raw.strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    is_tg_link = parsed.scheme.lower() == "tg" and parsed.netloc.lower() == "proxy"
    is_web_link = (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower() == "t.me"
        and parsed.path.rstrip("/").lower() == "/proxy"
    )
    if not (is_tg_link or is_web_link):
        return None
    values = parse_qs(parsed.query).get("secret", [])
    if not values:
        return None
    public_secret = values[0].strip().lower()
    if not public_secret.startswith("ee") or len(public_secret) <= 34:
        return None
    domain_hex = public_secret[34:]
    try:
        domain = bytes.fromhex(domain_hex).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if not domain or "/" in domain or " " in domain:
        return None
    return _normalize_secret_str(public_secret)


def _extract_secret(data: Any) -> str | None:
    """Pull a secret from any TeleMT envelope, including generated EE links."""
    secret_keys = {
        "secret",
        "client_secret",
        "user_secret",
        "ee_secret",
        "password",
        "key",
    }
    for value in _iter_api_values(data):
        if isinstance(value, dict):
            for key, candidate in value.items():
                if (
                    isinstance(key, str)
                    and key.lower() in secret_keys
                    and isinstance(candidate, str)
                ):
                    normalized = _normalize_secret_str(candidate)
                    if normalized:
                        return normalized
    for value in _iter_api_values(data):
        if isinstance(value, str):
            normalized = _normalize_secret_str(value)
            if normalized:
                return normalized
            normalized = _tls_link_secret(value)
            if normalized:
                return normalized
    return None


async def create_secret(client_id: str, provided_secret: str | None = None) -> str:
    """Create or return an existing TeleMT user; idempotent."""
    validate_client_id(client_id)
    secret = (provided_secret or generate_secret()).lower()
    validate_secret(secret)

    try:
        existing = await api_call("GET", f"/v1/users/{client_id}")
        existing_secret = _extract_secret(existing)
        if existing_secret:
            return existing_secret
    except ClientNotFoundError:
        pass

    await api_call("POST", "/v1/users", {"username": client_id, "secret": secret})
    logger.info("event=secret_created", client_id=client_id, secret=secret)
    return secret


async def delete_secret(client_id: str) -> str:
    """Delete a TeleMT user. Returns the previously-known secret if it could
    be fetched (best-effort)."""
    validate_client_id(client_id)
    settings = get_settings()
    if client_id == settings.telemt_system_user:
        raise ValueError(f"Нельзя удалять системного пользователя {client_id}")

    secret_known = ""
    try:
        existing = await api_call("GET", f"/v1/users/{client_id}")
        secret_known = _extract_secret(existing) or ""
    except ClientNotFoundError:
        raise ClientNotFoundError(f"Клиент {client_id} не найден")

    try:
        await api_call("DELETE", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"Клиент {client_id} не найден")

    logger.info("event=secret_deleted", client_id=client_id, secret=secret_known)
    return secret_known


async def rotate_secret(client_id: str) -> str:
    """Rotate a TeleMT user's secret. Enforces per-client API cooldown."""
    validate_client_id(client_id)
    settings = get_settings()

    async with _get_rotate_lock():
        last = _rotate_last.get(client_id)
        now = time.monotonic()
        if last is not None:
            elapsed = now - last
            if elapsed < settings.telemt_rotate_cooldown:
                raise RotateCooldownError(
                    settings.telemt_rotate_cooldown - elapsed, client_id
                )
        _rotate_last[client_id] = now

    new_secret = generate_secret()
    try:
        resp = await api_call(
            "POST",
            f"/v1/users/{client_id}/rotate-secret",
            {"secret": new_secret},
        )
    except ClientNotFoundError:
        raise ClientNotFoundError(f"Клиент {client_id} не найден")

    actual = _extract_secret(resp) or new_secret
    logger.info("event=secret_rotated", client_id=client_id, secret=actual)
    return actual


async def get_link(client_id: str) -> str:
    """Return a TLS-masked tg://proxy link for a client.

    The returned link is always locally rebuilt using picker order so the CLI
    and bot use the same selected domain. If picker has no usable domain,
    `build_alternative_links` falls back to configured `TLS_DOMAIN`.
    """
    validate_client_id(client_id)
    settings = get_settings()

    try:
        resp = await api_call("GET", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"Клиент {client_id} не найден")

    api_link_found = _extract_tls_link(resp) is not None
    secret = _extract_secret(resp)
    secret_source = "api_response"
    if not secret:
        secret = await create_secret(client_id)
        secret_source = "create_or_existing"

    pairs = build_alternative_links(secret, max_count=1, prefer_picker=True)
    domain, link = pairs[0]
    logger.info(
        "event=link_built",
        client_id=client_id,
        link_source="local_fake_tls",
        prefer_picker=True,
        tls_domain=domain,
        used_configured_fallback=domain == settings.tls_domain,
        api_tls_link_found=api_link_found,
        secret_source=secret_source,
        server_host=settings.server_host,
        proxy_port=settings.proxy_port,
    )
    return link


def _extract_tls_link(resp: Any) -> str | None:
    """Pull a validated EE link from nested envelopes and `links.tls_domains`."""
    for value in _iter_api_values(resp):
        if isinstance(value, str) and _tls_link_secret(value):
            return value.strip()
    return None


async def rotate_telegram_secret(telegram_id: int) -> str:
    """Rotate `tg_<telegram_id>` and sync the new secret into SQLite.

    Wraps `rotate_secret` with a DB-level update of the latest subscription
    (`update_latest_subscription_secret_by_telegram_id`) and a rotated-at
    timestamp (`mark_secret_rotated`). Designed for CLI use from manage.sh
    menu item 10 — keeps SQLite consistent when an admin manually rotates a
    key outside the bot flow.

    Raises:
        ClientNotFoundError: when no such TeleMT user exists.
        TeleMTError: on transport / 5xx after retries.
    """
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        raise ValueError("telegram_id должен быть положительным целым")

    settings = get_settings()
    client_id = f"tg_{telegram_id}"

    new_secret = await rotate_secret(client_id)

    try:
        from database import (
            mark_secret_rotated,
            update_latest_subscription_secret_by_telegram_id,
        )

        updated = await update_latest_subscription_secret_by_telegram_id(
            settings.database_path, telegram_id, new_secret
        )
        if updated is not None:
            await mark_secret_rotated(settings.database_path, telegram_id)
            logger.info(
                "event=telegram_secret_rotated",
                telegram_id=telegram_id,
                db_synced=True,
            )
        else:
            logger.warning(
                "event=telegram_secret_rotated_no_db_record",
                telegram_id=telegram_id,
            )
    except Exception as exc:
        logger.error(
            "event=telegram_secret_db_sync_failed",
            telegram_id=telegram_id,
            error=str(exc),
        )

    return new_secret


async def list_clients() -> dict[str, str]:
    settings = get_settings()
    try:
        resp = await api_call("GET", "/v1/users")
    except (TeleMTError, CircuitOpenError) as exc:
        logger.error("event=list_clients_failed", error=str(exc))
        return {}

    if resp is None:
        return {}

    if isinstance(resp, list):
        users: Any = resp
    elif isinstance(resp, dict):
        users = resp.get("users") or resp
    else:
        return {}

    result: dict[str, str] = {}
    if isinstance(users, list):
        for entry in users:
            if not isinstance(entry, dict):
                continue
            name = entry.get("username") or entry.get("name")
            if not name or name == settings.telemt_system_user:
                continue
            secret = _extract_secret(entry) or ""
            result[name] = secret.lower()
    elif isinstance(users, dict):
        for name, entry in users.items():
            if name == settings.telemt_system_user:
                continue
            if isinstance(entry, str):
                if SECRET_RE.fullmatch(entry.lower()):
                    result[name] = entry.lower()
            else:
                secret = _extract_secret(entry) or ""
                result[name] = secret.lower()
    return result


# ==================== CLI ====================
async def _cli_main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="TeleMT Proxy Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("create", help="Создать секрет")
    p.add_argument("client_id")
    p.add_argument("--secret", help="Задать секрет вручную")

    for cmd in ("delete", "link", "rotate"):
        p = subparsers.add_parser(cmd, help=f"Команда {cmd}")
        p.add_argument("client_id")

    p = subparsers.add_parser(
        "rotate-telegram",
        help="Обновить ключ клиента по Telegram ID + синхронизировать SQLite",
    )
    p.add_argument("telegram_id", type=int)

    subparsers.add_parser("list", help="Список клиентов")

    args = parser.parse_args()
    rc = 0
    try:
        if args.command == "create":
            secret = await create_secret(args.client_id, args.secret)
            print(await get_link(args.client_id))
            print(f"Создан {args.client_id}: {mask_secret(secret)}")
        elif args.command == "delete":
            secret = await delete_secret(args.client_id)
            print(f"Удалён {args.client_id}: {mask_secret(secret)}")
        elif args.command == "rotate":
            secret = await rotate_secret(args.client_id)
            print(await get_link(args.client_id))
            print(f"Обновлён {args.client_id}: {mask_secret(secret)}")
        elif args.command == "rotate-telegram":
            secret = await rotate_telegram_secret(args.telegram_id)
            print(await get_link(f"tg_{args.telegram_id}"))
            print(
                f"Обновлён tg_{args.telegram_id}: {mask_secret(secret)} (SQLite синхронизирован)"
            )
        elif args.command == "link":
            print(await get_link(args.client_id))
        elif args.command == "list":
            users = await list_clients()
            if not users:
                print("Клиентов нет")
            for cid, sec in sorted(users.items()):
                print(f"{cid}: {mask_secret(sec)}")
    except Exception as exc:
        logger.error("event=cli_failed", command=args.command, error=str(exc))
        print(f"ERROR: {exc}")
        rc = 1
    finally:
        await close_telemt()
    return rc


def main() -> int:
    from logging_setup import configure_logging

    configure_logging(level="INFO", fmt="console")
    return asyncio.run(_cli_main())


if __name__ == "__main__":
    raise SystemExit(main())
