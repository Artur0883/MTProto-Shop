# Proxy Self-Healing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot autonomously detect a blocked TLS masking domain and switch link generation to a healthy one, with guardrails and human-readable admin escalation — keeping the proxy usable without manual intervention.

**Architecture:** A pure decision function (`self_heal_logic.py`, fully unit-tested) decides keep/switch/recover/escalate from a state snapshot. A background loop (`self_heal.py`) gathers the snapshot from the existing `TLSDomainPicker`, executes the action via an in-memory "active primary domain" override on the picker, and sends Telegram messages to the admin. No proxy restart, no config writes — fully reversible. Container restarts are already handled by the separately-added `autoheal`.

**Tech Stack:** Python 3.12, aiogram 3, pydantic-settings, structlog, pytest (new dev dependency).

**Spec:** `docs/superpowers/specs/2026-05-29-proxy-self-healing-design.md`

---

## File Structure

- Create: `bot/self_heal_logic.py` — pure decision brain, zero external imports.
- Create: `bot/self_heal.py` — background loop + admin messaging (side effects).
- Create: `tests/test_self_heal_logic.py` — unit tests for the decision brain.
- Create: `tests/conftest.py` — puts `bot/` on `sys.path` so tests import flat modules.
- Create: `requirements-dev.txt` — pytest.
- Modify: `bot/tls_domains.py` — in-memory `active_primary` override.
- Modify: `bot/proxy_manager.py:128-135` — `pick_primary_tls_domain()` honors override.
- Modify: `bot/config.py` — self-heal settings.
- Modify: `.env.example` — self-heal env block.
- Modify: `bot/main.py` — start/stop the self-heal task.
- Modify: `bot/admin.py:546-563` — show active domain in status.

---

## Task 1: Pure decision brain

**Files:**
- Create: `bot/self_heal_logic.py`
- Create: `tests/conftest.py`
- Create: `tests/test_self_heal_logic.py`
- Create: `requirements-dev.txt`

- [ ] **Step 1: Add pytest dev dependency**

Create `requirements-dev.txt`:

```
pytest>=8.0
```

- [ ] **Step 2: Add test path bootstrap**

Create `tests/conftest.py` (the bot is a flat module dir, run with `bot/` as cwd; tests need the same import root):

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_self_heal_logic.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_self_heal_logic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'self_heal_logic'`

- [ ] **Step 5: Write the implementation**

Create `bot/self_heal_logic.py`:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_self_heal_logic.py -v`
Expected: PASS — 7 passed

- [ ] **Step 7: Commit**

```bash
git add bot/self_heal_logic.py tests/test_self_heal_logic.py tests/conftest.py requirements-dev.txt
git commit -m "feat: pure decision brain for proxy self-healing"
```

---

## Task 2: Picker active-primary override

**Files:**
- Modify: `bot/tls_domains.py`
- Modify: `bot/proxy_manager.py:128-135`
- Test: `tests/test_picker_override.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_picker_override.py`:

```python
from tls_domains import TLSDomainPicker


def test_active_primary_defaults_to_none():
    p = TLSDomainPicker()
    assert p.active_primary is None


def test_set_and_clear_active_primary():
    p = TLSDomainPicker()
    p.set_active_primary("b.com")
    assert p.active_primary == "b.com"
    p.clear_active_primary()
    assert p.active_primary is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_picker_override.py -v`
Expected: FAIL — `AttributeError: 'TLSDomainPicker' object has no attribute 'active_primary'`

- [ ] **Step 3: Add the override to the picker**

In `bot/tls_domains.py`, in `TLSDomainPicker.__init__` (currently ends with `self._interval = DEFAULT_PROBE_INTERVAL_SECONDS`), add the field:

```python
    def __init__(self) -> None:
        self._stats: dict[str, DomainStats] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._interval = DEFAULT_PROBE_INTERVAL_SECONDS
        self._active_primary: str | None = None
```

Then add these three members to the class (place them right after `__init__`, before `_ensure_stats`):

```python
    @property
    def active_primary(self) -> str | None:
        """Domain the self-heal loop currently advertises instead of the
        configured primary. None means: use the configured primary."""
        return self._active_primary

    def set_active_primary(self, domain: str) -> None:
        self._active_primary = domain

    def clear_active_primary(self) -> None:
        self._active_primary = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_picker_override.py -v`
Expected: PASS — 2 passed

- [ ] **Step 5: Make link generation honor the override**

Replace `pick_primary_tls_domain()` in `bot/proxy_manager.py:128-135` with:

