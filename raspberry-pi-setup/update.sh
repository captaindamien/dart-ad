#!/usr/bin/env bash
# update.sh — git pull + рестарт плеера.
# Запускается из ilsport-update.service (раз в час по таймеру).

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/ilsport/dart-ad}"
LOG="/var/log/ilsport-update.log"

ts() { date -Iseconds; }

{
  echo "[$(ts)] === update start ==="
  cd "$INSTALL_DIR"

  BEFORE="$(git rev-parse HEAD)"
  git fetch --quiet origin
  git reset --hard "origin/$(git rev-parse --abbrev-ref HEAD)"
  AFTER="$(git rev-parse HEAD)"

  if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "[$(ts)] no changes ($BEFORE)"
    exit 0
  fi

  echo "[$(ts)] $BEFORE -> $AFTER"

  if git diff --name-only "$BEFORE" "$AFTER" | grep -q '^requirements.txt$'; then
    echo "[$(ts)] requirements.txt changed — pip install"
    pip3 install --break-system-packages -r requirements.txt || true
  fi

  # перезапускаем X-сессионный процесс (он стартует из autostart, поэтому
  # просто убиваем — supervisor-обёртки нет, перезапустим через kiosk-обёртку).
  pkill -f "python3 .*main.py" || true
  echo "[$(ts)] player killed — kiosk-autostart перезапустит его автоматически"

  # если используется systemd-fallback — перезапустим и его
  if systemctl is-active --quiet ilsport-dart-ad.service; then
    systemctl restart ilsport-dart-ad.service || true
  fi

  echo "[$(ts)] === update done ==="
} | sudo tee -a "$LOG" >/dev/null
