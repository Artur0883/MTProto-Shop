"""Business-level proxy management on top of TeleMT HTTP API.

Public API is async. CLI (`python proxy_manager.py …`) wraps it via asyncio.run
so manage.sh and ad-hoc admin commands keep working unchanged.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
    "rotate_secret",
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
    public_secret = f"ee{secret.lower()}"
    params = urlencode(
        {
            "server": server_host,
            "port": str(proxy_port),
            "secret": public_secret,
        }
    )
    return f"tg://proxy?{params}"


def build_alternative_links(
    secret: str, *, max_count: int = 3, preferred_domain: str | None = None
) -> list[tuple[str, str]]:
    """Return ordered (domain, link) pairs across all known TLS domains.

    `preferred_domain`, if provided and valid, is placed first.
    """
    settings = get_settings()
    domains = list(settings.fallback_tls_domains)
    if preferred_domain and preferred_domain in domains:
        domains.remove(preferred_domain)
        domains.insert(0, preferred_domain)
    domains = domains[:max_count]
    return [
        (
            d,
            build_tls_proxy_link(settings.server_host, settings.proxy_port, secret, d),
        )
        for d in domains
    ]


def _extract_secret(data: Any) -> str | None:
    """Pull a 32-hex secret out of various TeleMT response shapes."""
    if data is None:
        return None
    if isinstance(data, str):
        return data.lower() if SECRET_RE.fullmatch(data.lower()) else None
    if isinstance(data, dict):
        for key in ("secret", "client_secret", "password", "key"):
            value = data.get(key)
            if isinstance(value, str) and SECRET_RE.fullmatch(value.lower()):
                return value.lower()
        for nested in ("user", "data", "result", "client"):
            nv = data.get(nested)
            if isinstance(nv, dict):
                found = _extract_secret(nv)
                if found:
                    return found
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

    Prefers TeleMT-supplied TLS links if present; otherwise constructs the link
    locally using the primary TLS domain.
    """
    validate_client_id(client_id)
    settings = get_settings()

    try:
        resp = await api_call("GET", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"Клиент {client_id} не найден")

    api_link = _extract_tls_link(resp)
    if api_link:
        return api_link

    secret = _extract_secret(resp)
    if not secret:
        secret = await create_secret(client_id)
    return build_tls_proxy_link(
        settings.server_host, settings.proxy_port, secret, settings.tls_domain
    )


def _extract_tls_link(resp: Any) -> str | None:
    if not isinstance(resp, dict):
        return None
    candidates = []
    for root in (resp, resp.get("user")):
        if isinstance(root, dict):
            links = root.get("links")
            if isinstance(links, dict):
                candidates.append(links)
    for links in candidates:
        for key in ("tls", "secure"):
            value = links.get(key)
            if isinstance(value, list) and value and isinstance(value[0], str):
                return value[0]
            if isinstance(value, str) and value:
                return value
    return None


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
