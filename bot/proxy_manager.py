import argparse
import logging
import os
import re
import runpy
import secrets
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from config import get_settings


SECRET_RE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SECURE_SECRET_PREFIX = "dd"


def get_example_config_path(config_path: Path) -> Path:
    return config_path.with_name("config.example.py")


def set_runtime_config_permissions(config_path: Path) -> None:
    os.chmod(config_path, 0o644)


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


def write_config(config_path: Path, users: dict[str, str]) -> None:
    ensure_runtime_config(config_path)
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
    "tls": False,
}}

TLS_DOMAIN = os.getenv("TLS_DOMAIN", "www.google.com")
'''

    if os.name == "nt":
        config_path.write_text(content, encoding="utf-8", newline="\n")
        set_runtime_config_permissions(config_path)
        return

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


def create_secret(client_id: str, provided_secret: str | None = None) -> str:
    settings = get_settings()
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
    validate_client_id(client_id)
    users = load_users(settings.proxy_config_path)
    if client_id not in users:
        raise ValueError(f"client '{client_id}' not found")

    secret = users.pop(client_id)
    write_config(settings.proxy_config_path, users)
    logging.info("Deleted secret for %s: %s", client_id, mask_secret(secret))
    return secret


def get_link(client_id: str) -> str:
    settings = get_settings()
    validate_client_id(client_id)
    users = load_users(settings.proxy_config_path)
    if client_id not in users:
        raise ValueError(f"client '{client_id}' not found")

    return build_proxy_link(settings.server_host, settings.proxy_port, users[client_id])


def list_clients() -> dict[str, str]:
    settings = get_settings()
    return load_users(settings.proxy_config_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage mtprotoproxy USERS config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a secret for client")
    create.add_argument("client_id")
    create.add_argument("--secret", help="optional 32 hex chars secret")

    delete = subparsers.add_parser("delete", help="delete client secret")
    delete.add_argument("client_id")

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
