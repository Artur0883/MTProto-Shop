#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="/root/MTProto-Shop/MTProto-Shop"
SENTINEL="$PROJECT_DIR/data/restart.request"
HEARTBEAT_DIR="$PROJECT_DIR/data/heartbeats"
HEARTBEAT="$HEARTBEAT_DIR/watcher.beat"
LOG="$PROJECT_DIR/data/restart.log"
mkdir -p "$HEARTBEAT_DIR"
while true; do
  touch "$HEARTBEAT"
  if [[ -f "$SENTINEL" ]]; then
    rm -f "$SENTINEL"
    echo "[$(date -Iseconds)] restart triggered" >> "$LOG"
    cd "$PROJECT_DIR" && docker compose up -d --build --force-recreate bot support_bot mtproto >> "$LOG" 2>&1 || \
      echo "[$(date -Iseconds)] restart FAILED" >> "$LOG"
  fi
  sleep 5
done
