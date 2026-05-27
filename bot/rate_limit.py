import logging
import time
from threading import Lock


_BUCKETS: dict[tuple[int, str], list[float]] = {}
_LOCK = Lock()
_GC_THRESHOLD = 20000


def check_action_rate(
    user_id: int,
    action: str,
    *,
    limit: int,
    window_seconds: float,
) -> tuple[bool, int]:
    """Sliding-window per-user per-action limiter.

    Returns (allowed, retry_after_seconds). retry_after_seconds is 0 when allowed.
    Heuristic in-memory state — survives within a bot uptime, reset on restart.
    """
    if limit <= 0:
        return True, 0
    now = time.monotonic()
    cutoff = now - window_seconds
    key = (user_id, action)
    with _LOCK:
        events = [t for t in _BUCKETS.get(key, []) if t > cutoff]
        if len(events) >= limit:
            oldest = events[0]
            retry_after = max(1, int(oldest + window_seconds - now + 0.5))
            _BUCKETS[key] = events
            return False, retry_after
        events.append(now)
        _BUCKETS[key] = events
        if len(_BUCKETS) > _GC_THRESHOLD:
            _gc_locked(cutoff)
    return True, 0


def count_action_recent(user_id: int, action: str, window_seconds: float) -> int:
    now = time.monotonic()
    cutoff = now - window_seconds
    key = (user_id, action)
    with _LOCK:
        events = _BUCKETS.get(key, [])
        live = [t for t in events if t > cutoff]
        if len(live) != len(events):
            _BUCKETS[key] = live
    return len(live)


def _gc_locked(cutoff: float) -> None:
    drop: list[tuple[int, str]] = []
    for k, events in _BUCKETS.items():
        live = [t for t in events if t > cutoff]
        if live:
            _BUCKETS[k] = live
        else:
            drop.append(k)
    for k in drop:
        _BUCKETS.pop(k, None)
    logging.info("event=rate_limit_gc remaining=%s dropped=%s", len(_BUCKETS), len(drop))


def format_retry_after(seconds: int) -> str:
    if seconds < 60:
        return f"{max(1, seconds)} сек"
    if seconds < 3600:
        return f"{max(1, seconds // 60)} мин"
    if seconds < 86400:
        return f"{max(1, seconds // 3600)} ч"
    return f"{max(1, seconds // 86400)} дн"
