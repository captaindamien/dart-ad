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
curl -fsSL https://raw.githubusercontent.com/captaindamien/dart-ad/main/raspberry-pi-setup/setup.sh | bash
```

Скрипт спросит:
- `Server URL` — например `https://ilsport.ae`. Значение проверяется сразу: только
  ASCII (кириллическая «р» вместо `h` уже стоила двух суток разбирательств),
  схема `http/https`, и тут же выполняется живой запрос к API с введённым токеном.
  Пока сервер не ответит `200`, скрипт переспрашивает URL и токен.
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
5. Дёрнется `POST /api/display/register-tunnel` с публичным ключом Pi: порт допишется в env, ключ Pi сохранится на сервере (sshd выдаёт его через AuthorizedKeysCommand), а ключ сервера из ответа ляжет в `~/.ssh/authorized_keys` — для веб-терминала дэшборда. Если порт не пришёл, установка **прерывается с ошибкой**: порт раздаёт только сервер, вручную его вводить нельзя (см. «Почему нельзя вводить порт руками»).
6. Включится `sshd` (`systemctl enable --now ssh`) — без него обратный туннель упирается в закрытый порт 22 на самой Pi.
7. Установятся 4 systemd-юнита: `ilsport-tunnel.service`, `ilsport-update.{service,timer}`, `ilsport-dart-ad.service` (последний — headless-fallback, **не enable**).
8. В `~/.config/autostart/dart-ad-kiosk.desktop` ляжет autostart, который при логине в X-сессию поднимает плеер через `~/.ilsport-kiosk-autostart.sh`.
9. Туннель стартует сразу — ключи уже обменяны. Если сервер не вернул `server_public_key` (бэкенд без автообмена), скрипт выведет публичный ключ и переключится в ручной обмен ключами (см. ниже).

Перезагрузка после `setup.sh` **не нужна**.

## После установки

### 1. Проверить туннель

```bash
systemctl status ilsport-tunnel
journalctl -u ilsport-tunnel -f
```

### Почему нельзя вводить порт руками

Раньше при провале `register-tunnel` скрипт предлагал ввести `TUNNEL_PORT`
вручную. Дважды это закончилось одинаково: введённый на глаз порт уже
принадлежал другому автомату, а ключ этой Pi на сервер не уехал вовсе. Итог —
autossh с `ExitOnForwardFailure yes` падает, systemd поднимает его через
`RestartSec=15`, и так до бесконечности: на одном автомате счётчик рестартов
дошёл до 11 тысяч, а в `auth.log` сервера копились `Failed password for tunnel`
каждые 15 секунд. Теперь скрипт в этой ситуации просто останавливается.

### Ручной обмен ключами (fallback — если сервер не вернул server_public_key)

Скрипт в конце выводит содержимое `~/.ssh/ilsport_tunnel.pub`. Передай его админу сервера, чтобы он добавил в `~tunnel/.ssh/authorized_keys` с ограничениями:

```
restrict,port-forwarding,command="echo tunnel only" ssh-ed25519 AAAA... ilsport-pi-<hostname>
```

И в `/etc/ssh/sshd_config` сервера (разово; для автообмена там же нужен блок `Match User tunnel` с `AuthorizedKeysCommand` — см. `docs/tunnel-keys-server-setup.md` в репо ilsport):

```
GatewayPorts no
AllowTcpForwarding yes
PermitOpen localhost:22100-22199
```

Ключ сервера для веб-терминала — вручную в `~/.ssh/authorized_keys` на Pi. Затем:

```bash
sudo systemctl start ilsport-tunnel
journalctl -u ilsport-tunnel -f
```

С сервера подключение к Pi:

```bash
ssh -p <TUNNEL_PORT> <pi_user>@localhost
```

(`TUNNEL_PORT` — поле `machines.tunnel_ssh_port` или последняя строка `/etc/ilsport/env`).

### 2. Проверить плеер

Плеер запускается **в X-сессии**, не из systemd. Если ты подключён по SSH, его не видно — нужен autologin или физический вход. Логи:

```bash
tail -f ~/.ilsport-player.log
pgrep -fa "python3 .*main.py"
```

Лог ротируется силами `kiosk-autostart.sh`: при старте `main.py`, если файл
перевалил за `PLAYER_LOG_MAX_MB` (по умолчанию 20), он уезжает в
`~/.ilsport-player.log.1`. Раньше ротации не было — на стенде без карты захвата
лог набирал по 28 МБ в сутки.

