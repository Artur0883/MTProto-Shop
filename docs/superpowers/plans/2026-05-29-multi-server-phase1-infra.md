# Multi-server — Этап 1 (infra + connectivity) Implementation Plan

> **For agentic workers:** this phase is live-infrastructure ops on two servers, not
> repo code. Execute interactively: run one step, verify its output, then continue.
> The only repo-code task is Task 5 (codify into the installer), which is optional and
> done after the manual flow is proven once.

**Goal:** Stand up server B as a proxy-only node and connect it privately to server A over
Tailscale, so server A's bot can reach server B's TeleMT API. No bot code changes yet.

**Architecture:** Server A keeps bot+proxy+DB. Server B runs only the telemt proxy
(public 443 for clients) + Tailscale (private link for management). A↔B private channel via
Tailscale; B's management API (`:9091`) firewalled to accept only A's Tailscale IP.

**Tech Stack:** Docker + docker compose, telemt (`ghcr.io/telemt/telemt`), Tailscale, ufw.

**Spec:** `docs/superpowers/specs/2026-05-29-multi-server-failover-design.md`

**Done-criterion for Этап 1:** from server A, `curl http://<B_TS_IP>:9091/v1/users` returns
JSON (B's API reachable over Tailscale); the same request from a public host is refused.

---

## Prerequisites (one-time)

- **Server B:** fresh Ubuntu/Debian, root access, different country/IP than A (confirmed).
- **Tailscale account** (free): sign up at tailscale.com, then create a reusable **auth key**
  in the admin console (Settings → Keys → Generate auth key). You'll paste it on both servers.
- Server A repo is at `/opt/mtproto-shop` (existing).
- You have A's public IP `31.76.77.226` and will note B's public IP after creation.

---

## Task 1: Connect A and B over Tailscale

**Run on SERVER A:**

- [ ] **Step 1: Install Tailscale on A**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

- [ ] **Step 2: Bring A onto the tailnet**

```bash
sudo tailscale up --auth-key=<YOUR_TAILSCALE_AUTH_KEY> --hostname=mtproto-a
```

- [ ] **Step 3: Record A's Tailscale IP**

```bash
tailscale ip -4
```
Expected: an address like `100.x.y.z`. **Write it down as A_TS_IP** — you'll need it on B.

**Run on SERVER B:**

- [ ] **Step 4: Install Tailscale on B**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

- [ ] **Step 5: Bring B onto the tailnet**

```bash
sudo tailscale up --auth-key=<YOUR_TAILSCALE_AUTH_KEY> --hostname=mtproto-b
```

- [ ] **Step 6: Record B's Tailscale IP**

```bash
tailscale ip -4
```
Expected: an address like `100.a.b.c`. **Write it down as B_TS_IP.**

- [ ] **Step 7: Verify the private link works**

On A, run (substitute B_TS_IP):
```bash
ping -c 3 <B_TS_IP>
```
Expected: replies from B_TS_IP. If this works, A and B see each other privately.

---

## Task 2: Stand up the telemt proxy on server B (proxy-only)

**Run on SERVER B.**

- [ ] **Step 1: Install Docker**

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker version
```
Expected: Docker server+client versions print without error.

- [ ] **Step 2: Create the node directory**

```bash
mkdir -p /opt/mtproto-node/telemt && cd /opt/mtproto-node
```

- [ ] **Step 3: Write the proxy-only compose file**

Create `/opt/mtproto-node/docker-compose.yml`:
```yaml
services:
  mtproto:
    image: ghcr.io/telemt/telemt:latest
    container_name: mtproto-node-proxy
    restart: unless-stopped
    working_dir: /etc/telemt
    healthcheck:
      test: ["CMD", "/app/telemt", "healthcheck", "/etc/telemt/config.toml", "--mode", "liveness"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 1g
    mem_reservation: 128m
    ulimits:
      nofile: {soft: 65536, hard: 262144}
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "5"}
    ports:
      - "443:443/tcp"
      - "9091:9091/tcp"
    volumes:
      - ./telemt:/etc/telemt:rw
```

Note: `9091` is published here but will be firewalled to A only in Task 3.

- [ ] **Step 4: Write B's telemt config**

Create `/opt/mtproto-node/telemt/config.toml` (substitute `<B_PUBLIC_IP>` and `<A_TS_IP>`;
the `[censorship]` block MUST match server A's domains so links work on both):
```toml
[general]
use_middle_proxy = false
log_level = "normal"

[general.modes]
classic = false
secure = false
tls = true

[general.links]
show = "*"
public_host = "<B_PUBLIC_IP>"
public_port = 443

[server]
port = 443

[server.api]
enabled = true
listen = "0.0.0.0:9091"
whitelist = ["127.0.0.1/32", "::1/128", "<A_TS_IP>/32"]
read_only = false

[[server.listeners]]
ip = "0.0.0.0"

[censorship]
tls_domain = "reso.ru"
tls_domains = ["1c.ru", "3dnews.ru", "www.ingos.ru", "www.soglasie.ru", "www.sogaz.ru", "magnit.ru", "www.perekrestok.ru", "rivegauche.ru", "www.rendez-vous.ru"]
mask = true
tls_emulation = true
tls_front_dir = "tlsfront"

[access.users]
shop_bootstrap = "<RUN: openssl rand -hex 16>"
```
For the bootstrap secret, run `openssl rand -hex 16` and paste the value.

- [ ] **Step 5: Set ownership and start**

```bash
chown -R 65532:65532 /opt/mtproto-node/telemt
chmod 700 /opt/mtproto-node/telemt && chmod 600 /opt/mtproto-node/telemt/config.toml
cd /opt/mtproto-node && docker compose up -d
docker compose ps
```
Expected: `mtproto-node-proxy` is `Up` and `(healthy)` within ~40s.

- [ ] **Step 6: Verify the proxy listens publicly on 443**

```bash
ss -ltn 'sport = :443'
```
Expected: a listening socket on `:443`.

---

## Task 3: Lock down B's API to server A only

**Run on SERVER B.**

- [ ] **Step 1: Install and enable ufw with public 443**

```bash
apt-get update && apt-get install -y ufw
ufw allow 22/tcp
ufw allow 443/tcp
```

- [ ] **Step 2: Allow API (9091) only from A's Tailscale IP, deny otherwise**

```bash
ufw allow from <A_TS_IP> to any port 9091 proto tcp
ufw deny 9091/tcp
ufw --force enable
ufw status verbose
```
Expected: `443 ALLOW Anywhere`; `9091 ALLOW <A_TS_IP>`; `9091 DENY Anywhere`.

---

## Task 4: Verify the Этап-1 done-criterion

- [ ] **Step 1: From SERVER A, reach B's API over Tailscale**

```bash
curl -fsS http://<B_TS_IP>:9091/v1/users
```
Expected: a JSON response (list of users — likely just `shop_bootstrap`). This proves A's
bot host can manage B. **This is the success signal for Этап 1.**

- [ ] **Step 2: Confirm the API is NOT reachable publicly**

From any machine that is NOT on your tailnet (e.g. your laptop without Tailscale):
```bash
curl -m 5 http://<B_PUBLIC_IP>:9091/v1/users
```
Expected: timeout / connection refused (good — public can't reach the API).

- [ ] **Step 3: Confirm B's proxy is usable as a proxy**

From server A:
```bash
curl -m 5 -v telnet://<B_PUBLIC_IP>:443 2>&1 | head -5
```
Expected: connection opens on 443 (the proxy port is publicly reachable for clients).

---

## Task 5 (optional, after the manual flow works): codify into the installer

Once Tasks 1-4 succeed once, capture the flow as a `mtp` menu option so future nodes are
one guided action (honors "minimize + automate setup", see project memory).

**Files:**
- Modify: `manage.sh` — add menu item `28) Установить как дополнительный прокси-узел` and a
  `setup_proxy_node` function that runs Tasks 2-3 automatically (installs Docker, writes the
  node compose+config from the same template, prompts for the Tailscale auth key and A's
  Tailscale IP, runs `tailscale up`, applies ufw rules, brings up the proxy, prints B_TS_IP +
  B_PUBLIC_IP for adding on A).
- Modify: `manage.sh` — add menu item `29) Добавить прокси-узел в бота (на сервере A)` that
  records the node (name, public IP, `http://<B_TS_IP>:9091`) for Этап 2's `PROXY_NODES`.

