"""Application settings — Pydantic v2 BaseSettings with validation.

Reads from environment variables and `.env` file. All HTTP/CB timings are
configurable through env so production tuning does not require code changes.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


TOKEN_LIKE_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
SAFE_SUPPORT_FALLBACK = "Поддержка временно не указана."


def _sanitize_support_contact(contact: str, bot_token: str) -> str:
    contact = (contact or "").strip()
    if not contact:
        return SAFE_SUPPORT_FALLBACK
    if bot_token and contact == bot_token:
        return SAFE_SUPPORT_FALLBACK
    if TOKEN_LIKE_RE.fullmatch(contact):
        return SAFE_SUPPORT_FALLBACK
    return contact


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Telegram ---
    bot_token: str = Field(default="")
    support_bot_token: str = Field(default="")
    admin_id: int | None = Field(default=None)

    # --- Proxy public endpoint ---
    server_host: str = Field(default="SERVER_HOST")
    proxy_port: int = Field(default=443, ge=1, le=65535)
    telemt_proxy_internal_port: int = Field(default=443, ge=1, le=65535)

    # --- TLS-маскировка (DPI bypass) ---
    tls_domain: str = Field(default="www.microsoft.com", description="Primary TLS domain")
    tls_domains: str = Field(
        default="",
        description="CSV of fallback TLS domains, e.g. 'www.cloudflare.com,www.apple.com,www.bing.com'",
    )

    # --- TeleMT API ---
    proxy_core: str = Field(default="telemt")
    telemt_api_url: str = Field(default="http://mtproto:9091")
    telemt_system_user: str = Field(default="shop_bootstrap")
    telemt_api_timeout: float = Field(default=5.0, gt=0, le=30)
    telemt_api_max_retries: int = Field(default=3, ge=1, le=10)
    telemt_circuit_breaker_threshold: int = Field(
        default=5, ge=1, description="Consecutive failures before circuit opens"
    )
    telemt_circuit_breaker_recovery: float = Field(
        default=30.0, gt=0, description="Seconds in OPEN state before HALF_OPEN trial"
    )
    telemt_rotate_cooldown: float = Field(
        default=30.0, ge=0, description="Per-client rotate cooldown in seconds (API-level)"
    )

    # --- Storage ---
    database_path: Path = Field(default=Path("data/shop.db"))

    # --- UX ---
    support_contact: str = Field(default="")

    # --- Payments ---
    PAYMENT_MODE: Literal["manual", "stars", "crypto", "auto_free"] = Field(default="manual")
    test_auto_issue_access: bool = Field(default=False, validation_alias="DEV_AUTO_ISSUE")
    payments_enabled: bool = Field(default=True, validation_alias="PAYMENTS_ENABLED")

    # --- Observability ---
    log_level: str = Field(default="INFO")
    log_format: Literal["json", "console"] = Field(
        default="json", description="json for production, console for dev"
    )
    healthcheck_ping_url: str = Field(
        default="",
        description=(
            "Optional external dead-man's-switch URL (e.g. healthchecks.io). "
            "Pinged on each successful heartbeat; alerts if the whole VPS dies. "
            "Inert when empty."
        ),
    )

    # --- Self-heal ---
    self_heal_enabled: bool = Field(default=True)
    self_heal_check_interval: float = Field(default=120.0, gt=0)
    self_heal_switch_cooldown: float = Field(default=600.0, ge=0)
    self_heal_max_switches_per_day: int = Field(default=6, ge=0)

    # --- Proxy nodes (multi-server) ---
    # JSON list: [{"name","public_host","api_url","primary"}]. Empty => single node
    # derived from SERVER_HOST + TELEMT_API_URL (backward compatible).
    proxy_nodes: list[dict] = Field(default_factory=list)
    reconcile_interval: float = Field(default=300.0, gt=0)

    # --- Proxy serving watchdog ---
    # Probes the client port like a real client. Restarts a proxy that is alive
    # (API up) but no longer accepting connections — the gap the liveness
    # healthcheck + autoheal miss (hung listener / middle-proxy churn).
    proxy_serving_watchdog_enabled: bool = Field(default=True)
    proxy_serving_check_interval: float = Field(default=30.0, gt=0)
    proxy_serving_failure_threshold: int = Field(default=3, gt=0)
    proxy_restart_cooldown: float = Field(default=600.0, gt=0)

    @field_validator("admin_id", mode="before")
    @classmethod
    def _parse_admin_id(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() == "none":
            return None
        return int(s)

    @field_validator(
        "bot_token",
        "support_bot_token",
        "support_contact",
        "server_host",
        "healthcheck_ping_url",
        mode="before",
    )
    @classmethod
    def _strip_str(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("tls_domain", mode="before")
    @classmethod
    def _strip_domain(cls, v):
        return (v or "").strip() if isinstance(v, str) else v

    @field_validator("telemt_api_url", mode="before")
    @classmethod
    def _strip_url(cls, v):
        return v.strip().rstrip("/") if isinstance(v, str) else v

    @field_validator("proxy_core", mode="before")
    @classmethod
    def _normalize_core(cls, v):
        return ((v or "telemt").strip().lower() or "telemt") if isinstance(v, str) else v

    @field_validator("telemt_system_user", mode="before")
    @classmethod
    def _system_user_default(cls, v):
        if isinstance(v, str):
            return v.strip() or "shop_bootstrap"
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _post(self) -> "Settings":
        sanitized = _sanitize_support_contact(self.support_contact, self.bot_token)
        if sanitized != self.support_contact:
            self.support_contact = sanitized
        return self

    @property
    def fallback_tls_domains(self) -> list[str]:
        """Ordered, deduplicated list of valid TLS domains: primary + fallbacks."""
        items: list[str] = [self.tls_domain]
        for raw in (self.tls_domains or "").split(","):
            d = raw.strip()
            if d:
                items.append(d)

        seen: set[str] = set()
        out: list[str] = []
        for d in items:
            if d and DOMAIN_RE.fullmatch(d) and d not in seen:
                seen.add(d)
                out.append(d)
        return out or [self.tls_domain]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache and re-read settings. Use in tests or after .env edit."""
    get_settings.cache_clear()
    return get_settings()
