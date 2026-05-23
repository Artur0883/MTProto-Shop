#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="/root/MTProto-Shop/MTProto-Shop"
SENTINEL="$PROJECT_DIR/data/restart.request"
HEARTBEAT_DIR="$PROJECT_DIR/data/heartbeats"
HEARTBEAT="$HEARTBEAT_DIR/watcher.beat"
LOG="$PROJECT_DIR/data/restart.log"
PROXY_SENTINEL="$PROJECT_DIR/data/proxy.reload.request"
PROXY_LOG="$PROJECT_DIR/data/proxy-reload.log"
mkdir -p "$HEARTBEAT_DIR"
while true; do
  touch "$HEARTBEAT"
  if [[ -f "$PROXY_SENTINEL" ]]; then
    rm -f "$PROXY_SENTINEL"
    if cd "$PROJECT_DIR" && docker compose kill -s SIGUSR2 mtproto >> "$PROXY_LOG" 2>&1; then
      echo "[$(date -Iseconds)] proxy reload OK via SIGUSR2" >> "$PROXY_LOG"
    else
      echo "[$(date -Iseconds)] SIGUSR2 failed, fallback to restart" >> "$PROXY_LOG"
      docker compose restart mtproto >> "$PROXY_LOG" 2>&1 \
        || echo "[$(date -Iseconds)] proxy restart FAILED" >> "$PROXY_LOG"
    fi
  fi
  if [[ -f "$SENTINEL" ]]; then
    rm -f "$SENTINEL"
    echo "[$(date -Iseconds)] restart triggered" >> "$LOG"
    cd "$PROJECT_DIR" && docker compose up -d --build --force-recreate bot support_bot mtproto >> "$LOG" 2>&1 || \
      echo "[$(date -Iseconds)] restart FAILED" >> "$LOG"
  fi
  sleep 5
done
