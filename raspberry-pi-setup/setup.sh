#!/usr/bin/env bash
# ILSport dart-ad — установка плеера на УЖЕ настроенный Raspberry Pi.
#
# Скрипт НЕ трогает дисплей-сервер, не меняет boot-режим, не настраивает watchdog.
# Если нужен hardware watchdog — отдельно: bash setup-watchdog.sh
#
# Запуск (по SSH, не из под root):
#   git clone https://github.com/captaindamien/dart-ad.git ~/dart-ad
#   bash ~/dart-ad/raspberry-pi-setup/setup.sh
# или одной строкой:
#   curl -fsSL https://raw.githubusercontent.com/captaindamien/dart-ad/master/raspberry-pi-setup/setup.sh | bash

set -euo pipefail

REPO_URL="https://github.com/captaindamien/dart-ad.git"
INSTALL_DIR="/opt/ilsport/dart-ad"
ENV_FILE="/etc/ilsport/env"
SERVICE_USER="${SUDO_USER:-$USER}"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"

if [[ "$EUID" -eq 0 ]]; then
  echo "Не запускай скрипт от root. sudo будет вызываться по месту." >&2
  exit 1
fi
if ! command -v sudo >/dev/null; then
  echo "sudo не установлен." >&2
  exit 1
fi

echo "=== ILSport dart-ad setup ==="
echo "  user: $SERVICE_USER"
echo "  home: $SERVICE_HOME"
echo ""

# --- диагностика графики (только информация, ничего не меняем) ----------------
echo ">>> Текущее состояние графики:"
echo "    XDG_SESSION_TYPE = ${XDG_SESSION_TYPE:-<не задан, ssh-сессия>}"
if command -v loginctl >/dev/null; then
  ACTIVE_SESSION="$(loginctl list-sessions --no-legend 2>/dev/null \
    | awk -v u="$SERVICE_USER" '$3==u{print $1; exit}')"
  if [[ -n "${ACTIVE_SESSION:-}" ]]; then
    SESSION_TYPE="$(loginctl show-session "$ACTIVE_SESSION" -p Type --value 2>/dev/null || echo '')"
    echo "    активная сессия $SERVICE_USER: type=$SESSION_TYPE"
    if [[ "$SESSION_TYPE" == "wayland" ]]; then
      echo ""
      echo "    ВНИМАНИЕ: сессия на Wayland. cv2.imshow в OpenCV полноценно работает"
      echo "    только на X11. Если плеер не запустится — переключи вручную:"
      echo "       sudo raspi-config  →  6 Advanced Options  →  Wayland  →  X11"
      echo "       sudo reboot"
      echo ""
    fi
  else
    echo "    активная графическая сессия $SERVICE_USER не найдена (это норма при ssh)."
  fi
fi
echo ""

# --- сбор конфигурации --------------------------------------------------------
read -rp "Server URL (например https://ilsport.ae): " SERVER_URL
read -rp "Machine Token (из админки): " MACHINE_TOKEN
read -rp "Hostname сервера для SSH-туннеля [${SERVER_URL#*://}]: " SERVER_HOST
SERVER_HOST="${SERVER_HOST:-${SERVER_URL#*://}}"
read -rp "Tunnel gateway user [tunnel]: " TUNNEL_USER
TUNNEL_USER="${TUNNEL_USER:-tunnel}"
read -rp "X offset второго монитора, px [1920]: " MONITOR_X_OFFSET
MONITOR_X_OFFSET="${MONITOR_X_OFFSET:-1920}"
read -rp "Sync interval, сек [300]: " SYNC_INTERVAL
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"
read -rp "Heartbeat interval, сек [30]: " HEARTBEAT_INTERVAL
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-30}"

# --- пакеты (минимум) ---------------------------------------------------------
echo ""
echo ">>> Устанавливаю зависимости (минимум, без X-пакетов)…"
sudo apt-get update -q
sudo apt-get install -y \
  git curl ca-certificates \
  python3 python3-pip python3-opencv python3-numpy \
  unclutter \
  autossh openssh-client

