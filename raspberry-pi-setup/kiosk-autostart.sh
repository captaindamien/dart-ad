#!/usr/bin/env bash
# kiosk-autostart.sh — стартует в X-сессии при autologin.
# Гасит DPMS/screensaver, прячет курсор, запускает main.py и перезапускает его при падении.

set -u

ENV_FILE="/etc/ilsport/env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

INSTALL_DIR="${INSTALL_DIR:-/opt/ilsport/dart-ad}"
MONITOR_X_OFFSET="${MONITOR_X_OFFSET:-1920}"
LOG="$HOME/.ilsport-player.log"
LOG_MAX_MB="${PLAYER_LOG_MAX_MB:-20}"

# Ротация перед каждым запуском main.py. Без неё лог рос неограниченно:
# на стенде без карты захвата агент падал и перезапускался каждые 5 секунд,
# и один только перебор устройств давал ~28 МБ варнингов OpenCV в сутки.
# Держим одно поколение — .1 нужен ровно для того, чтобы разобрать причину
# падения, случившегося прямо перед ротацией.
rotate_log() {
  [[ -f "$LOG" ]] || return 0
  local bytes
  bytes="$(stat -c %s "$LOG" 2>/dev/null || echo 0)"
  if (( bytes / 1048576 >= LOG_MAX_MB )); then
    mv -f "$LOG" "$LOG.1"
    echo "[$(date -Iseconds)] лог превысил ${LOG_MAX_MB}M, предыдущий сохранён в $LOG.1" >> "$LOG"
  fi
}

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
  # Перечитываем env перед каждым запуском. Обёртка живёт всю X-сессию, и
  # раньше main.py получал окружение, прочитанное при логине: исправленный
  # на диске SERVER_URL двое суток не доезжал до плеера, пока Pi не
  # перезагрузят. Теперь достаточно перезапуска плеера (его делает update.sh).
  [[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
  INSTALL_DIR="${INSTALL_DIR:-/opt/ilsport/dart-ad}"
  MONITOR_X_OFFSET="${MONITOR_X_OFFSET:-1920}"
  rotate_log
  cd "$INSTALL_DIR" || { echo "[$(date -Iseconds)] no $INSTALL_DIR" >> "$LOG"; sleep 10; continue; }
  python3 -u main.py "$MONITOR_X_OFFSET" >> "$LOG" 2>&1 &
  PID=$!
  echo "$PID" | sudo tee "$PIDFILE" >/dev/null 2>&1 || echo "$PID" > "$PIDFILE"
  wait "$PID" || true
  echo "[$(date -Iseconds)] main.py exited, restart in 5s" >> "$LOG"
  sleep 5
done
