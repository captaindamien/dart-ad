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
#   curl -fsSL https://raw.githubusercontent.com/captaindamien/dart-ad/main/raspberry-pi-setup/setup.sh | bash

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
# Проверка ввода здесь не формальность. На одном из автоматов в SERVER_URL
# уехала кириллическая «р» вместо латинской h (раскладка при наборе):
# curl отвергал такой адрес, register-tunnel молча провалился, а heartbeat не
# уходил вообще — машина просто никогда не появилась в дэшборде, и найти
# причину удалось только через двое суток. Ловим это на вводе.
ascii_only() {  # ascii_only <строка> — 0, если только печатаемый ASCII
  ! printf '%s' "$1" | LC_ALL=C grep -q '[^ -~]'
}

check_server() {  # check_server <url> <token> — печатает диагноз, возвращает 0/1
  local code
  # curl при обрыве связи и печатает 000, и возвращает ненулевой код — без
  # `|| true` подстановка склеила бы вывод с запасным значением в «000000».
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
    -H "X-Machine-Token: $2" "$1/api/display/playlist" 2>/dev/null || true)"
  code="${code:-000}"
  case "$code" in
    200)     echo "    OK: сервер отвечает, токен принят."; return 0 ;;
    401|403) echo "    Сервер отверг Machine Token (HTTP $code) — проверь токен в админке." ;;
    000)     echo "    Сервер недоступен по адресу $1 — проверь URL и сеть." ;;
    *)       echo "    Неожиданный ответ HTTP $code от $1." ;;
  esac
  return 1
}

while :; do
  read -rp "Server URL (например https://ilsport.ae): " SERVER_URL
  SERVER_URL="$(printf '%s' "$SERVER_URL" | tr -d '[:space:]')"
  SERVER_URL="${SERVER_URL%/}"
  if ! ascii_only "$SERVER_URL"; then
    echo "    В адресе непечатаемые или не-ASCII символы (кириллица?). Проверь раскладку и набери заново."
    continue
  fi
  if [[ ! "$SERVER_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
    echo "    Нужен адрес вида https://ilsport.ae — схема http/https, без пути в конце."
    continue
  fi

  read -rp "Machine Token (из админки): " MACHINE_TOKEN
  MACHINE_TOKEN="$(printf '%s' "$MACHINE_TOKEN" | tr -d '[:space:]')"
  if [[ -z "$MACHINE_TOKEN" ]] || ! ascii_only "$MACHINE_TOKEN"; then
    echo "    Токен пустой или содержит посторонние символы. Скопируй его из админки заново."
    continue
  fi

  if ! command -v curl >/dev/null; then
    echo "    curl ещё не установлен — связь проверим после установки пакетов."
    break
  fi
  echo ">>> Проверяю связь с сервером…"
  check_server "$SERVER_URL" "$MACHINE_TOKEN" && break
  echo "    Повтори ввод."
done

DEFAULT_HOST="${SERVER_URL#*://}"; DEFAULT_HOST="${DEFAULT_HOST%%[:/]*}"
read -rp "Hostname сервера для SSH-туннеля [$DEFAULT_HOST]: " SERVER_HOST
SERVER_HOST="${SERVER_HOST:-$DEFAULT_HOST}"
read -rp "Tunnel gateway user [tunnel]: " TUNNEL_USER
TUNNEL_USER="${TUNNEL_USER:-tunnel}"
read -rp "X offset второго монитора, px [1920]: " MONITOR_X_OFFSET
MONITOR_X_OFFSET="${MONITOR_X_OFFSET:-1920}"
read -rp "Sync interval, сек [300]: " SYNC_INTERVAL
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"
read -rp "Heartbeat interval, сек [15]: " HEARTBEAT_INTERVAL
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-15}"

# --- пакеты (минимум) ---------------------------------------------------------
echo ""
echo ">>> Устанавливаю зависимости (минимум, без X-пакетов)…"
sudo apt-get update -q
sudo apt-get install -y \
  git curl ca-certificates \
  python3 python3-pip python3-opencv python3-numpy \
  mpv \
  unclutter wmctrl \
  autossh openssh-client

# --- связь с сервером: добор проверки, если curl не было на старте ------------
echo ""
echo ">>> Проверяю связь с сервером…"
if ! check_server "$SERVER_URL" "$MACHINE_TOKEN"; then
  echo "    Установка прервана: без связи с сервером агент всё равно не заработает." >&2
  exit 1
fi

# --- gpu_mem для V4L2 M2M H.264 декодера на Pi 4 ------------------------------
# mpv с --hwdec=v4l2m2m-copy требует минимум ~128 MB GPU-памяти.
# На Pi 5 строка gpu_mem игнорируется, поэтому правка безопасна для обеих моделей.
CONFIG_TXT=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
  if [[ -f "$candidate" ]]; then
    CONFIG_TXT="$candidate"
    break
  fi
done
if [[ -n "$CONFIG_TXT" ]]; then
  CURRENT_GPU_MEM="$(awk -F= '/^[[:space:]]*gpu_mem[[:space:]]*=/{print $2}' "$CONFIG_TXT" | tr -d ' ' | tail -1)"
  if [[ -z "$CURRENT_GPU_MEM" || "$CURRENT_GPU_MEM" -lt 128 ]]; then
    echo ">>> Поднимаю gpu_mem=128 в $CONFIG_TXT (было: ${CURRENT_GPU_MEM:-<не задано>})"
    sudo sed -i '/^[[:space:]]*gpu_mem[[:space:]]*=/d' "$CONFIG_TXT"
    echo 'gpu_mem=128' | sudo tee -a "$CONFIG_TXT" >/dev/null
    GPU_MEM_CHANGED=1
  else
    echo ">>> gpu_mem уже >= 128 ($CURRENT_GPU_MEM), оставляю как есть."
    GPU_MEM_CHANGED=0
  fi
else
  echo ">>> config.txt не найден, gpu_mem не правлю."
  GPU_MEM_CHANGED=0
fi

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

# --- регистрация туннеля + автообмен ключами ----------------------------------
echo ">>> Регистрирую tunnel port и обмениваюсь ключами с сервером…"
# Серверу шлём ровно "ssh-ed25519 <base64>" без комментария — так требует валидатор.
PUB_KEY="$(awk '{print $1" "$2}' "$SSH_KEY.pub")"
REG_RESP="$(curl -fsS -X POST \
  -H "X-Machine-Token: $MACHINE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"public_key\":\"$PUB_KEY\"}" \
  "$SERVER_URL/api/display/register-tunnel" || true)"