```python
def pick_primary_tls_domain() -> str:
    """Primary TLS domain for the main link.

    Honors the self-heal override (`picker.active_primary`) when set and still a
    configured domain; otherwise the configured primary. Keeps the main link
    predictable while letting self-heal steer away from a blocked domain.
    """
    settings = get_settings()
    try:
        from tls_domains import get_picker

        active = get_picker().active_primary
        if active and active in settings.fallback_tls_domains:
            return active
    except Exception:
        pass
    return settings.tls_domain
```

- [ ] **Step 6: Verify nothing else broke**

Run: `python -m py_compile bot/proxy_manager.py bot/tls_domains.py && echo OK`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add bot/tls_domains.py bot/proxy_manager.py tests/test_picker_override.py
git commit -m "feat: in-memory active-primary override on TLS picker"
```

---

## Task 3: Self-heal settings

**Files:**
- Modify: `bot/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add settings fields**

In `bot/config.py`, in the `Settings` class, immediately after the `healthcheck_ping_url` field (end of the `# --- Observability ---` block), add:

```python

    # --- Self-heal ---
    self_heal_enabled: bool = Field(default=True)
    self_heal_check_interval: float = Field(default=120.0, gt=0)
    self_heal_switch_cooldown: float = Field(default=600.0, ge=0)
    self_heal_max_switches_per_day: int = Field(default=6, ge=0)
```

(Field names map case-insensitively to `SELF_HEAL_*` env vars; no alias needed, matching how `log_level` reads `LOG_LEVEL`.)

- [ ] **Step 2: Document env vars**

In `.env.example`, after the `HEALTHCHECK_PING_URL=` block, add:

```
# ============== Само-лечение прокси (необязательно, по умолчанию включено) ==============
# Бот сам следит за маскировочными доменами и при блокировке переключает выдачу
# ссылок на здоровый домен. Трогать не обязательно.
SELF_HEAL_ENABLED=true
SELF_HEAL_CHECK_INTERVAL=120
SELF_HEAL_SWITCH_COOLDOWN=600
SELF_HEAL_MAX_SWITCHES_PER_DAY=6
```

- [ ] **Step 3: Verify settings load**

Run: `cd bot && python -c "from config import reload_settings; s=reload_settings(); print(s.self_heal_enabled, s.self_heal_check_interval, s.self_heal_switch_cooldown, s.self_heal_max_switches_per_day)"`
Expected: `True 120.0 600.0 6`

- [ ] **Step 4: Commit**

```bash
git add bot/config.py .env.example
git commit -m "feat: self-heal configuration settings"
```

---

## Task 4: Self-heal loop and admin messaging

**Files:**
- Create: `bot/self_heal.py`

- [ ] **Step 1: Write the loop module**

Create `bot/self_heal.py`:

