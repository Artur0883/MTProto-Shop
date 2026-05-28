"""TLS-маскировка domain picker.

Periodically probes the configured TLS domains via real TLS handshake (port 443)
and ranks them by latency and success rate. Used to:

  * pick the best primary domain for new links,
  * provide alternate links to users in regions where one domain is DPI-blocked.

The picker is purely advisory — link generation always works using configured
domains; the picker only changes their *order*.
"""
from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass, field

from config import get_settings
from logging_setup import get_logger


logger = get_logger(__name__)


PROBE_TIMEOUT_SECONDS = 4.0
DEFAULT_PROBE_INTERVAL_SECONDS = 300.0  # 5 minutes
DEFAULT_PROBE_PORT = 443
_HISTORY_SIZE = 5


@dataclass
class DomainStats:
    domain: str
    last_latency_ms: float | None = None
    last_ok: bool = False
    last_probed_at: float | None = None
    recent: list[bool] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if not self.recent:
            return 0.0
        return sum(1 for x in self.recent if x) / len(self.recent)

    def record(self, ok: bool, latency_ms: float | None) -> None:
        self.last_ok = ok
        self.last_latency_ms = latency_ms
        self.last_probed_at = time.time()
        self.recent.append(ok)
        if len(self.recent) > _HISTORY_SIZE:
            self.recent = self.recent[-_HISTORY_SIZE:]


async def probe_domain(domain: str, port: int = DEFAULT_PROBE_PORT) -> tuple[bool, float | None]:
    """Open a TLS connection to `domain:port` and time the handshake.

    Returns (ok, latency_ms). On any error returns (False, None).
    """
    ctx = ssl.create_default_context()
    started = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=domain, port=port, ssl=ctx, server_hostname=domain),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        latency_ms = (time.monotonic() - started) * 1000.0
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, latency_ms
    except (asyncio.TimeoutError, OSError, ssl.SSLError) as exc:
        logger.debug("event=tls_probe_failed", domain=domain, error=str(exc))
        return False, None


class TLSDomainPicker:
    """Ranks TLS domains by latency and success rate. Refreshes in the background."""

    def __init__(self) -> None:
        self._stats: dict[str, DomainStats] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._interval = DEFAULT_PROBE_INTERVAL_SECONDS
        self._active_primary: str | None = None

    @property
    def active_primary(self) -> str | None:
        """Domain the self-heal loop currently advertises instead of the
        configured primary. None means: use the configured primary."""
        return self._active_primary

    def set_active_primary(self, domain: str) -> None:
        self._active_primary = domain

    def clear_active_primary(self) -> None:
        self._active_primary = None

    def _ensure_stats(self, domains: list[str]) -> None:
        for d in domains:
            self._stats.setdefault(d, DomainStats(domain=d))

    async def probe_all(self) -> None:
        settings = get_settings()
        domains = settings.fallback_tls_domains
        self._ensure_stats(domains)
        results = await asyncio.gather(
            *(probe_domain(d) for d in domains), return_exceptions=False
        )
        async with self._lock:
            for d, (ok, latency) in zip(domains, results):
                stats = self._stats.setdefault(d, DomainStats(domain=d))
                stats.record(ok, latency)
        ranked = self.ranked()
        logger.info(
            "event=tls_probe_complete",
            domains=[
                {"domain": s.domain, "ok": s.last_ok, "latency_ms": s.last_latency_ms}
                for s in ranked
            ],
        )

    def ranked(self) -> list[DomainStats]:
        """Return all known domains sorted best-first.

        Sort key: never-probed domains last; otherwise by (success_rate desc,
        latency asc, original-config-order asc).
        """
        settings = get_settings()
        original_order = {d: i for i, d in enumerate(settings.fallback_tls_domains)}
        items = list(self._stats.values())

        def key(s: DomainStats):
            never_probed = s.last_probed_at is None
            latency = s.last_latency_ms if s.last_latency_ms is not None else 9999.0
            return (
                never_probed,
                -s.success_rate,
                latency,
                original_order.get(s.domain, 999),
            )

        return sorted(items, key=key)

    def pick_best(self) -> str:
        """Return the best-known domain, or the configured primary as fallback."""
        settings = get_settings()
        ranked = self.ranked()
        for stats in ranked:
            if stats.last_ok:
                return stats.domain
        return settings.tls_domain

    def pick_alternatives(self, n: int = 3) -> list[str]:
        """Return up to N domains, best-first, including the best one."""
        settings = get_settings()
        ranked = self.ranked()
        seen: set[str] = set()
        out: list[str] = []
        for stats in ranked:
            if stats.domain not in seen:
                seen.add(stats.domain)
                out.append(stats.domain)
            if len(out) >= n:
                break
        # Ensure configured domains are represented even if never probed.
        for d in settings.fallback_tls_domains:
            if len(out) >= n:
                break
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out[:n]

    def snapshot(self) -> list[DomainStats]:
        return list(self._stats.values())

    async def _runner(self) -> None:
        while True:
            try:
                await self.probe_all()
            except Exception:
                logger.exception("event=tls_probe_loop_error")
            await asyncio.sleep(self._interval)

    def start(self, interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS) -> None:
        if self._task is not None and not self._task.done():
            return
        self._interval = interval_seconds
        self._task = asyncio.create_task(self._runner())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None


_picker: TLSDomainPicker | None = None


def get_picker() -> TLSDomainPicker:
    global _picker
    if _picker is None:
        _picker = TLSDomainPicker()
    return _picker
