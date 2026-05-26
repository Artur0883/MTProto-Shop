import argparse
import asyncio
import logging
import os
import re
import runpy
import secrets
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from config import get_settings


SECRET_RE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SECURE_SECRET_PREFIX = "dd"
TLS_SECRET_PREFIX = "ee"  # ee + 32 hex client secret + hex(UTF-8 TLS domain)
SUPPORTED_PROXY_CORE = "alexbers"


class ClientNotFoundError(ValueError):
    pass


def get_example_config_path(config_path: Path) -> Path:
    return config_path.with_name("config.example.py")


def set_runtime_config_permissions(config_path: Path) -> None:
    os.chmod(config_path, 0o644)


def require_supported_proxy_core() -> None:
    core = os.getenv("PROXY_CORE", SUPPORTED_PROXY_CORE).strip().lower()
    if core != SUPPORTED_PROXY_CORE:
        raise RuntimeError(
            "TeleMT adapter is not implemented; set PROXY_CORE=alexbers "
            "before managing proxy clients"
        )


def ensure_runtime_config(config_path: Path) -> None:
    if config_path.exists():
        set_runtime_config_permissions(config_path)
        return

    example_path = get_example_config_path(config_path)
    if not example_path.exists():
        raise FileNotFoundError(
            f"runtime config is missing and template was not found: {example_path}"
        )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_path, config_path)
    set_runtime_config_permissions(config_path)
    logging.info("Created runtime proxy config from template: %s", config_path)


def mask_secret(secret: str) -> str:
    if len(secret) <= 10:
        return "***"
    return f"{secret[:6]}...{secret[-4:]}"


def generate_secret() -> str:
    return secrets.token_hex(16)


def load_users(config_path: Path) -> dict[str, str]:
    ensure_runtime_config(config_path)

    data = runpy.run_path(str(config_path))
    users = data.get("USERS", {})
    if not isinstance(users, dict):
        raise ValueError("USERS in proxy config must be a dict")

    return {str(name): str(secret).lower() for name, secret in users.items()}


def request_proxy_reload() -> None:
    try:
        settings = get_settings()
        sentinel_path = settings.database_path.parent / "proxy.reload.request"
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    except Exception as exc:
        logging.warning("proxy reload sentinel not written: %s", exc)


