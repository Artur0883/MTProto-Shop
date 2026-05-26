#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="/opt/mtproto-shop"
SENTINEL="$PROJECT_DIR/data/restart.request"
HEARTBEAT_DIR="$PROJECT_DIR/data/heartbeats"
HEARTBEAT="$HEARTBEAT_DIR/watcher.beat"
LOG="$PROJECT_DIR/data/restart.log"
PROXY_SENTINEL="$PROJECT_DIR/data/proxy.reload.request"
PROXY_LOG="$PROJECT_DIR/data/proxy-reload.log"
while true; do
  mkdir -p "$HEARTBEAT_DIR"
  touch "$HEARTBEAT"
  if [[ -f "$PROXY_SENTINEL" ]]; then
    rm -f "$PROXY_SENTINEL"
    if [[ -f "$PROXY_LOG" && $(stat -c %s "$PROXY_LOG") -gt 1048576 ]]; then
      mv "$PROXY_LOG" "${PROXY_LOG}.1"
    fi
    if cd "$PROJECT_DIR" && timeout 30s docker compose kill -s SIGUSR2 mtproto >> "$PROXY_LOG" 2>&1; then
      echo "[$(date -Iseconds)] proxy reload OK via SIGUSR2" >> "$PROXY_LOG"
    else
      echo "[$(date -Iseconds)] SIGUSR2 failed, fallback to restart" >> "$PROXY_LOG"
      timeout 30s docker compose restart mtproto >> "$PROXY_LOG" 2>&1 \
        || echo "[$(date -Iseconds)] proxy restart FAILED" >> "$PROXY_LOG"
    fi
  fi
  if [[ -f "$SENTINEL" ]]; then
    rm -f "$SENTINEL"
    if [[ -f "$LOG" && $(stat -c %s "$LOG") -gt 1048576 ]]; then
      mv "$LOG" "${LOG}.1"
    fi
    echo "[$(date -Iseconds)] restart triggered" >> "$LOG"
    cd "$PROJECT_DIR" && timeout 30s docker compose up -d --build --force-recreate bot support_bot mtproto >> "$LOG" 2>&1 || \
      echo "[$(date -Iseconds)] restart FAILED" >> "$LOG"
  fi
  sleep 5
done
