from self_heal_logic import DomainHealth, SelfHealState, decide


def mk(domain, ok, rate, probed=True):
    return DomainHealth(domain=domain, probed=probed, last_ok=ok, success_rate=rate)


def state(**kw):
    base = dict(
        configured_primary="a.com",
        active_primary=None,
        domains=(),
        now=1000.0,
        last_switch_at=None,
        switches_today=0,
        switch_cooldown=600.0,
        max_switches_per_day=6,
    )
    base.update(kw)
    return SelfHealState(**base)


def test_switch_when_primary_unhealthy_and_alt_healthy():
    s = state(domains=(mk("a.com", False, 0.0), mk("b.com", True, 1.0)))
    d = decide(s)
    assert d.action == "switch"
    assert d.target == "b.com"


def test_keep_when_primary_healthy():
    s = state(domains=(mk("a.com", True, 1.0), mk("b.com", True, 1.0)))
    assert decide(s).action == "keep"


def test_keep_during_cooldown():
    s = state(
        domains=(mk("a.com", False, 0.0), mk("b.com", True, 1.0)),
        last_switch_at=900.0,
        now=1000.0,
        switch_cooldown=600.0,
    )
    assert decide(s).action == "keep"


def test_escalate_when_daily_limit_reached():
    s = state(
        domains=(mk("a.com", False, 0.0), mk("b.com", True, 1.0)),
        switches_today=6,
        max_switches_per_day=6,
    )
    d = decide(s)
    assert d.action == "escalate"
    assert d.escalation == "switch_limit_reached"


def test_escalate_when_all_domains_down_no_override():
    s = state(domains=(mk("a.com", False, 0.0), mk("b.com", False, 0.1)))
    d = decide(s)
    assert d.action == "escalate"
    assert d.escalation == "all_domains_down"


def test_revert_and_escalate_when_override_degraded_and_no_healthy():
    s = state(
        active_primary="b.com",
        domains=(mk("a.com", False, 0.0), mk("b.com", False, 0.0)),
    )
    d = decide(s)
    assert d.action == "revert_escalate"
    assert d.target == "a.com"
    assert d.escalation == "all_domains_down"


def test_recover_when_configured_primary_healthy_again():
    s = state(
        active_primary="b.com",
        domains=(mk("a.com", True, 1.0), mk("b.com", True, 1.0)),
    )
    d = decide(s)
    assert d.action == "recover"
    assert d.target == "a.com"