### 3. Машина без карты захвата

Агент **не умирает**, если карта захвата не подключена или отвалилась на ходу.
Он остаётся жив, шлёт heartbeat со `state=error` и подхватывает карту, как
только она появится, — перезагрузка после монтажа в автомат не нужна. В
дэшборде такая Pi видна как `error`, а не как выключенная; конкретная причина
пишется в лог плеера строкой `[HB] state=error, …, fault=no_capture_device`.

Плейлист при этом синхронизируется в обычном режиме, так что к моменту монтажа
ролики уже лежат на диске и первый же маркер не уводит в рекламу с пустым
плейлистом.

### 4. Залипание в рекламе

Выход из рекламы — только по marker2, принудительного таймаута нет: так решил
заказчик (21.08.2026), потому что длительность рекламного слота равна
длительности игры и заранее не известна. Но на площадке видели автомат, у
которого реклама крутилась 13 часов подряд (8071 показ девятисекундного
ролика), поэтому с 1.3.0 агент делает следующее:

- **Второй, терпимый к масштабу детектор.** Основной (полный шаблон, порог
  `THRESHOLD`) остался ровно таким, каким его проверяли на площадке. Рядом
  работает второй: центральный фрагмент эталона, вдвое более грубое разрешение
  со сглаживанием и лестница масштабов `DETECT_SCALES` (0.8–1.2 с шагом 0.05).
  Маркер найден, если сработал любой из двух. Причина: полноширинный шаблон не
  переживает рассогласование масштаба больше пары процентов — замер дал 0.117
  для marker2 при зуме 12 %, а карты захвата на разных автоматах отдают кадр
  чуть крупнее или мельче эталона. Отключить: `DETECT_TOLERANT=0`; свой порог:
  `DETECT_TOLERANT_THRESHOLD`. `MARKER_DEBUG=1` печатает оба отклика.
- **Окно подтверждения растёт вместе с реальным fps.** Раньше два попадания
  должны были уложиться в фиксированные `DEBOUNCE_MAX_GAP=0.4` с; при просадке
  ниже ~2,5 fps (mpv занимает CPU) соседние кадры уже были дальше друг от друга,
  и маркер не подтверждался никогда.
- **marker1 во время рекламы = новый цикл автомата.** Ролик перезапускается,
  а пропущенный marker2 засчитывается — это видно в диагностике.
- **Через `STUCK_WARN_SEC` (300 с) в heartbeat уходит `fault=ad_stuck`** с
  диагностикой: сколько секунд без marker2, максимальный отклик marker2 за
  сеанс и сколько раз повторился marker1. Дэшборд показывает это как
  предупреждение, состояние остаётся `playing` — реклама действительно идёт.
  По максимальному отклику сразу видно, в чём дело: `0.6–0.74` — порог или
  кадрирование, `~0.2` — меню на экране не появляется вовсе.
- **`STUCK_EXIT_SEC` — аварийный рубильник, по умолчанию `0` (выключен).**
  Значение больше нуля в `/etc/ilsport/env` включает принудительный возврат в
  трансляцию через столько секунд рекламы. Это ровно тот таймаут, от которого
  заказчик отказался, — включать только на конкретном автомате и осознанно.

### 5. Пустой плейлист

Первая установка до назначения роликов, «рекламодатели закончились», ролик ещё
не докачался — во всех этих случаях `public/ads/` пуст, и агент **не входит в
рекламный режим**: marker1 пишет в лог «плейлист пуст — остаюсь в трансляции»,
нижний дисплей продолжает зеркалить автомат. Если плейлист опустел посреди
рекламы — mpv прячется и агент возвращается в трансляцию. Раньше в обоих
случаях экран замирал на заставке до конца игры, а серверу уходило ложное
`playing`. Оператору об этом сообщает алерт `machine.empty_playlist` в дэшборде.

### 6. Запись экрана для подбора маркеров

Выход из рекламы ловится по эталону шапки меню (`public/marker2.png`). В части
режимов автомата (онлайн-игра и др.) шапка другая — иконки и цвет не те, и
такой режим реклама не отпускает до конца игры. Чтобы собрать шапки всех
режимов, агент с 1.3.1 умеет записывать экран автомата. Снаружи (`ffmpeg`)
карту захвата не открыть — агент держит `/dev/videoN` монопольно, поэтому
запись живёт внутри него и запускается **без перезапуска плеера**.