This task is bash (no unit-test harness in this repo); verification is re-running the guided
install on a throwaway node and confirming Task 4 passes. Defer the full code until the
manual runbook above is validated, so the script encodes exactly what worked.

---

## Self-Review

**Spec coverage (Этап 1 scope from spec §11):**
- Role: B = proxy-only node → Task 2 (proxy-only compose, no bot). ✅
- Tailscale A↔B → Task 1. ✅
- B's API reachable from A over Tailscale, firewalled to A → Tasks 3-4. ✅
- Masking domains match A (links valid on both) → Task 2 Step 4 `[censorship]` block. ✅
- Installer role-choice (minimize/automate) → Task 5 (codify after manual proof). ✅
- Done-criterion (`GET /v1/users` from A) → Task 4 Step 1. ✅

**Placeholder scan:** Substitution values (`<A_TS_IP>`, `<B_TS_IP>`, `<B_PUBLIC_IP>`, auth key,
bootstrap secret) are runtime values captured by the listed commands, not unresolved design
gaps — acceptable for an ops runbook. No "TBD" requirements.

**Consistency:** `<A_TS_IP>` used identically in B's config whitelist (Task 2.4) and ufw rule
(Task 3.2). `<B_TS_IP>` used identically in verify (Task 4.1) and recorded in Task 1.6. The
`[censorship]` domains match the live set configured on A.

**Out of scope (Этап 2, separate plan):** bot multi-node config (`PROXY_NODES`), dual
provisioning/links/cleanup, reconciler, per-node monitoring, admin display.