```python
"""Background self-heal loop.

Detects a blocked masking domain (via the TLS picker probes) and switches the
advertised primary to a healthy one, with guardrails and human-readable admin
reporting. Decision brain lives in self_heal_logic (pure, tested); this module
gathers the snapshot, executes the action, and messages the admin.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
import time

from aiogram import Bot

from config import get_settings
from logging_setup import get_logger
from self_heal_logic import DomainHealth, SelfHealState, decide
from tls_domains import get_picker


logger = get_logger(__name__)


@dataclass
class _Runtime:
    last_switch_at: float | None = None
    switches_today: int = 0
    switch_day: date = field(default_factory=date.today)
    last_escalation: str | None = None


def _build_state(rt: _Runtime) -> SelfHealState:
    settings = get_settings()
    picker = get_picker()
    domains = tuple(
        DomainHealth(
            domain=s.domain,
            probed=s.last_probed_at is not None,
            last_ok=s.last_ok,
            success_rate=s.success_rate,
        )
        for s in picker.ranked()
    )
    return SelfHealState(
        configured_primary=settings.tls_domain,
        active_primary=picker.active_primary,
        domains=domains,
        now=time.monotonic(),
        last_switch_at=rt.last_switch_at,
        switches_today=rt.switches_today,
        switch_cooldown=settings.self_heal_switch_cooldown,
        max_switches_per_day=settings.self_heal_max_switches_per_day,
    )


async def _notify(bot: Bot, text: str) -> None:
    settings = get_settings()
    if settings.admin_id is None:
        return
    try:
        await bot.send_message(settings.admin_id, text)
    except Exception:
        logger.exception("event=self_heal_notify_failed")


def _escalation_text(kind: str) -> str:
    if kind == "all_domains_down":
        return (
            "❌ Все маскировочные домены недоступны с сервера — сам исправить не могу.\n\n"
            "Что сделать:\n"
            "1) Откройте меню: <code>mtp</code>\n"
            "2) Пункт 25 — проверьте кандидатов TLS-доменов с этого сервера\n"
            "3) Добавьте 2–3 новых рабочих домена\n"
            "4) Пересоздайте прокси (пункт обновления/перезапуска)\n\n"
            "Если домены с сервера живы, но клиенты не подключаются — возможно, "
            "заблокирован IP сервера: смените IP у хостера или поднимите прокси "
            "на новом IP."
        )
    if kind == "switch_limit_reached":
        return (
            "⚠️ Домен снова деградировал, но дневной лимит авто-переключений "
            "исчерпан.\n\nНужно внимание: проверьте список доменов "
            "(<code>mtp</code> → 25) и при необходимости добавьте новые рабочие домена."
        )
    return f"⚠️ Self-heal: {kind}"


async def self_heal_loop(bot: Bot) -> None:
    settings = get_settings()
    rt = _Runtime()
    logger.info("event=self_heal_started", interval=settings.self_heal_check_interval)
    while True:
        try:
            today = date.today()
            if today != rt.switch_day:
                rt.switch_day = today
                rt.switches_today = 0

            state = _build_state(rt)
            decision = decide(state)
            picker = get_picker()
            advertised = state.active_primary or state.configured_primary

            if decision.action == "keep":
                rt.last_escalation = None

            elif decision.action == "switch":
                picker.set_active_primary(decision.target)
                rt.last_switch_at = state.now
                rt.switches_today += 1
                rt.last_escalation = None
                logger.warning(
                    "event=self_heal_switch", frm=advertised, to=decision.target
                )
                await _notify(
                    bot,
                    f"⚠️ Домен <code>{advertised}</code> перестал отвечать. "
                    f"Переключил выдачу ссылок на <code>{decision.target}</code> — "
                    f"новые ссылки уже рабочие. Слежу дальше.",
                )

            elif decision.action == "recover":
                picker.clear_active_primary()
                rt.last_escalation = None
                logger.info("event=self_heal_recover", to=decision.target)
                await _notify(
                    bot,
                    f"✅ Домен <code>{decision.target}</code> снова стабилен. "
                    f"Вернул его как основной.",
                )

            elif decision.action == "revert_escalate":
                picker.clear_active_primary()
                logger.error(
                    "event=self_heal_revert_escalate", kind=decision.escalation
                )
                if rt.last_escalation != decision.escalation:
                    rt.last_escalation = decision.escalation
                    await _notify(bot, _escalation_text(decision.escalation))

            elif decision.action == "escalate":
                logger.error("event=self_heal_escalate", kind=decision.escalation)
                if rt.last_escalation != decision.escalation:
                    rt.last_escalation = decision.escalation
                    await _notify(bot, _escalation_text(decision.escalation))

        except Exception:
            logger.exception("event=self_heal_loop_error")
        await asyncio.sleep(settings.self_heal_check_interval)
```

- [ ] **Step 2: Verify it compiles**

Run: `python -m py_compile bot/self_heal.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add bot/self_heal.py
git commit -m "feat: self-heal background loop with admin escalation"
```

---

## Task 5: Wire the loop into the bot lifecycle

**Files:**
- Modify: `bot/main.py`

- [ ] **Step 1: Import the loop**

In `bot/main.py`, after the existing `from subscriptions import subscription_worker` import line, add:

```python
from self_heal import self_heal_loop
```

- [ ] **Step 2: Add the task handle**

In `main()`, where the task handles are declared (currently `worker_task`, `heartbeat_task`, `telemt_monitor_task` near `picker = get_picker()`), add a fourth:

```python
    worker_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None
    telemt_monitor_task: asyncio.Task | None = None
    self_heal_task: asyncio.Task | None = None
    picker = get_picker()
```

- [ ] **Step 3: Start the loop in on_startup**

In `on_startup`, update the `nonlocal` line and start the task after `telemt_monitor_task` is created:

```python
    async def on_startup() -> None:
        nonlocal heartbeat_task, telemt_monitor_task, worker_task, self_heal_task
        runtime.STARTED_AT = datetime.now(UTC)
        worker_task = asyncio.create_task(subscription_worker(bot))
        heartbeat_task = asyncio.create_task(bot_heartbeat_loop())
        telemt_monitor_task = asyncio.create_task(telemt_monitor_loop())
        if settings.self_heal_enabled:
            self_heal_task = asyncio.create_task(self_heal_loop(bot))
        picker.start()
```

(Leave the rest of `on_startup` — `picker.probe_all()` and the startup log — unchanged.)

- [ ] **Step 4: Cancel the loop in on_shutdown**

In `on_shutdown`, add `self_heal_task` to the cancellation tuple:

