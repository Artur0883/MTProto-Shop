"""Async TeleMT HTTP API client.

Single shared aiohttp.ClientSession per process, circuit breaker for
infrastructure failures, decorrelated-jitter retries for transient errors.
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


_session: aiohttp.ClientSession | None = None
_circuit: _CircuitBreaker | None = None
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def _ensure_initialized() -> tuple[aiohttp.ClientSession, _CircuitBreaker]:
    global _session, _circuit
    if _session is not None and _circuit is not None and not _session.closed:
        return _session, _circuit
    async with _get_init_lock():
        settings = get_settings()
        if _session is None or _session.closed:
            timeout = aiohttp.ClientTimeout(total=settings.telemt_api_timeout)
            _session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "MTProto-Shop/1.0"},
                connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
            )
        if _circuit is None:
            _circuit = _CircuitBreaker(
                threshold=settings.telemt_circuit_breaker_threshold,
                recovery=settings.telemt_circuit_breaker_recovery,
            )
    return _session, _circuit


async def close_telemt() -> None:
    """Close the shared session. Call on graceful shutdown."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def get_circuit_state() -> str:
    """Return current CB state for diagnostics (admin /status)."""
    return _circuit.state if _circuit else "unknown"


async def api_call(method: str, path: str, body: dict | None = None) -> Any:
    """Perform one TeleMT API call. Returns parsed JSON or None.

    Raises:
        ClientNotFoundError: on HTTP 404.
        CircuitOpenError: when CB is OPEN.
        TeleMTError: on persistent transport or 5xx failure.
    """
    settings = get_settings()
    session, cb = await _ensure_initialized()

    if not await cb.allow():
        logger.warning("event=telemt_blocked_by_cb", method=method, path=path)
        raise CircuitOpenError("TeleMT circuit breaker is OPEN")

    url = f"{settings.telemt_api_url}{path}"
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


async def is_available() -> bool:
    """Lightweight health probe used by /admin status and the monitor loop."""
    try:
        await api_call("GET", "/v1/users")
        return True
    except (TeleMTError, CircuitOpenError):
        return False
