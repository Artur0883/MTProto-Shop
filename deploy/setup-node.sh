#!/usr/bin/env bash
#
# setup-node.sh — install this server as a proxy-only node for MTProto-Shop.
#
# Run on a fresh Ubuntu/Debian server that is already on the Tailscale tailnet:
#   bash setup-node.sh <MAIN_SERVER_TAILSCALE_IP>
# e.g.
#   bash setup-node.sh 100.74.212.63
#
# Idempotent: safe to re-run. Auto-detects this node's public + Tailscale IPs,
# installs Docker if needed, writes a clean telemt config, runs the proxy, and
# locks the management API (9091) to the main server's Tailscale IP via ufw.
set -Eeuo pipefail

A_TS_IP="${1:-}"
if [[ -z "$A_TS_IP" ]]; then
  echo "ERROR: pass the main server's Tailscale IP."
  echo "Usage: bash setup-node.sh <MAIN_SERVER_TAILSCALE_IP>   (e.g. 100.74.212.63)"
  exit 1
fi

NODE_DIR="/opt/mtproto-node"
CONTAINER="mtproto-node-proxy"
IMAGE="ghcr.io/telemt/telemt:latest"

# Masking domains — keep in sync with the main server (server A).
TLS_DOMAIN="reso.ru"
TLS_DOMAINS='["1c.ru", "3dnews.ru", "www.ingos.ru", "www.soglasie.ru", "www.sogaz.ru", "magnit.ru", "www.perekrestok.ru", "rivegauche.ru", "www.rendez-vous.ru"]'

echo "==> Preparing $NODE_DIR"
mkdir -p "$NODE_DIR/telemt"

echo "==> Ensuring Docker is installed"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

echo "==> Detecting addresses"
if ! command -v tailscale >/dev/null 2>&1; then
  echo "ERROR: tailscale is not installed / not running on this node. Install and 'tailscale up' first."
  exit 1
fi
B_TS_IP="$(tailscale ip -4 | head -1)"
B_PUBLIC_IP="$(curl -4 -fsS --max-time 10 ifconfig.me)"
if [[ -z "$B_TS_IP" || -z "$B_PUBLIC_IP" ]]; then
  echo "ERROR: could not determine this node's Tailscale IP ($B_TS_IP) or public IP ($B_PUBLIC_IP)."
  exit 1
fi
echo "    public IP   : $B_PUBLIC_IP"
echo "    tailscale IP: $B_TS_IP"

echo "==> Writing $NODE_DIR/telemt/config.toml"
SECRET="$(openssl rand -hex 16)"
cat > "$NODE_DIR/telemt/config.toml" <<EOF
[general]
use_middle_proxy = false
log_level = "normal"
[general.modes]
classic = false
secure = false
tls = true
[general.links]
show = "*"
public_host = "$B_PUBLIC_IP"
public_port = 443
[server]
port = 443
[server.api]
enabled = true
listen = "0.0.0.0:9091"
whitelist = ["127.0.0.1/32", "::1/128", "100.64.0.0/10", "172.16.0.0/12", "10.0.0.0/8"]
read_only = false
[[server.listeners]]
ip = "0.0.0.0"
[censorship]
tls_domain = "$TLS_DOMAIN"
tls_domains = $TLS_DOMAINS
mask = true
tls_emulation = true
tls_front_dir = "tlsfront"
[access.users]
shop_bootstrap = "$SECRET"
EOF
chown -R 65532:65532 "$NODE_DIR/telemt"
chmod 700 "$NODE_DIR/telemt"
chmod 600 "$NODE_DIR/telemt/config.toml"

echo "==> Starting the proxy container"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -w /etc/telemt \
  --cap-drop ALL --cap-add NET_BIND_SERVICE \
  --security-opt no-new-privileges:true \
  --memory 1g \
  --ulimit nofile=65536:262144 \
  --health-cmd '/app/telemt healthcheck /etc/telemt/config.toml --mode liveness' \
  --health-interval 30s --health-timeout 5s --health-retries 5 --health-start-period 30s \
  --log-opt max-size=10m --log-opt max-file=5 \
  -p 443:443/tcp \
  -p "${B_TS_IP}:9091:9091" \
  -v "$NODE_DIR/telemt:/etc/telemt:rw" \
  "$IMAGE"

echo "==> Configuring firewall (ufw)"
if ! command -v ufw >/dev/null 2>&1; then
  apt-get update && apt-get install -y ufw
fi
ufw allow 22/tcp
ufw allow 443/tcp
ufw allow from "$A_TS_IP" to any port 9091 proto tcp
ufw deny 9091/tcp
ufw --force enable

echo
echo "=================== NODE READY ==================="
echo "Public IP (client links) : $B_PUBLIC_IP"
echo "Tailscale IP (bot API)   : $B_TS_IP"
echo "API endpoint for the bot : http://$B_TS_IP:9091"
echo "=================================================="
sleep 15
docker ps --filter "name=$CONTAINER"
echo "--- port 443 ---"
ss -ltn 'sport = :443' || true
