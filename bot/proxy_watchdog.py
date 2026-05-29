"""Background watchdog for a proxy that is alive but no longer serving clients.

Docker's liveness healthcheck + autoheal only catch a *dead* proxy. A proxy that
keeps running yet stops accepting client connections (hung listener, upstream
churn) stays "healthy" and is never restarted — that gap caused a real outage.

This watchdog probes the actual client port like a real client (a TLS handshake,
which a FakeTLS proxy answers as camouflage). On sustained failure *while the API
is still up*, it asks the host restart-watcher to recreate the proxy (same
mechanism as the admin "reboot" button) and tells the admin in plain language.
Decision is the pure, tested `should_restart_proxy`.
"""
from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot

from config import get_settings
from logging_setup import get_logger
from proxy_watchdog_logic import should_restart_proxy
from telemt_client import is_available


logger = get_logger(__name__)

RESTART_REQUEST_PATH = Path("/app/data/restart.request")
RESTART_LOG = Path("/app/data/restart.log")


def _proxy_client_endpoint() -> tuple[str, int]:
    """Host:port of the LOCAL proxy's client listener (the one we can restart)."""
    settings = get_settings()
    host = urlparse(settings.telemt_api_url).hostname or "mtproto"
    return host, settings.telemt_proxy_internal_port


async def probe_client_port(host: str, port: int, *, timeout: float, sni: str) -> bool:
    """True if a TLS handshake to the proxy's client port completes — i.e. the
    proxy is actually accepting connections. Any failure/timeout => not serving."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni),
            timeout=timeout,
        )
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def _seconds_since_last_restart() -> float | None:
    try:
        if RESTART_LOG.exists():
            return datetime.now(UTC).timestamp() - RESTART_LOG.stat().st_mtime
    except OSError:
        pass
    return None


def _request_restart() -> None:
    RESTART_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESTART_REQUEST_PATH.write_text(f"mtproto\n{datetime.now(UTC).isoformat()}")


async def proxy_watchdog_loop(bot: Bot) -> None:
    settings = get_settings()
    host, port = _proxy_client_endpoint()
    sni = settings.fallback_tls_domains[0]
    consecutive_failures = 0
    logger.info(
        "event=proxy_watchdog_started",
        host=host,
        port=port,
        interval=settings.proxy_serving_check_interval,
    )
    while True:
        try:
            api_up = await is_available()
            port_ok = await probe_client_port(
                host, port, timeout=settings.telemt_api_timeout, sni=sni
            )
            consecutive_failures = 0 if port_ok else consecutive_failures + 1
            if not port_ok:
                logger.warning(
                    "event=proxy_serving_probe_failed",
                    consecutive_failures=consecutive_failures,
                    api_up=api_up,
                )

            if should_restart_proxy(
                api_up=api_up,
                port_ok=port_ok,
                consecutive_failures=consecutive_failures,
                failure_threshold=settings.proxy_serving_failure_threshold,
                seconds_since_last_restart=_seconds_since_last_restart(),
                restart_cooldown=settings.proxy_restart_cooldown,
            ):
                logger.error(
                    "event=proxy_watchdog_restart",
                    consecutive_failures=consecutive_failures,
                    host=host,
                    port=port,
                )
                _request_restart()
                consecutive_failures = 0
                if settings.admin_id is not None:
                    try:
                        await bot.send_message(
                            settings.admin_id,
                            "🛠 Прокси перестал принимать новые подключения — "
                            "перезапускаю его автоматически. Клиенты переподключатся "
                            "сами примерно через минуту, вмешательства не требуется.",
                        )
                    except Exception:
                        logger.exception("event=proxy_watchdog_notify_failed")
        except Exception:
            logger.exception("event=proxy_watchdog_loop_error")
        await asyncio.sleep(settings.proxy_serving_check_interval)