# --- git clone / pull ---------------------------------------------------------
echo ""
echo ">>> Клонирую/обновляю репозиторий в $INSTALL_DIR…"
sudo mkdir -p "$(dirname "$INSTALL_DIR")"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$(dirname "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
SETUP_DIR_SRC="$INSTALL_DIR/raspberry-pi-setup"

# --- /etc/ilsport/env ---------------------------------------------------------
echo ">>> Записываю $ENV_FILE…"
sudo mkdir -p "$(dirname "$ENV_FILE")"
sudo tee "$ENV_FILE" >/dev/null <<EOF
# Сгенерировано setup.sh $(date -Iseconds)
MACHINE_TOKEN=$MACHINE_TOKEN
SERVER_URL=$SERVER_URL
SERVER_HOST=$SERVER_HOST
TUNNEL_USER=$TUNNEL_USER
ADS_DIR=$INSTALL_DIR/public/ads
SYNC_INTERVAL=$SYNC_INTERVAL
HEARTBEAT_INTERVAL=$HEARTBEAT_INTERVAL
MONITOR_X_OFFSET=$MONITOR_X_OFFSET
INSTALL_DIR=$INSTALL_DIR
SERVICE_USER=$SERVICE_USER
EOF
sudo chown root:"$SERVICE_USER" "$ENV_FILE"
sudo chmod 640 "$ENV_FILE"

# --- SSH ключ для туннеля -----------------------------------------------------
SSH_DIR="$SERVICE_HOME/.ssh"
SSH_KEY="$SSH_DIR/ilsport_tunnel"
mkdir -p "$SSH_DIR"; chmod 700 "$SSH_DIR"
if [[ ! -f "$SSH_KEY" ]]; then
  echo ">>> Генерирую SSH-ключ: $SSH_KEY"
  ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "ilsport-pi-$(hostname)"
fi

# --- регистрация порта туннеля -----------------------------------------------
echo ">>> Регистрирую tunnel port на сервере…"
REG_RESP="$(curl -fsS -X POST -H "X-Machine-Token: $MACHINE_TOKEN" \
  "$SERVER_URL/api/display/register-tunnel" || true)"
TUNNEL_PORT="$(python3 - "$REG_RESP" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.loads(sys.argv[1])
    print((d.get("data") or d).get("port", ""))
except Exception:
    pass
PY
)"
if [[ -z "$TUNNEL_PORT" ]]; then
  echo "    не удалось получить порт автоматически (ответ: ${REG_RESP:-<пусто>})"
  read -rp "    введи tunnel port вручную: " TUNNEL_PORT
fi
echo "    tunnel port = $TUNNEL_PORT"
echo "TUNNEL_PORT=$TUNNEL_PORT" | sudo tee -a "$ENV_FILE" >/dev/null

# --- systemd units ------------------------------------------------------------
echo ">>> Устанавливаю systemd units…"
render_unit() {
  sed -e "s|@USER@|$SERVICE_USER|g" \
      -e "s|@HOME@|$SERVICE_HOME|g" \
      -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
      "$1"
}
for unit in dart-ad.service tunnel.service update.service update.timer; do
  render_unit "$SETUP_DIR_SRC/$unit" | sudo tee "/etc/systemd/system/ilsport-$unit" >/dev/null
done
sudo systemctl daemon-reload

# --- kiosk autostart в X-сессии ----------------------------------------------
echo ">>> Раскладываю kiosk-autostart в $SERVICE_HOME/.config/autostart/…"
mkdir -p "$SERVICE_HOME/.config/autostart"
cp "$SETUP_DIR_SRC/dart-ad-kiosk.desktop" "$SERVICE_HOME/.config/autostart/dart-ad-kiosk.desktop"
install -m 755 "$SETUP_DIR_SRC/kiosk-autostart.sh" "$SERVICE_HOME/.ilsport-kiosk-autostart.sh"

# --- enable timers/services ---------------------------------------------------
echo ">>> Активирую update.timer и tunnel.service…"
sudo systemctl enable --now ilsport-update.timer
sudo systemctl enable ilsport-tunnel.service
# tunnel НЕ стартуем сразу — нужен загруженный публичный ключ на сервере.
# dart-ad.service не enable — основной запуск идёт из X-autostart.

# --- финал --------------------------------------------------------------------
echo ""
echo "=========================================================="
echo "Установка завершена."
echo ""
echo "1. Передай этот публичный ключ админу сервера:"
echo ""
cat "$SSH_KEY.pub"
echo ""
echo "2. Когда ключ авторизован на сервере — стартуй туннель:"
echo "     sudo systemctl start ilsport-tunnel"
echo "     journalctl -u ilsport-tunnel -f"
echo ""
echo "3. Чтобы плеер поднялся — нужно войти в графическую X-сессию"
echo "   под пользователем $SERVICE_USER (физически или через autologin)."
echo "   Лог плеера: $SERVICE_HOME/.ilsport-player.log"
echo ""
echo "4. (опционально) Hardware watchdog:"
echo "     bash $SETUP_DIR_SRC/setup-watchdog.sh"
echo ""
echo "Полезные команды:"
echo "   systemctl status ilsport-tunnel"
echo "   systemctl list-timers ilsport-update.timer"
echo "   sudo systemctl start ilsport-update.service   # обновить вручную"
echo "=========================================================="
