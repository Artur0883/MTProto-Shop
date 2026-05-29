"""Async TeleMT HTTP API client.

Per-base-url aiohttp.ClientSession + circuit breaker, decorrelated-jitter
retries for transient errors.  Pass ``base_url`` to target a specific proxy
node; omit it (or pass None) to fall back to ``settings.telemt_api_url``.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Literal

import aiohttp

from config import get_settings
from logging_setup import get_logger


logger = get_logger(__name__)


class TeleMTError(Exception):
    """Base exception for TeleMT API errors."""


class ClientNotFoundError(TeleMTError):
    """HTTP 404 — the user does not exist on TeleMT."""


class CircuitOpenError(TeleMTError):
    """CB is OPEN — no call attempted."""


class _CircuitBreaker:
    """Simple async CB: CLOSED → OPEN after N consecutive failures,
    HALF_OPEN after recovery delay, returns to CLOSED on first success."""

    def __init__(self, threshold: int, recovery: float) -> None:
        self._threshold = threshold
        self._recovery = recovery
        self._failures = 0
        self._opened_at: float | None = None
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    async def allow(self) -> bool:
        async with self._lock:
            if self._state == "open":
                if self._opened_at is None:
                    return True
                now = asyncio.get_running_loop().time()
                if now - self._opened_at >= self._recovery:
                    self._state = "half_open"
                    self._opened_at = None
                    logger.info("event=cb_half_open")
                    return True
                return False
            return True

    async def on_success(self) -> None:
        async with self._lock:
            if self._state != "closed":
                logger.info("event=cb_closed", previous=self._state)
            self._state = "closed"
            self._failures = 0
            self._opened_at = None

    async def on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._threshold and self._state != "open":
                self._state = "open"
                self._opened_at = asyncio.get_running_loop().time()
                logger.error(
                    "event=cb_open",
                    failures=self._failures,
                    threshold=self._threshold,
                )


# Per-base-url registries replacing the single _session / _circuit globals.
_sessions: dict[str, aiohttp.ClientSession] = {}
_circuits: dict[str, _CircuitBreaker] = {}
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def _ensure_initialized(
    base_url: str,
) -> tuple[aiohttp.ClientSession, _CircuitBreaker]:
    """Return (session, circuit) for *base_url*, creating them on first use."""
    session = _sessions.get(base_url)
    circuit = _circuits.get(base_url)
    if session is not None and circuit is not None and not session.closed:
        return session, circuit

    async with _get_init_lock():
        # Re-check inside the lock (double-checked locking).
        session = _sessions.get(base_url)
        if session is None or session.closed:
            settings = get_settings()
            timeout = aiohttp.ClientTimeout(total=settings.telemt_api_timeout)
            session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "MTProto-Shop/1.0"},
                connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
            )
            _sessions[base_url] = session

        if _circuits.get(base_url) is None:
            settings = get_settings()
            _circuits[base_url] = _CircuitBreaker(
                threshold=settings.telemt_circuit_breaker_threshold,
                recovery=settings.telemt_circuit_breaker_recovery,
            )

    return _sessions[base_url], _circuits[base_url]


async def close_telemt() -> None:
    """Close all per-node sessions. Call on graceful shutdown."""
    for url, session in list(_sessions.items()):
        if not session.closed:
            await session.close()
    _sessions.clear()
    _circuits.clear()


def get_circuit_state(base_url: str | None = None) -> str:
    """Return current CB state for diagnostics (admin /status).

    If *base_url* is None the default ``settings.telemt_api_url`` is used.
    Returns ``"unknown"`` when no circuit has been created for that URL yet.
    """
    resolved = base_url or get_settings().telemt_api_url
    cb = _circuits.get(resolved)
    return cb.state if cb is not None else "unknown"


async def api_call(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    base_url: str | None = None,
) -> Any:
    """Perform one TeleMT API call against *base_url* (or the default).

    Returns parsed JSON or None.

    Raises:
        ClientNotFoundError: on HTTP 404.
        CircuitOpenError: when CB is OPEN.
        TeleMTError: on persistent transport or 5xx failure.
    """
    settings = get_settings()
    base_url = base_url or settings.telemt_api_url
    session, cb = await _ensure_initialized(base_url)

    if not await cb.allow():
        logger.warning("event=telemt_blocked_by_cb", method=method, path=path)
        raise CircuitOpenError("TeleMT circuit breaker is OPEN")

    url = f"{base_url}{path}"
    base_delay = 0.2
    cap_delay = 2.5
    prev = base_delay
    last_exc: Exception | None = None

    for attempt in range(settings.telemt_api_max_retries):
        try:
            async with session.request(method, url, json=body) as resp:
                if resp.status == 404:
                    await cb.on_success()
                    raise ClientNotFoundError(f"telemt 404 for {method} {path}")
                if 400 <= resp.status < 500:
                    text = (await resp.text())[:200]
                    await cb.on_success()
                    raise TeleMTError(
                        f"telemt {resp.status} on {method} {path}: {text}"
                    )
                if resp.status >= 500:
                    text = (await resp.text())[:200]
                    raise TeleMTError(
                        f"telemt {resp.status} on {method} {path}: {text}"
                    )
                if resp.content_length == 0:
                    await cb.on_success()
                    return None
                data = await resp.json(content_type=None)
                await cb.on_success()
                return data
        except ClientNotFoundError:
            raise
        except asyncio.TimeoutError:
            last_exc = TeleMTError(f"telemt timeout on {method} {path}")
            logger.warning(
                "event=telemt_timeout", method=method, path=path, attempt=attempt + 1
            )
        except aiohttp.ClientError as exc:
            last_exc = TeleMTError(f"telemt connection error: {exc}")
            logger.warning(
                "event=telemt_client_error",
                method=method,
                path=path,
                attempt=attempt + 1,
                error=str(exc),
            )
        except TeleMTError as exc:
            if "telemt 5" not in str(exc):
                raise  # 4xx is terminal — do not retry, do not count as CB failure
            last_exc = exc
            logger.warning(
                "event=telemt_server_5xx",
                method=method,
                path=path,
                attempt=attempt + 1,
                error=str(exc),
            )

        if attempt < settings.telemt_api_max_retries - 1:
            delay = min(cap_delay, random.uniform(base_delay, prev * 3))
            await asyncio.sleep(delay)
            prev = delay

    await cb.on_failure()
    assert last_exc is not None
    raise last_exc


async def is_available(base_url: str | None = None) -> bool:
    """Lightweight health probe used by /admin status and the monitor loop."""
    try:
        await api_call("GET", "/v1/users", base_url=base_url)
        return True
    except (TeleMTError, CircuitOpenError):
        return False
