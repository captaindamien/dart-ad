#!/usr/bin/env bash
# kiosk-autostart.sh — стартует в X-сессии при autologin.
# Гасит DPMS/screensaver, прячет курсор, запускает main.py и перезапускает его при падении.

set -u

ENV_FILE="/etc/ilsport/env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

INSTALL_DIR="${INSTALL_DIR:-/opt/ilsport/dart-ad}"
MONITOR_X_OFFSET="${MONITOR_X_OFFSET:-1920}"
LOG="$HOME/.ilsport-player.log"

# --- ждём, пока поднимется DISPLAY ---
for _ in $(seq 1 30); do
  [[ -n "${DISPLAY:-}" ]] && break
  export DISPLAY=:0
  sleep 1
done

# --- гасим screen blank / DPMS / xset s off ---
xset s off || true
xset -dpms || true
xset s noblank || true

# --- прячем курсор ---
pgrep -x unclutter >/dev/null || unclutter -idle 0 -root &

# --- запись pid для watchdog ---
PIDFILE="/run/ilsport-dart-ad.pid"

# --- supervised loop ---
echo "[$(date -Iseconds)] kiosk-autostart up; INSTALL_DIR=$INSTALL_DIR offset=$MONITOR_X_OFFSET" >> "$LOG"

while true; do
  cd "$INSTALL_DIR" || { echo "[$(date -Iseconds)] no $INSTALL_DIR" >> "$LOG"; sleep 10; continue; }
  python3 -u main.py "$MONITOR_X_OFFSET" >> "$LOG" 2>&1 &
  PID=$!
  echo "$PID" | sudo tee "$PIDFILE" >/dev/null 2>&1 || echo "$PID" > "$PIDFILE"
  wait "$PID" || true
  echo "[$(date -Iseconds)] main.py exited, restart in 5s" >> "$LOG"
  sleep 5
done
