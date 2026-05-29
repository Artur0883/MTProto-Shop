# Multi-server Этап 2 — bot multi-node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Pure-logic tasks have unit tests (run with `.venv/Scripts/python.exe -m pytest`); refactor tasks of existing files require reading the current function first and preserving its behavior. Steps use checkbox (`- [ ]`).

**Goal:** Make the bot provision each client on every configured proxy node and hand the client a link for each node, so a client can switch to a healthy server if one IP is blocked — while staying backward-compatible (one node = today's behavior).

**Architecture:** Introduce a node list (`PROXY_NODES`). A pure `nodes.py` exposes the configured `ProxyNode`s (falls back to a single node from `SERVER_HOST`/`TELEMT_API_URL`). `telemt_client.api_call` becomes node-aware (per-node base URL + circuit breaker). `proxy_manager` provisions/revokes a client's secret on ALL nodes; link generation produces one link per node. A background reconciler keeps each node's user set in sync with active subscriptions. Admin status shows per-node health.

**Tech Stack:** Python 3.12, aiogram 3, aiohttp, pydantic-settings, structlog, pytest.

**Spec:** `docs/superpowers/specs/2026-05-29-multi-server-failover-design.md`
**Infra (done):** node B live at public `2.26.55.53`, API `http://100.87.48.113:9091` reachable from server A's host over Tailscale.

---

## File Structure

- Create `bot/nodes.py` — `ProxyNode` dataclass + `get_nodes()` / `primary_node()` (depends only on `config`). Pure-ish, unit-tested.
- Create `bot/reconcile_logic.py` — pure `diff_users()` (zero deps). Unit-tested.
- Create `bot/reconciler.py` — background loop using `diff_users` + telemt client per node. Side effects.
- Modify `bot/config.py` — add `proxy_nodes` (JSON list) field.
- Modify `bot/telemt_client.py` — `api_call(method, path, body, *, base_url=None)`; per-base-url session + circuit breaker registry; `is_available(base_url)`.
- Modify `bot/proxy_manager.py` — `create_secret`/`delete_secret`/`rotate_secret` iterate all nodes; new `build_node_links(secret)`.
- Modify `bot/client.py` — show one link per node (primary + per-node backups).
- Modify `bot/subscriptions.py` — expiry deletes the secret on all nodes.
- Modify `bot/main.py` — start the reconciler; per-node TeleMT monitor.
- Modify `bot/admin.py` — per-node status block.

---

## Task 0: Verify the bot container can reach node B's API (GATE — user runs on server A)

This whole phase assumes the bot's docker container can reach `http://100.87.48.113:9091`
(node B) through server A's host Tailscale. Confirm before writing code.

- [ ] **Step 1:** On server A (`root@3xui`, `/opt/mtproto-shop`):
```
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://100.87.48.113:9091/v1/users', timeout=5).read()[:60])"
```
Expected: prints the start of a JSON byte string (`b'{"ok":true...'`).
- [ ] **Step 2 (only if Step 1 fails):** the container can't reach the tailnet. Fix by adding to
the `bot` service in `docker-compose.yml`: `extra_hosts: ["host.docker.internal:host-gateway"]`
is NOT enough for tailnet routing — instead the simplest reliable fix is to point node B's
`api_url` at server A's own Tailscale IP is wrong too. The robust fix: run the proxy node's API
reachable via the host, then give the bot container access by adding the host Tailscale subnet
route — set in `docker-compose.yml` bot service: `dns: []` is irrelevant. **STOP and report**;
the controller will decide between (a) `network_mode: host` for the bot, or (b) a socat/relay on
the host. Do not proceed to Task 1 until Step 1 returns JSON.

---

## Task 1: Node configuration + `nodes.py`

**Files:** Modify `bot/config.py`; Create `bot/nodes.py`, `tests/test_nodes.py`.

- [ ] **Step 1: Write the failing test** — `tests/test_nodes.py`:
```python
import json
import os
import importlib


def _reload_with(env: dict):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import config
    importlib.reload(config)
    import nodes
    importlib.reload(nodes)
    return nodes


def test_single_node_fallback_when_unset():
    nodes = _reload_with({"PROXY_NODES": None, "SERVER_HOST": "1.2.3.4", "TELEMT_API_URL": "http://mtproto:9091"})
    ns = nodes.get_nodes()
    assert len(ns) == 1
    assert ns[0].public_host == "1.2.3.4"
    assert ns[0].api_url == "http://mtproto:9091"
    assert ns[0].primary is True


def test_multi_node_from_json():
    payload = json.dumps([
        {"name": "A", "public_host": "1.1.1.1", "api_url": "http://mtproto:9091", "primary": True},
        {"name": "B", "public_host": "2.2.2.2", "api_url": "http://100.87.48.113:9091", "primary": False},
    ])
    nodes = _reload_with({"PROXY_NODES": payload, "SERVER_HOST": "1.1.1.1"})
    ns = nodes.get_nodes()
    assert [n.name for n in ns] == ["A", "B"]
    assert nodes.primary_node(ns).name == "A"
```

- [ ] **Step 2: Run, expect fail** — `.venv/Scripts/python.exe -m pytest tests/test_nodes.py -v` → ModuleNotFoundError: nodes.

- [ ] **Step 3: Add config field** — in `bot/config.py`, after the `# --- Self-heal ---` block add:
```python

    # --- Proxy nodes (multi-server) ---
    # JSON list: [{"name","public_host","api_url","primary"}]. Empty => single node
    # derived from SERVER_HOST + TELEMT_API_URL (backward compatible).
    proxy_nodes: list[dict] = Field(default_factory=list)
```
pydantic-settings parses the `PROXY_NODES` env JSON into this list automatically.

- [ ] **Step 4: Create `bot/nodes.py`:**
```python
"""Configured proxy nodes. One node = today's single-server behavior."""
from __future__ import annotations

from dataclasses import dataclass

from config import get_settings


@dataclass(frozen=True)
class ProxyNode:
    name: str
    public_host: str
    api_url: str
    primary: bool


def get_nodes() -> list[ProxyNode]:
    settings = get_settings()
    raw = settings.proxy_nodes or []
    if raw:
        nodes = [
            ProxyNode(
                name=str(n.get("name") or "node"),
                public_host=str(n["public_host"]),
                api_url=str(n["api_url"]).rstrip("/"),
                primary=bool(n.get("primary", False)),
            )
            for n in raw
        ]
    else:
        nodes = [
            ProxyNode(
                name="default",
                public_host=settings.server_host,
                api_url=settings.telemt_api_url,
                primary=True,
            )
        ]
    if nodes and not any(n.primary for n in nodes):
        first = nodes[0]
        nodes[0] = ProxyNode(first.name, first.public_host, first.api_url, True)
    return nodes


def primary_node(nodes: list[ProxyNode]) -> ProxyNode:
    for node in nodes:
        if node.primary:
            return node
    return nodes[0]
```

- [ ] **Step 5: Run, expect pass.** **Step 6: Commit** is deferred — the user wants one commit at the end (do NOT commit per task).

---

## Task 2: Reconcile diff logic (`reconcile_logic.py`)

**Files:** Create `bot/reconcile_logic.py`, `tests/test_reconcile_logic.py`.

- [ ] **Step 1: Failing test** — `tests/test_reconcile_logic.py`:
```python
from reconcile_logic import diff_users


def test_add_missing_and_remove_stale():
    desired = {"tg_1", "tg_2"}
    present = {"tg_2", "tg_3"}
    protected = {"shop_bootstrap"}
    to_add, to_remove = diff_users(desired, present, protected)
    assert to_add == {"tg_1"}
    assert to_remove == {"tg_3"}


def test_protected_never_removed():
    to_add, to_remove = diff_users(set(), {"shop_bootstrap"}, {"shop_bootstrap"})
    assert to_remove == set()
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement `bot/reconcile_logic.py`:**
```python
"""Pure node-reconciliation diff. No I/O."""
from __future__ import annotations


def diff_users(
    desired: set[str], present: set[str], protected: set[str]
) -> tuple[set[str], set[str]]:
    """Return (to_add, to_remove) to make a node's user set match `desired`.
    `protected` usernames are never removed (e.g. the system bootstrap user)."""
    to_add = desired - present
    to_remove = (present - desired) - protected
    return to_add, to_remove
```

- [ ] **Step 4: Run, expect pass.** Do not commit (one commit at end).

---

## Task 3: Node-aware TeleMT client

**Files:** Modify `bot/telemt_client.py`.

Read the current file first. Keep the retry/circuit-breaker logic identical; change only WHERE
the base URL comes from.

- [ ] **Step 1:** Replace the single module-global `_session`/`_circuit` with per-base-url
registries:
```python
_sessions: dict[str, aiohttp.ClientSession] = {}
_circuits: dict[str, _CircuitBreaker] = {}
```
- [ ] **Step 2:** Change `_ensure_initialized()` to `_ensure_initialized(base_url: str)` returning
the session+circuit for that base_url (create on first use, keyed by base_url). Default base_url
= `get_settings().telemt_api_url`.
- [ ] **Step 3:** `api_call(method, path, body=None, *, base_url=None)` — resolve
`base_url = base_url or get_settings().telemt_api_url`; build `url = f"{base_url}{path}"`; use that
base_url's session+circuit. All existing retry/CB behavior unchanged.
- [ ] **Step 4:** `async def is_available(base_url: str | None = None) -> bool:` — passes base_url
through to `api_call("GET", "/v1/users", base_url=base_url)`.
- [ ] **Step 5:** `close_telemt()` closes ALL sessions in `_sessions` and clears the dict.
- [ ] **Step 6:** `get_circuit_state(base_url: str | None = None)` returns that base_url's CB state.
- [ ] **Verify:** `.venv/Scripts/python.exe -m py_compile bot/telemt_client.py` (aiohttp import is
fine to compile; do not run). No per-task commit.

---

## Task 4: Provision across all nodes (`proxy_manager.py`)

**Files:** Modify `bot/proxy_manager.py`.

Read the current `create_secret`, `delete_secret`, `rotate_secret`, `get_link` first. The pattern:
each currently calls `api_call(..., )` (default node). Make each iterate `get_nodes()` and pass
`base_url=node.api_url`.

- [ ] **Step 1:** Import `from nodes import get_nodes, primary_node`.
- [ ] **Step 2:** `create_secret(client_id, secret=None)` — create the same secret on **every**
node (call the existing per-node create against each `node.api_url`). Return the secret. If a
non-primary node is unreachable, log `event=node_provision_failed` and continue (the reconciler
fixes it later); if the **primary** fails and there's no previous secret, raise as today.
- [ ] **Step 3:** `delete_secret(client_id)` — delete on **every** node; swallow
`ClientNotFoundError` per node; log failures, don't abort.
- [ ] **Step 4:** `rotate_secret(client_id)` — rotate on the primary to get the new secret, then
set that same secret on the other nodes (delete+create or the rotate endpoint). Keep the existing
`RotateCooldownError` handling on the primary.
- [ ] **Step 5:** Add `build_node_links(secret: str) -> list[tuple[str, str]]` returning
`(node_name, link)` for each node, using the existing `build_tls_proxy_link(node.public_host,
settings.proxy_port, secret, pick_primary_tls_domain())`.
- [ ] **Verify:** `.venv/Scripts/python.exe -m py_compile bot/proxy_manager.py`. No per-task commit.

---

## Task 5: Show a link per node to the client (`client.py`)

**Files:** Modify `bot/client.py`, `bot/keyboards.py`.

- [ ] **Step 1:** In `send_granted_access` and `send_my_link`, when more than one node is
configured, after the primary link append a short section listing each node's link labelled by
node name (reuse `build_node_links`). When only one node, behave exactly as today.
- [ ] **Step 2:** Keep wording simple (per project memory): e.g. "🔁 Запасной сервер
(<name>)" — no jargon. Provide the connect button for the primary and a list/buttons for the rest.
- [ ] **Verify:** `.venv/Scripts/python.exe -m py_compile bot/client.py bot/keyboards.py`. No per-task commit.

---

## Task 6: Expiry cleans up all nodes (`subscriptions.py`)

**Files:** Modify `bot/subscriptions.py`.

- [ ] **Step 1:** `expire_subscriptions` already calls `delete_secret(client_id)`. Since Task 4
made `delete_secret` delete on every node, no logic change is needed here — just confirm by
reading. If `delete_secret`'s signature changed, update the call. No per-task commit.

---

## Task 7: Reconciler + per-node monitor (`reconciler.py`, `main.py`)

**Files:** Create `bot/reconciler.py`; Modify `bot/main.py`.

- [ ] **Step 1: Create `bot/reconciler.py`** — `async def reconciler_loop(bot)` that every
`settings.reconcile_interval` seconds, for each node: fetch its users via
`api_call("GET", "/v1/users", base_url=node.api_url)`, compute `desired` = active-subscription
client_ids from the DB, call `diff_users(desired, present, {settings.telemt_system_user})`, then
create missing / delete stale on that node. Wrap the whole body in try/except (like
`subscription_worker`). Skip nodes that are unreachable (log, continue).
- [ ] **Step 2: config** — add `reconcile_interval: float = Field(default=300.0, gt=0)` to
`config.py`.
- [ ] **Step 3: main.py** — start `reconciler_loop` as a background task in `on_startup` (guarded
by "more than one node configured"); cancel it in `on_shutdown` (add to the tuple). Extend the
existing TeleMT monitor to check each node's `is_available(node.api_url)` and alert the admin
naming the down node.
- [ ] **Verify:** `.venv/Scripts/python.exe -m py_compile bot/reconciler.py bot/main.py`. No per-task commit.

---

## Task 8: Per-node status in admin (`admin.py`)

**Files:** Modify `bot/admin.py`.

- [ ] **Step 1:** In the system-status handler, add a "Серверы (узлы)" block listing each node:
name, public host, and reachable/not via `is_available(node.api_url)`. Keep the existing single
"Активный домен" line. Plain language. `py_compile` to verify. No per-task commit.

---

## Task 9: Configure nodes on server A, deploy, verify end-to-end (user-run)

- [ ] **Step 1:** On server A, add to `.env`:
`PROXY_NODES=[{"name":"RU-1","public_host":"31.76.77.226","api_url":"http://mtproto:9091","primary":true},{"name":"EU-2","public_host":"2.26.55.53","api_url":"http://100.87.48.113:9091","primary":false}]`
- [ ] **Step 2:** `docker compose up -d --build` on server A.
- [ ] **Step 3:** In the bot, issue a trial to a test account → confirm the client receives TWO
links (server `31.76.77.226` and `2.26.55.53`), and both connect from Telegram.
- [ ] **Step 4:** On node B, `curl -fsS http://100.87.48.113:9091/v1/users` (from A) shows the new
`tg_<id>` user appeared (provisioned on B too).
- [ ] **Step 5:** Let a subscription expire (or disable) → confirm the user is removed from BOTH
nodes (reconciler/cleanup).

---

## Self-Review

**Spec coverage:** §6.1 node list → Task 1; §6.2 node-aware client → Task 3; §6.3 provision all
nodes → Task 4; §6.4 dual links → Task 5; §6.5 cleanup all nodes → Task 6; §6.6 reconciler →
Tasks 2+7; §6.7 per-node monitor → Task 7; §6.8 admin → Task 8; config → Tasks 1+7; verify →
Task 9. Connectivity assumption (§3) gated by Task 0. ✅

**Placeholder scan:** Task 0 Step 2 intentionally says STOP-and-report (a real decision gate, not
a code placeholder). Refactor tasks (3,4) describe interface changes precisely and require reading
the current function — code shown for new modules (1,2,7) is complete.

**Type consistency:** `ProxyNode(name, public_host, api_url, primary)` and `get_nodes()` /
`primary_node()` consistent across Tasks 1,4,5,7,8. `api_call(..., base_url=)` and
`is_available(base_url=)` consistent across Tasks 3,4,7,8. `diff_users(desired, present,
protected)` consistent across Tasks 2,7.

**Out of scope:** auto IP/DNS failover; bot/DB redundancy; 3rd node — all deferred per spec §10.
