import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int | None
    server_host: str
    proxy_port: int
    proxy_config_path: Path
    database_path: Path
    support_contact: str


def get_settings() -> Settings:
    load_dotenv()

    admin_id_raw = os.getenv("ADMIN_ID", "").strip()
    admin_id = int(admin_id_raw) if admin_id_raw else None

    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        admin_id=admin_id,
        server_host=os.getenv("SERVER_HOST", "SERVER_HOST"),
        proxy_port=int(os.getenv("PROXY_PORT", "443")),
        proxy_config_path=Path(
            os.getenv("PROXY_CONFIG_PATH", "proxy/config/config.py")
        ),
        database_path=Path(os.getenv("DATABASE_PATH", "data/shop.db")),
        support_contact=os.getenv(
            "SUPPORT_CONTACT",
            "Напишите администратору этого бота для оплаты и поддержки.",
        ),
    )
