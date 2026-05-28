"""Pure decision logic for proxy self-healing.

No external dependencies (no config, no picker, no network) so the brain of the
self-heal feature is deterministic and unit-testable. The side-effecting loop in
self_heal.py feeds a SelfHealState snapshot into `decide`.
"""
from __future__ import annotations

from dataclasses import dataclass


# A probed domain is unhealthy when its success rate drops below this.
UNHEALTHY_BELOW = 0.5
# A probed domain is a safe switch target only at/above this success rate.
HEALTHY_AT_LEAST = 0.6


@dataclass(frozen=True)
class DomainHealth:
    domain: str
    probed: bool
    last_ok: bool
    success_rate: float


@dataclass(frozen=True)
class SelfHealState:
    configured_primary: str
    active_primary: str | None
    domains: tuple[DomainHealth, ...]  # ranked best-first
    now: float                         # monotonic seconds
    last_switch_at: float | None
    switches_today: int
    switch_cooldown: float
    max_switches_per_day: int


@dataclass(frozen=True)
class Decision:
    action: str                    # keep | switch | recover | revert_escalate | escalate
    target: str | None = None      # domain to advertise
    escalation: str | None = None  # all_domains_down | switch_limit_reached


def is_unhealthy(d: DomainHealth) -> bool:
    return d.probed and d.success_rate < UNHEALTHY_BELOW


def is_healthy(d: DomainHealth) -> bool:
    return d.probed and d.last_ok and d.success_rate >= HEALTHY_AT_LEAST


def _find(domains: tuple[DomainHealth, ...], name: str) -> DomainHealth | None:
    for d in domains:
        if d.domain == name:
            return d
    return None


def decide(state: SelfHealState) -> Decision:
    current = state.active_primary or state.configured_primary

    # Configured primary recovered while an override is active -> go back to it.
    if state.active_primary is not None:
        configured = _find(state.domains, state.configured_primary)
        if configured is not None and is_healthy(configured):
            return Decision("recover", target=state.configured_primary)

    current_health = _find(state.domains, current)
    if current_health is None or not is_unhealthy(current_health):
        return Decision("keep")

    # Current advertised domain is unhealthy. Find the best healthy alternative.
    healthy_alts = [
        d.domain for d in state.domains if d.domain != current and is_healthy(d)
    ]
    if not healthy_alts:
        if state.active_primary is not None:
            return Decision(
                "revert_escalate",
                target=state.configured_primary,
                escalation="all_domains_down",
            )
        return Decision("escalate", escalation="all_domains_down")

    if state.switches_today >= state.max_switches_per_day:
        return Decision("escalate", escalation="switch_limit_reached")

    if (
        state.last_switch_at is not None
        and (state.now - state.last_switch_at) < state.switch_cooldown
    ):
        return Decision("keep")

    return Decision("switch", target=healthy_alts[0])
