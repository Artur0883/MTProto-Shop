import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast


TOKEN_LIKE_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
SAFE_SUPPORT_FALLBACK = "Поддержка временно не указана."


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def sanitize_support_contact(contact: str, bot_token: str) -> str:
    contact = contact.strip()
    if not contact:
        return SAFE_SUPPORT_FALLBACK
    if bot_token and contact == bot_token:
        return SAFE_SUPPORT_FALLBACK
    if TOKEN_LIKE_RE.fullmatch(contact):
        return SAFE_SUPPORT_FALLBACK
    return contact


@dataclass(frozen=True)
class Settings:
    bot_token: str
    support_bot_token: str
    admin_id: int | None
    server_host: str
    proxy_port: int
    tls_domain: str
    proxy_core: str
    telemt_api_url: str
    telemt_system_user: str
    database_path: Path
    support_contact: str
    PAYMENT_MODE: Literal["manual", "stars", "crypto", "auto_free"]
    test_auto_issue_access: bool


def get_settings() -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_id_raw = os.getenv("ADMIN_ID", "").strip()
    admin_id = int(admin_id_raw) if admin_id_raw else None
    payment_mode = os.getenv("PAYMENT_MODE", "manual").strip().lower()
    if payment_mode not in {"manual", "stars", "crypto", "auto_free"}:
        payment_mode = "manual"

    return Settings(
        bot_token=bot_token,
        support_bot_token=os.getenv("SUPPORT_BOT_TOKEN", "").strip(),
        admin_id=admin_id,
        server_host=os.getenv("SERVER_HOST", "SERVER_HOST"),
        proxy_port=int(os.getenv("PROXY_PORT", "443")),
        tls_domain=os.getenv("TLS_DOMAIN", "www.cloudflare.com").strip(),
        proxy_core=os.getenv("PROXY_CORE", "telemt").strip().lower() or "telemt",
        telemt_api_url=os.getenv("TELEMT_API_URL", "http://mtproto:9091").strip().rstrip("/"),
        telemt_system_user=os.getenv("TELEMT_SYSTEM_USER", "shop_bootstrap").strip() or "shop_bootstrap",
        database_path=Path(os.getenv("DATABASE_PATH", "data/shop.db")),
        support_contact=sanitize_support_contact(
            os.getenv("SUPPORT_CONTACT", ""),
            bot_token,
        ),
        PAYMENT_MODE=cast(Literal["manual", "stars", "crypto", "auto_free"], payment_mode),
        test_auto_issue_access=parse_bool(
            os.getenv("DEV_AUTO_ISSUE"),
            default=False,
        ),
    )
