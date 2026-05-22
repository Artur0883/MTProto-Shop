import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    server_host: str
    proxy_port: int
    proxy_config_path: Path


def get_settings() -> Settings:
    return Settings(
        server_host=os.getenv("SERVER_HOST", "SERVER_HOST"),
        proxy_port=int(os.getenv("PROXY_PORT", "443")),
        proxy_config_path=Path(
            os.getenv("PROXY_CONFIG_PATH", "proxy/config/config.py")
        ),
    )