def write_config(config_path: Path, users: dict[str, str]) -> None:
    ensure_runtime_config(config_path)
    if os.name != "nt":
        for stale in config_path.parent.glob("tmp*.py"):
            try:
                stale.unlink()
            except OSError:
                pass
    config_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_users = dict(sorted(users.items(), key=lambda item: item[0]))
    users_lines = "\n".join(
        f'    "{name}": "{secret}",' for name, secret in sorted_users.items()
    )
    if users_lines:
        users_block = "{\n" + users_lines + "\n}"
    else:
        users_block = "{}"

    content = f'''import os

PORT = int(os.getenv("PROXY_PORT", "443"))

USERS = {users_block}

MODES = {{
    "classic": False,
    "secure": True,
    "tls": True,
}}

TLS_DOMAIN = os.getenv("TLS_DOMAIN", "www.cloudflare.com")
'''

    if os.name == "nt":
        config_path.write_text(content, encoding="utf-8", newline="\n")
        set_runtime_config_permissions(config_path)
    else:
        fd, tmp_name = tempfile.mkstemp(
            prefix="tmp",
            suffix=".py",
            dir=str(config_path.parent),
            text=True,
        )
        tmp_path = Path(tmp_name)

        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp_file:
                tmp_file.write(content)
            os.replace(tmp_path, config_path)
            set_runtime_config_permissions(config_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    request_proxy_reload()


def validate_client_id(client_id: str) -> None:
    if not CLIENT_RE.fullmatch(client_id):
        raise ValueError("client_id must be 1-64 chars: letters, digits, _, . or -")


def validate_secret(secret: str) -> None:
    if not SECRET_RE.fullmatch(secret):
        raise ValueError("secret must be exactly 32 lowercase hex chars")


def build_proxy_link(server_host: str, proxy_port: int, secret: str) -> str:
    public_secret = f"{SECURE_SECRET_PREFIX}{secret}"
    params = urlencode(
        {
            "server": server_host,
            "port": str(proxy_port),
            "secret": public_secret,
        }
    )
    return f"tg://proxy?{params}"


def build_tls_proxy_link(
    server_host: str, proxy_port: int, secret: str, tls_domain: str
) -> str:
    domain_hex = tls_domain.encode("utf-8").hex()
    public_secret = f"{TLS_SECRET_PREFIX}{secret}{domain_hex}"
    params = urlencode(
        {
            "server": server_host,
            "port": str(proxy_port),
            "secret": public_secret,
        }
    )
    return f"tg://proxy?{params}"


def create_secret(client_id: str, provided_secret: str | None = None) -> str:
    settings = get_settings()
    require_supported_proxy_core()
    validate_client_id(client_id)
    secret = (provided_secret or generate_secret()).lower()
    validate_secret(secret)

    users = load_users(settings.proxy_config_path)
    if client_id in users:
        raise ValueError(f"client '{client_id}' already exists")

    users[client_id] = secret
    write_config(settings.proxy_config_path, users)
    logging.info("Created secret for %s: %s", client_id, mask_secret(secret))
    return secret


def delete_secret(client_id: str) -> str:
    settings = get_settings()
    require_supported_proxy_core()
    validate_client_id(client_id)
    users = load_users(settings.proxy_config_path)
    if client_id not in users:
        raise ClientNotFoundError(f"client '{client_id}' not found")

    secret = users.pop(client_id)
    write_config(settings.proxy_config_path, users)
    logging.info("Deleted secret for %s: %s", client_id, mask_secret(secret))
    return secret


def rotate_secret(client_id: str) -> str:
    settings = get_settings()
    require_supported_proxy_core()
    validate_client_id(client_id)
    users = load_users(settings.proxy_config_path)
    if client_id not in users:
        raise ClientNotFoundError(f"client '{client_id}' not found")

    old_secret = users[client_id]
    new_secret = generate_secret()
    users[client_id] = new_secret
    write_config(settings.proxy_config_path, users)
    logging.info(
        "Rotated secret for %s: %s -> %s",
        client_id,
        mask_secret(old_secret),
        mask_secret(new_secret),
    )
    return new_secret


async def rotate_telegram_secret(telegram_id: int) -> str:
    from database import (
        get_latest_subscription_by_telegram_id,
        update_latest_subscription_secret_by_telegram_id,
    )

    settings = get_settings()
    subscription = await get_latest_subscription_by_telegram_id(
        settings.database_path,
        telegram_id,
    )
    if subscription is None:
        raise ClientNotFoundError(
            f"subscription for telegram_id '{telegram_id}' not found"
        )

    client_id = f"tg_{telegram_id}"
    secret = rotate_secret(client_id)
    updated = await update_latest_subscription_secret_by_telegram_id(
        settings.database_path,
        telegram_id,
        secret,
    )
    if updated is None:
        raise ClientNotFoundError(
            f"subscription for telegram_id '{telegram_id}' not found"
        )
    return secret


def get_link(client_id: str) -> str:
    settings = get_settings()
    require_supported_proxy_core()
    validate_client_id(client_id)
    users = load_users(settings.proxy_config_path)
    if client_id not in users:
        raise ClientNotFoundError(f"client '{client_id}' not found")

    return build_tls_proxy_link(
        settings.server_host,
        settings.proxy_port,
        users[client_id],
        settings.tls_domain,
    )


def list_clients() -> dict[str, str]:
    settings = get_settings()
    require_supported_proxy_core()
    return load_users(settings.proxy_config_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage mtprotoproxy USERS config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a secret for client")
    create.add_argument("client_id")
    create.add_argument("--secret", help="optional 32 hex chars secret")

    delete = subparsers.add_parser("delete", help="delete client secret")
    delete.add_argument("client_id")

    rotate = subparsers.add_parser("rotate", help="generate a new secret for client")
    rotate.add_argument("client_id")

    rotate_tg = subparsers.add_parser(
        "rotate-telegram",
        help="generate a new secret for Telegram user and sync latest subscription in DB",
    )
    rotate_tg.add_argument("telegram_id", type=int)

    link = subparsers.add_parser("link", help="print proxy link for client")
    link.add_argument("client_id")

    subparsers.add_parser("list", help="list clients with masked secrets")

    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args()

    try:
        if args.command == "create":
            secret = create_secret(args.client_id, args.secret)
            print(get_link(args.client_id))
            print(f"created {args.client_id}: {mask_secret(secret)}")
            print("apply changes: docker compose kill -s SIGUSR2 mtproto")
        elif args.command == "delete":
            secret = delete_secret(args.client_id)
            print(f"deleted {args.client_id}: {mask_secret(secret)}")
            print("apply changes: docker compose kill -s SIGUSR2 mtproto")
        elif args.command == "rotate":
            secret = rotate_secret(args.client_id)
            print(get_link(args.client_id))
            print(f"rotated {args.client_id}: {mask_secret(secret)}")
            print("apply changes: docker compose kill -s SIGUSR2 mtproto")
        elif args.command == "rotate-telegram":
            secret = asyncio.run(rotate_telegram_secret(args.telegram_id))
            print(get_link(f"tg_{args.telegram_id}"))
            print(f"rotated tg_{args.telegram_id}: {mask_secret(secret)}")
            print("subscription secret in SQLite was updated")
            print("apply changes: docker compose kill -s SIGUSR2 mtproto")
        elif args.command == "link":
            print(get_link(args.client_id))
        elif args.command == "list":
            users = list_clients()
            if not users:
                print("no clients")
            for client_id, secret in sorted(users.items()):
                print(f"{client_id}: {mask_secret(secret)}")
    except Exception as exc:
        logging.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
