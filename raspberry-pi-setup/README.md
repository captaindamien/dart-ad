# Развёртывание dart-ad на Raspberry Pi (по SSH)

Скрипт ставится на **уже настроенный** Pi 4/5 (Raspberry Pi OS Desktop). Он не трогает дисплей-сервер, не меняет boot-режим, не настраивает autologin и не лезет в `/boot/firmware/config.txt`. Только то, что нужно для плеера: пакеты, код, `/etc/ilsport/env`, ssh-ключ туннеля и systemd-юниты.

## Требования к Pi (должно быть уже настроено)

- Raspberry Pi OS Desktop (Bookworm), Pi 4 или Pi 5.
- Входит в **X11**-сессию под целевым пользователем (`echo $XDG_SESSION_TYPE` → `x11`). На Wayland `cv2.imshow` работает нестабильно; если ты на Wayland — `sudo raspi-config` → Advanced → Wayland → X11, затем reboot.
- Autologin в desktop (или другой способ входа в графическую сессию под целевым пользователем) — без графической сессии плеер не поднимется.
- Подключён 2-й монитор и USB-карта захвата (HDMI in).
- Есть интернет и ssh-доступ.

## Установка

По SSH под нужным пользователем (не от root):

```bash
git clone https://github.com/captaindamien/dart-ad.git ~/dart-ad
bash ~/dart-ad/raspberry-pi-setup/setup.sh
```

или одной строкой:

```bash
curl -fsSL https://raw.githubusercontent.com/captaindamien/dart-ad/master/raspberry-pi-setup/setup.sh | bash
```

Скрипт спросит:
- `Server URL` — например `https://ilsport.ae`
- `Machine Token` — из админки (`machines.api_key`)
- `Hostname сервера для SSH-туннеля` — по умолчанию хост из Server URL
- `Tunnel gateway user` — `tunnel`
- `X offset второго монитора` — обычно ширина основного экрана (`1920`)
- интервалы sync/heartbeat — оставь дефолты

Что произойдёт:
1. apt поставит `python3-opencv numpy unclutter autossh` и др.
2. `git clone` в `/opt/ilsport/dart-ad` (или `git pull`, если уже есть).
3. Запишется `/etc/ilsport/env` (mode 640, owner `root:$USER`).
4. Сгенерируется `~/.ssh/ilsport_tunnel(.pub)`.
5. Дёрнется `POST /api/display/register-tunnel`, порт допишется в env.
6. Установятся 4 systemd-юнита: `ilsport-tunnel.service`, `ilsport-update.{service,timer}`, `ilsport-dart-ad.service` (последний — headless-fallback, **не enable**).
7. В `~/.config/autostart/dart-ad-kiosk.desktop` ляжет autostart, который при логине в X-сессию поднимает плеер через `~/.ilsport-kiosk-autostart.sh`.
8. В консоль выведется публичный ключ и инструкция, что делать дальше.

Перезагрузка после `setup.sh` **не нужна**.

## После установки

### 1. Авторизовать ключ Pi на сервере

Скрипт в конце выводит содержимое `~/.ssh/ilsport_tunnel.pub`. Передай его админу сервера, чтобы он добавил в `~tunnel/.ssh/authorized_keys` с ограничениями:

```
restrict,port-forwarding,command="echo tunnel only" ssh-ed25519 AAAA... ilsport-pi-<hostname>
```

И в `/etc/ssh/sshd_config` сервера:

```
GatewayPorts no
AllowTcpForwarding yes
PermitOpen localhost:22100-22199
```

### 2. Запустить туннель

```bash
sudo systemctl start ilsport-tunnel
journalctl -u ilsport-tunnel -f
```

С сервера подключение к Pi:

```bash
ssh -p <TUNNEL_PORT> <pi_user>@localhost
```

(`TUNNEL_PORT` — поле `machines.tunnel_ssh_port` или последняя строка `/etc/ilsport/env`).

### 3. Проверить плеер

Плеер запускается **в X-сессии**, не из systemd. Если ты подключён по SSH, его не видно — нужен autologin или физический вход. Логи:

```bash
tail -f ~/.ilsport-player.log
pgrep -fa "python3 .*main.py"
```

## Опционально: hardware watchdog

Если нужен аппаратный ребут при зависании Pi:

```bash
bash /opt/ilsport/dart-ad/raspberry-pi-setup/setup-watchdog.sh
```

Этот скрипт **спросит подтверждение** перед каждым действием: установка пакета, перезапись `/etc/watchdog.conf` (с бэкапом), добавление `dtparam=watchdog=on` в `/boot/firmware/config.txt` (требует ребут).

## Авто-обновление

`ilsport-update.timer` срабатывает раз в час (с 5-минутным jitter). `update.sh`:

- `git fetch && git reset --hard origin/<branch>`
- если изменился `requirements.txt` — `pip install --break-system-packages -r requirements.txt`
- `pkill -f "python3 .*main.py"` (kiosk-autostart поднимет обратно)
- если активен `ilsport-dart-ad.service` — `systemctl restart`

Принудительно сейчас:

```bash
sudo systemctl start ilsport-update.service
tail /var/log/ilsport-update.log
```

## Headless-отладка (без второго монитора)

`ilsport-dart-ad.service` запускает `main.py` под `EnvironmentFile=/etc/ilsport/env`. Учти: `cv2.imshow` требует DISPLAY, поэтому юнит сработает только если есть запущенный X-сервер. По умолчанию **не enable** — иначе будет конкурировать с X-autostart за капчер-карту. Включать руками:

```bash
sudo systemctl enable --now ilsport-dart-ad.service
journalctl -u ilsport-dart-ad -f
```

## Архитектура запуска

```
boot
 └─> autologin в X-сессию (pi/lightdm/labwc → X11)
       └─> ~/.config/autostart/dart-ad-kiosk.desktop
             └─> ~/.ilsport-kiosk-autostart.sh
                   ├─ ждёт DISPLAY=:0
                   ├─ xset s off, xset -dpms, unclutter
                   └─ while true; python3 main.py $MONITOR_X_OFFSET; sleep 5; done
                          ├─ pid → /run/ilsport-dart-ad.pid (для watchdog)
                          └─ stdout → ~/.ilsport-player.log
```

`main.py` сам:
- качает плейлист с `GET /api/display/playlist`,
- скачивает новые ролики через `GET /api/display/videos/:filename`,
- шлёт `POST /api/display/heartbeat` каждые `HEARTBEAT_INTERVAL` секунд с метриками (cpu_temp, ram, disk, local_ip, uptime, current_video).

## Откат

```bash
sudo systemctl disable --now ilsport-tunnel ilsport-update.timer ilsport-dart-ad watchdog 2>/dev/null || true
sudo rm -f /etc/systemd/system/ilsport-*.service /etc/systemd/system/ilsport-*.timer
sudo rm -rf /opt/ilsport /etc/ilsport
rm -f ~/.config/autostart/dart-ad-kiosk.desktop ~/.ilsport-kiosk-autostart.sh
sudo systemctl daemon-reload
```