json_field() {  # json_field <json> <key>
  python3 - "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.loads(sys.argv[1])
    v = (d.get("data") or d).get(sys.argv[2])
    if v is not None:
        print(v)
except Exception:
    pass
PY
}
TUNNEL_PORT="$(json_field "$REG_RESP" port)"
SERVER_PUB_KEY="$(json_field "$REG_RESP" server_public_key)"

if [[ -z "$TUNNEL_PORT" ]]; then
  # Раньше здесь предлагался ручной ввод порта — и это дважды выходило боком:
  # введённый на глаз порт оказывался занят другим автоматом, а его ключ при
  # этом на сервер не уехал, так что autossh уходил в вечный цикл рестартов
  # раз в 15 секунд (счётчик доходил до 11 тысяч). Порт раздаёт только сервер.
  echo "" >&2
  echo "ОШИБКА: сервер не выдал tunnel port." >&2
  echo "  ответ register-tunnel: ${REG_RESP:-<пусто>}" >&2
  echo "  Порт назначает сервер и только он — вводить его вручную нельзя:" >&2
  echo "  чужой порт уже занят другим автоматом, а ключ этой Pi на сервер не попал." >&2
  echo "  Разберись с причиной (SERVER_URL, токен, доступность сервера) и запусти setup.sh заново." >&2
  exit 1
fi
echo "    tunnel port = $TUNNEL_PORT"
echo "TUNNEL_PORT=$TUNNEL_PORT" | sudo tee -a "$ENV_FILE" >/dev/null

# Ключ сервера — в authorized_keys, чтобы веб-терминал дэшборда мог заходить.
# Пишем только то, что похоже на ключ — защита от мусора в ответе.
if [[ -n "$SERVER_PUB_KEY" && "$SERVER_PUB_KEY" =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+ ]]; then
  AUTH_KEYS="$SSH_DIR/authorized_keys"
  touch "$AUTH_KEYS"; chmod 600 "$AUTH_KEYS"
  grep -qxF "$SERVER_PUB_KEY" "$AUTH_KEYS" || echo "$SERVER_PUB_KEY" >> "$AUTH_KEYS"
  echo "    ключ сервера добавлен в $AUTH_KEYS — веб-терминал заработает сразу"
