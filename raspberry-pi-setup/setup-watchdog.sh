#!/usr/bin/env bash
# Опциональный hardware watchdog для Pi.
# Запускай только если нужен аппаратный ребут при зависании системы.
#
# Делает три вещи (каждая — после подтверждения):
#   1. ставит пакет watchdog
#   2. бэкапит и перезаписывает /etc/watchdog.conf (с привязкой к pid плеера)
#   3. добавляет dtparam=watchdog=on в /boot/firmware/config.txt (требует ребут)
#
# Запуск:
#   bash /opt/ilsport/dart-ad/raspberry-pi-setup/setup-watchdog.sh

set -euo pipefail

if [[ "$EUID" -eq 0 ]]; then
  echo "Не запускай от root, sudo будет вызываться по месту." >&2
  exit 1
fi

confirm() {
  local q="$1"
  read -rp "$q [y/N]: " a
  [[ "${a,,}" == "y" || "${a,,}" == "yes" ]]
}

echo "=== ILSport watchdog setup ==="

# --- пакет --------------------------------------------------------------------
if ! dpkg -s watchdog >/dev/null 2>&1; then
  if confirm "Установить пакет 'watchdog'?"; then
    sudo apt-get update -q
    sudo apt-get install -y watchdog
  else
    echo "Отмена."
    exit 0
  fi
fi

# --- /etc/watchdog.conf -------------------------------------------------------
CONF=/etc/watchdog.conf
NEW_CONF="$(cat <<'EOF'
# Сгенерировано setup-watchdog.sh (ILSport dart-ad)
watchdog-device = /dev/watchdog
watchdog-timeout = 15
interval        = 5
max-load-1      = 24
realtime        = yes
priority        = 1

# Привязка к процессу плеера: kiosk-autostart.sh пишет pid в этот файл.
# Если файла нет дольше realtime-таймаута — watchdog перезагрузит Pi.
pidfile = /run/ilsport-dart-ad.pid
EOF
)"

if [[ -f "$CONF" ]]; then
  echo "Существующий $CONF будет сохранён в $CONF.bak-$(date +%s)"
  if confirm "Перезаписать $CONF новым конфигом?"; then
    sudo cp -a "$CONF" "$CONF.bak-$(date +%s)"
    echo "$NEW_CONF" | sudo tee "$CONF" >/dev/null
  else
    echo "Конфиг не тронут. Учти, что для контроля плеера в нём должна быть строка:"
    echo "    pidfile = /run/ilsport-dart-ad.pid"
  fi
else
  echo "$NEW_CONF" | sudo tee "$CONF" >/dev/null
fi

# --- /boot/firmware/config.txt -----------------------------------------------
CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"

if grep -q "^dtparam=watchdog=on" "$CONFIG_TXT" 2>/dev/null; then
  echo "$CONFIG_TXT уже содержит dtparam=watchdog=on — пропускаю."
else
  echo "В $CONFIG_TXT нет 'dtparam=watchdog=on' — без этого hardware watchdog не активируется."
  if confirm "Добавить строку (потребует ПЕРЕЗАГРУЗКУ)?"; then
    echo "dtparam=watchdog=on" | sudo tee -a "$CONFIG_TXT" >/dev/null
    NEED_REBOOT=1
  fi
fi

# --- enable -------------------------------------------------------------------
if confirm "Включить и запустить watchdog.service?"; then
  sudo systemctl enable --now watchdog
  systemctl status watchdog --no-pager | head -n 10 || true
fi

echo ""
echo "=== готово ==="
if [[ -n "${NEED_REBOOT:-}" ]]; then
  echo "Был изменён $CONFIG_TXT. Перезагрузи Pi: sudo reboot"
fi
