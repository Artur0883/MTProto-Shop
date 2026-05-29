from proxy_watchdog_logic import should_restart_proxy

BASE = dict(failure_threshold=3, seconds_since_last_restart=None, restart_cooldown=600.0)


def test_restarts_when_alive_but_not_serving_sustained():
    assert should_restart_proxy(api_up=True, port_ok=False, consecutive_failures=3, **BASE) is True


def test_no_restart_while_serving():
    assert should_restart_proxy(api_up=True, port_ok=True, consecutive_failures=0, **BASE) is False


def test_no_restart_below_threshold():
    assert should_restart_proxy(api_up=True, port_ok=False, consecutive_failures=2, **BASE) is False


def test_no_restart_when_api_down():
    # Whole proxy dead -> Docker/autoheal handles it, not the watchdog.
    assert should_restart_proxy(api_up=False, port_ok=False, consecutive_failures=10, **BASE) is False


def test_respects_cooldown():
    assert should_restart_proxy(
        api_up=True, port_ok=False, consecutive_failures=5,
        failure_threshold=3, seconds_since_last_restart=120.0, restart_cooldown=600.0,
    ) is False


def test_restarts_after_cooldown():
    assert should_restart_proxy(
        api_up=True, port_ok=False, consecutive_failures=5,
        failure_threshold=3, seconds_since_last_restart=700.0, restart_cooldown=600.0,
    ) is True