**Запустить** — в веб-терминале дэшборда (или по SSH через туннель):

```bash
touch ~/.cache/ilsport/record.request          # 15 минут (REC_DEFAULT_SEC)
echo 600 > ~/.cache/ilsport/record.request     # своя длительность, с
echo stop > ~/.cache/ilsport/record.request    # остановить раньше
tail -f ~/.ilsport-player.log | grep REC       # [REC] старт … / [REC] стоп …
```

Файл-запрос одноразовый: агент удаляет его, как только прочитал. Запись сама
останавливается по времени (не больше `REC_MAX_SEC`, 3600) или по потолку
`REC_MAX_MB` (1500) и не стартует, если на диске меньше потолка плюс 500 МБ.
Хранятся последние `REC_KEEP` (3) записей, старые удаляются. Любая ошибка
записи пишет одну строку `[REC] ошибка …` и выключает запись — детект и реклама
продолжают работать. Ещё вариант — `RECORD_ON_START_SEC=600` в
`/etc/ilsport/env`: запись при каждом старте агента, пока переменная стоит.

**Что протыкать на автомате**, задерживаясь на каждом экране не меньше 3 с
(шапка должна попасть в запись так же, как её увидит детектор — в первую
секунду появления):

1. касание из аттракт-лупа → HOME;
2. **каждый пункт GAME SELECT** — все игры (01, Cricket, Count-Up, party и т.д.);
3. PLAYER SELECT → GAME START, несколько бросков, завершение/выход из партии,
   экран результатов;
4. **онлайн-режим**: вход, лобби, ожидание и матч;
5. настройки и прочие экраны, до которых доходят игроки;
6. в конце оставить автомат в покое минуты на две: запишется полный аттракт-луп
   (70 с), а если включится реклама — и реальный цикл marker1.

**Что получится** в `~/.cache/ilsport/rec/<дата-время>/`:

| Файл | Зачем |
|---|---|
| `screen.avi` | MJPG 960×540, 5 к/с — прогон через боевой детект: `DEV_DETECT=…/screen.avi python3 dev.py` (dev.py растягивает кадр обратно; детект работает на четверти кадра, так что для него ничего не теряется) |
| `shots/NNNN_tСЕК_состояние.png` | полный кадр 1920×1080 без потерь на каждой смене экрана и переходе состояния — из них вырезаются эталоны |
| `detect.csv` | на каждый кадр: отклики marker1/marker2 (основной и терпимый), fps, состояние, номер кадра видео, имя снимка |
| `meta.json` | параметры, версия агента, причина остановки, счётчики |

На 15 минут — примерно 150–250 МБ видео и 100–400 МБ снимков.

**Забрать** с сервера через туннель (порт — `machines.tunnel_ssh_port`):

```bash
ssh -p <TUNNEL_PORT> <user>@localhost 'cd ~/.cache/ilsport/rec && tar czf - <дата-время>' > rec.tgz
```

**Сделать новые эталоны** (на рабочей машине, в репозитории):

```bash
python3 tools/rec_report.py ~/rec/<дата-время>            # контактные листы shots_sheet_N.jpg + отчёт по marker2
python3 tools/make_marker.py ~/rec/<дата-время>/shots/0012_t0084.5_video.png --out cand/online.png
mkdir -p ~/rec/<дата-время>/labels/online && cp ~/rec/<дата-время>/shots/0012_*.png ~/rec/<дата-время>/labels/online/
python3 tools/rec_report.py ~/rec/<дата-время> --templates public/marker2.png cand/online.png --replay
```

`rec_report.py` печатает для каждого эталона отклик на «своих» снимках
(положительные — те, что скопированы в `labels/<имя эталона>/`) и максимум
на всех остальных кадрах записи. Эталон годится, если на своих ≥ 0.85, на
чужих ≤ 0.60 (порог 0.75 посередине), а marker1 нигде, кроме заставки, не
поднимается выше 0.5. `--replay` прогоняет запись через `capture_thread_fn`
на виртуальных часах и печатает каждый переход состояния — выходы из рекламы
должны совпадать с появлением шапки и больше ни с чем.

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
tail ~/.ilsport-update.log
```

Лог лежит в домашней директории, а не в `/var/log`: юнит работает под обычным
пользователем без tty, и `sudo` в скрипте раньше молча ломал всё обновление —
файлы обновлялись, а плеер не перезапускался. Дублируется в journald:
`journalctl -u ilsport-update`.

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
