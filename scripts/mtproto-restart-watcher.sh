#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="/opt/mtproto-shop"
SENTINEL="$PROJECT_DIR/data/restart.request"
HEARTBEAT_DIR="$PROJECT_DIR/data/heartbeats"
HEARTBEAT="$HEARTBEAT_DIR/watcher.beat"
LOG="$PROJECT_DIR/data/restart.log"
while true; do
  mkdir -p "$HEARTBEAT_DIR"
  touch "$HEARTBEAT"
  if [[ -f "$SENTINEL" ]]; then
    request="$(head -n 1 "$SENTINEL" 2>/dev/null || true)"
    rm -f "$SENTINEL"
    if [[ -f "$LOG" && $(stat -c %s "$LOG") -gt 1048576 ]]; then
      mv "$LOG" "${LOG}.1"
    fi
    if [[ "$request" == "mtproto" ]]; then
      services=(mtproto)
      build_args=()
    else
      services=(bot support_bot mtproto)
      build_args=(--build)
    fi
    echo "[$(date -Iseconds)] restart triggered: ${services[*]}" >> "$LOG"
    cd "$PROJECT_DIR" && timeout 120s docker compose up -d "${build_args[@]}" --force-recreate "${services[@]}" >> "$LOG" 2>&1 || \
      echo "[$(date -Iseconds)] restart FAILED" >> "$LOG"
  fi
  sleep 5
done
