import argparse
import asyncio
import json
import logging
import re
import secrets
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

from config import get_settings


SECRET_RE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SECURE_SECRET_PREFIX = "dd"
TLS_SECRET_PREFIX = "ee"
DEFAULT_TIMEOUT = 5.0


class ClientNotFoundError(ValueError):
    pass


def mask_secret(secret: str) -> str:
    if len(secret) <= 10:
        return "***"
    return f"{secret[:6]}...{secret[-4:]}"


def generate_secret() -> str:
    return secrets.token_hex(16)


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


def _api_request(
    method: str, path: str, body: dict | None = None
) -> dict | list | None:
    settings = get_settings()
    url = f"{settings.telemt_api_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            raw = response.read()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"telemt API {method} {path} returned invalid JSON"
                ) from exc
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ClientNotFoundError(f"telemt 404 for {method} {path}") from exc
        raise RuntimeError(
            f"telemt API {method} {path} failed: {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"telemt API {method} {path} unreachable: {exc.reason}"
        ) from exc


def _response_data(response: object) -> object:
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response


def _secret_from_link(link: str) -> str | None:
    secret_values = parse_qs(urlparse(link).query).get("secret", [])
    if not secret_values:
        return None
    public_secret = secret_values[0].lower()
    if public_secret.startswith((SECURE_SECRET_PREFIX, TLS_SECRET_PREFIX)):
        secret = public_secret[2:34]
    else:
        secret = public_secret[:32]
    return secret if SECRET_RE.fullmatch(secret) else None


def _extract_secret(entry: object) -> str | None:
    if entry is None:
        return None
    if isinstance(entry, str):
        candidate = entry.lower()
        if SECRET_RE.fullmatch(candidate):
            return candidate
        return _secret_from_link(entry)
    if isinstance(entry, dict):
        for key in ("secret", "client_secret"):
            value = entry.get(key)
            if isinstance(value, str) and SECRET_RE.fullmatch(value.lower()):
                return value.lower()
        for key in ("data", "user"):
            value = _extract_secret(entry.get(key))
            if value:
                return value
        links = entry.get("links")
        if isinstance(links, dict):
            for mode in ("tls", "secure", "classic"):
                values = links.get(mode) or []
                if isinstance(values, list):
                    for link in values:
                        if isinstance(link, str):
                            value = _secret_from_link(link)
                            if value:
                                return value
    return None


def _extract_tls_link(entry: object) -> str | None:
    if isinstance(entry, dict):
        for key in ("data", "user"):
            link = _extract_tls_link(entry.get(key))
            if link:
                return link
        links = entry.get("links")
        if isinstance(links, dict):
            tls_links = links.get("tls") or []
            if isinstance(tls_links, list) and tls_links:
                if isinstance(tls_links[0], str):
                    return tls_links[0]
    return None


def create_secret(client_id: str, provided_secret: str | None = None) -> str:
    validate_client_id(client_id)
    secret = (provided_secret or generate_secret()).lower()
    validate_secret(secret)
    try:
        existing = _api_request("GET", f"/v1/users/{client_id}")
        current = _extract_secret(existing)
        if current:
            return current
    except ClientNotFoundError:
        pass
    response = _api_request(
        "POST", "/v1/users", {"username": client_id, "secret": secret}
    )
    effective_secret = _extract_secret(response) or secret
    logging.info("Created secret for %s: %s", client_id, mask_secret(effective_secret))
    return effective_secret


def delete_secret(client_id: str) -> str:
    validate_client_id(client_id)
    settings = get_settings()
    if client_id == settings.telemt_system_user:
        raise ValueError(f"refusing to delete system user '{client_id}'")
    try:
        existing = _api_request("GET", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    secret = _extract_secret(existing) or ""
    try:
        _api_request("DELETE", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    logging.info("Deleted secret for %s: %s", client_id, mask_secret(secret))
    return secret


def rotate_secret(client_id: str) -> str:
    validate_client_id(client_id)
    new_secret = generate_secret()
    try:
        response = _api_request(
            "POST", f"/v1/users/{client_id}/rotate-secret", {"secret": new_secret}
        )
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    secret = _extract_secret(response) or new_secret
    logging.info("Rotated secret for %s: %s", client_id, mask_secret(secret))
    return secret


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
    validate_client_id(client_id)
    settings = get_settings()
    try:
        response = _api_request("GET", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    tls_link = _extract_tls_link(response)
    if tls_link:
        return tls_link
    secret = _extract_secret(response) or ""
    if not secret:
        raise RuntimeError(f"client '{client_id}' has no TLS link or secret")
    return build_tls_proxy_link(
        settings.server_host,
        settings.proxy_port,
        secret,
        settings.tls_domain,
    )


def list_clients() -> dict[str, str]:
    settings = get_settings()
    response = _response_data(_api_request("GET", "/v1/users") or [])
    users = response.get("users") if isinstance(response, dict) else response
    result: dict[str, str] = {}
    if isinstance(users, list):
        for entry in users:
            if not isinstance(entry, dict):
                continue
            name = entry.get("username") or entry.get("name")
            if not isinstance(name, str) or name == settings.telemt_system_user:
                continue
            result[name] = _extract_secret(entry) or ""
    elif isinstance(users, dict):
        for name, entry in users.items():
            if name == settings.telemt_system_user:
                continue
            result[str(name)] = _extract_secret(entry) or ""
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage TeleMT users via HTTP API")
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
            print("apply changes via TeleMT API")
        elif args.command == "delete":
            secret = delete_secret(args.client_id)
            print(f"deleted {args.client_id}: {mask_secret(secret)}")
            print("apply changes via TeleMT API")
        elif args.command == "rotate":
            secret = rotate_secret(args.client_id)
            print(get_link(args.client_id))
            print(f"rotated {args.client_id}: {mask_secret(secret)}")
            print("apply changes via TeleMT API")
        elif args.command == "rotate-telegram":
            secret = asyncio.run(rotate_telegram_secret(args.telegram_id))
            print(get_link(f"tg_{args.telegram_id}"))
            print(f"rotated tg_{args.telegram_id}: {mask_secret(secret)}")
            print("subscription secret in SQLite was updated")
            print("apply changes via TeleMT API")
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