else
  SERVER_PUB_KEY=""
  echo "    сервер не вернул server_public_key — обмен ключами в ручном режиме (см. финал)"
fi

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

# --- sshd: без него обратный туннель бесполезен -------------------------------
# Туннель пробрасывает 221xx на порт 22 самой Pi. В Raspberry Pi OS ssh выключен
# по умолчанию, и на первых автоматах туннель поднимался, а веб-терминал дэшборда
# всё равно упирался в «Connection reset»: на том конце никто не слушал.
echo ">>> Включаю sshd (нужен для веб-терминала через обратный туннель)…"
sudo systemctl enable --now ssh

# --- kiosk autostart в X-сессии ----------------------------------------------
echo ">>> Раскладываю kiosk-autostart в $SERVICE_HOME/.config/autostart/…"
mkdir -p "$SERVICE_HOME/.config/autostart"
cp "$SETUP_DIR_SRC/dart-ad-kiosk.desktop" "$SERVICE_HOME/.config/autostart/dart-ad-kiosk.desktop"
install -m 755 "$SETUP_DIR_SRC/kiosk-autostart.sh" "$SERVICE_HOME/.ilsport-kiosk-autostart.sh"

# --- enable timers/services ---------------------------------------------------
echo ">>> Активирую update.timer и tunnel.service…"
sudo systemctl enable --now ilsport-update.timer
TUNNEL_STARTED=0
if [[ -n "$SERVER_PUB_KEY" ]]; then
  # Ключ автомата уже на сервере (AuthorizedKeysCommand) — стартуем сразу.
  sudo systemctl enable --now ilsport-tunnel.service
  sleep 5
  if systemctl is-active --quiet ilsport-tunnel.service; then
    TUNNEL_STARTED=1
  else
    echo "    туннель пока не поднялся — autossh ретраит каждые 15 с (journalctl -u ilsport-tunnel -f)"
  fi
else
  # Старый бэкенд без автообмена: нужен загруженный публичный ключ на сервере.
  sudo systemctl enable ilsport-tunnel.service
fi
# dart-ad.service не enable — основной запуск идёт из X-autostart.

# --- финал --------------------------------------------------------------------
echo ""
echo "=========================================================="
echo "Установка завершена."
echo ""
if [[ "$TUNNEL_STARTED" -eq 1 ]]; then
  echo "1. Ключи обменяны с сервером автоматически, туннель уже запущен —"
  echo "   ручная раскладка ключей не нужна. Проверка:"
  echo "     systemctl status ilsport-tunnel"
else
  echo "1. Передай этот публичный ключ админу сервера"
  echo "   (в /home/tunnel/.ssh/authorized_keys, см. INSTALL.md → Ручной режим):"
  echo ""
  cat "$SSH_KEY.pub"
  echo ""
  echo "2. Когда ключ авторизован на сервере — стартуй туннель:"
  echo "     sudo systemctl start ilsport-tunnel"
  echo "     journalctl -u ilsport-tunnel -f"
fi
echo ""
echo "3. Чтобы плеер поднялся — нужно войти в графическую X-сессию"
echo "   под пользователем $SERVICE_USER (физически или через autologin)."
echo "   Лог плеера: $SERVICE_HOME/.ilsport-player.log"
echo ""
echo "4. (опционально) Hardware watchdog:"
echo "     bash $SETUP_DIR_SRC/setup-watchdog.sh"
if [[ "${GPU_MEM_CHANGED:-0}" -eq 1 ]]; then
  echo ""
  echo "ВНИМАНИЕ: gpu_mem изменён — нужна перезагрузка, чтобы HW-декодер заработал:"
  echo "     sudo reboot"
fi
echo ""
echo "Полезные команды:"
echo "   systemctl status ilsport-tunnel"
echo "   systemctl list-timers ilsport-update.timer"
echo "   sudo systemctl start ilsport-update.service   # обновить вручную"
echo "=========================================================="
