"""Pure decision for the proxy serving watchdog. No I/O.

Decides whether to restart a proxy that is alive (its API answers) but has
stopped accepting client connections. Kept pure so it is unit-tested without a
running proxy.
"""
from __future__ import annotations


def should_restart_proxy(
    *,
    api_up: bool,
    port_ok: bool,
    consecutive_failures: int,
    failure_threshold: int,
    seconds_since_last_restart: float | None,
    restart_cooldown: float,
) -> bool:
    """Restart only when the proxy is alive but not serving clients, sustained.

    - ``api_up`` False means the whole proxy is down — that is a *dead* process,
      handled by Docker's restart policy / autoheal, not us. Don't act.
    - ``port_ok`` True means it is serving — nothing to do.
    - A single failed probe is ignored as transient: require ``failure_threshold``
      consecutive failures.
    - Never restart again within ``restart_cooldown`` seconds of the last restart
      (prevents restart loops if a restart doesn't fix it).
    """
    if not api_up:
        return False
    if port_ok:
        return False
    if consecutive_failures < failure_threshold:
        return False
    if (
        seconds_since_last_restart is not None
        and seconds_since_last_restart < restart_cooldown
    ):
        return False
    return True