```python
        for task in (worker_task, heartbeat_task, telemt_monitor_task, self_heal_task):
```

- [ ] **Step 5: Verify it compiles**

Run: `python -m py_compile bot/main.py && echo OK`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add bot/main.py
git commit -m "feat: start self-heal loop in bot lifecycle"
```

---

## Task 6: Show active domain in admin status

**Files:**
- Modify: `bot/admin.py:546-563`

- [ ] **Step 1: Inject the active-domain line**

In `bot/admin.py`, replace the picker block at lines 546-563 (`picker = get_picker()` through the `else: tls_block = "   (ещё не пробованы)"`) with:

```python
    picker = get_picker()
    active_primary = picker.active_primary
    if active_primary:
        active_line = f"   🔀 Активный домен: {active_primary} (авто-переключён)"
    else:
        active_line = f"   📌 Активный домен: {settings.tls_domain} (основной)"
    ranked = picker.snapshot()
    if ranked:
        picker_lines = [active_line]
        for stats_dom in picker.ranked():
            latency = (
                f"{stats_dom.last_latency_ms:.0f} мс"
                if stats_dom.last_latency_ms is not None
                else "—"
            )
            mark = "✅" if stats_dom.last_ok else "❌"
            picker_lines.append(
                f"   {mark} {stats_dom.domain} · {latency} · "
                f"успех {int(stats_dom.success_rate * 100)}%"
            )
        tls_block = "\n".join(picker_lines)
    else:
        tls_block = active_line + "\n   (ещё не пробованы)"
```

Note: this assumes `settings` is already bound earlier in the function. If `python -m py_compile` passes but `settings` is undefined at runtime here, add `settings = get_settings()` as the first line of the replacement. Confirm by checking for an existing `settings = get_settings()` earlier in the same function.

- [ ] **Step 2: Verify it compiles**

Run: `python -m py_compile bot/admin.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add bot/admin.py
git commit -m "feat: show active TLS domain in admin status"
```

---

## Task 7: Full test run and deploy verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS — all tests (9: 7 logic + 2 picker) green.

- [ ] **Step 2: Compile-check every touched Python file**

Run: `python -m py_compile bot/self_heal_logic.py bot/self_heal.py bot/tls_domains.py bot/proxy_manager.py bot/config.py bot/main.py bot/admin.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Deploy on the VPS and observe**

On the server:

```bash
cd /opt/mtproto-shop
docker compose up -d --build
docker compose logs --tail=50 bot | grep self_heal
```

Expected: a line `event=self_heal_started`. Open the admin `/status` screen and confirm the new `Активный домен: … (основной)` line appears.

- [ ] **Step 4: (Optional, on live server) Check TeleMT for a connections metric**

The spec leaves open whether TeleMT exposes a connection-count metric (only `/v1/users` is used today). To explore for a future "traffic flatline" signal:

```bash
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://mtproto:9091/v1/stats', timeout=3).read()[:500])"
```

If this 404s, there is no stats endpoint — v1 relies on domain probes only (as designed). If it returns data, file a follow-up to add the traffic signal. Do not block v1 on this.

---

## Self-Review

**Spec coverage:**
- §2 self-heal switch → Task 1 (`switch`), Task 2 (override), Task 4 (execute). ✅
- §2 recover → Task 1 (`recover`), Task 4. ✅
- §2 escalation kinds (all_domains_down, switch_limit_reached) → Task 1, Task 4 (`_escalation_text`). ✅
- §3 detection via picker probes → Task 4 (`_build_state` reads `picker.ranked()`). ✅
- §3 traffic/connections signal (conditional) → Task 7 Step 4 (verification, not built — matches "open question"). ✅
- §4 guardrails (cooldown, daily limit, auto-revert, anti-spam) → Task 1 (cooldown/limit/revert), Task 4 (`last_escalation` dedup, daily reset). ✅
- §5 in-memory override mechanism → Task 2. ✅
- §6 human escalation messages → Task 4 (`_escalation_text`, switch/recover texts). ✅
- §7 components/files → Tasks 1-6. ✅
- §10 config env → Task 3. ✅
- §11 tests → Task 1 (all six cases + the healthy-keep case). ✅

**Placeholder scan:** No TBD/TODO; every code step is complete. ✅

**Type consistency:** `DomainHealth`/`SelfHealState`/`Decision` field names match between `self_heal_logic.py` (Task 1) and `self_heal.py` (`_build_state`, Task 4). Picker members `active_primary`/`set_active_primary`/`clear_active_primary` consistent across Tasks 2, 4, 6. `decide` action strings (`keep`/`switch`/`recover`/`revert_escalate`/`escalate`) match between Task 1 and Task 4 dispatch. ✅
