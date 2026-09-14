import os

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _load_dotenv(path):
    """
    Подхватывает .env из корня проекта — нужно только для локального запуска.
    В проде переменные приходят из systemd (EnvironmentFile=/etc/ilsport/env),
    поэтому уже заданное окружение имеет приоритет и здесь не перетирается.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            continue

        key   = key.strip()
        value = value.strip()
        # Кавычки — часть синтаксиса файла, а не значения: без этого токен
        # уехал бы на сервер вместе с ними и получил 401.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(os.path.join(_ROOT, ".env"))

AGENT_VERSION      = "1.3.1"

MACHINE_TOKEN      = os.environ.get("MACHINE_TOKEN", "")
SERVER_URL         = os.environ.get("SERVER_URL", "http://localhost:3000").rstrip("/")
ADS_DIR            = os.environ.get("ADS_DIR", os.path.join(_ROOT, "public", "ads"))
SYNC_INTERVAL      = int(os.environ.get("SYNC_INTERVAL", "300"))
# 15 с — это верхняя граница задержки, с которой дашборд видит температуру,
# CPU и диск. Смена состояния и смена ролика шлют хартбит вне графика, поэтому
# дальнейшее учащение почти ничего не даёт. Внимание: на автомате значение
# может быть закреплено в /etc/ilsport/env — окружение имеет приоритет.
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "15"))

# --- статистика показов ---
# Очередь на диске, а не в памяти: при обрыве связи или перезагрузке Pi
# статистика показов иначе теряется безвозвратно.
PLAYBACK_QUEUE_PATH = os.environ.get(
    "PLAYBACK_QUEUE_PATH",
    os.path.join(os.path.expanduser("~"), ".cache", "ilsport", "playback_queue.jsonl"),
)
PLAYBACK_FLUSH_INTERVAL = int(os.environ.get("PLAYBACK_FLUSH_INTERVAL", "60"))
# Сервер принимает не больше 200 событий за раз.
PLAYBACK_BATCH_SIZE = int(os.environ.get("PLAYBACK_BATCH_SIZE", "100"))
# Потолок очереди: если сервер недоступен неделями, файл не должен съесть диск.
PLAYBACK_QUEUE_MAX = int(os.environ.get("PLAYBACK_QUEUE_MAX", "20000"))
# Показ короче этого не считаем: это перелистывание, а не просмотр.
PLAYBACK_MIN_SEC = float(os.environ.get("PLAYBACK_MIN_SEC", "1.0"))

_PUBLIC = os.path.join(_ROOT, "public")
MARKER1_PATH = os.path.join(_PUBLIC, "marker.png")
MARKER2_PATH = os.path.join(_PUBLIC, "marker2.png")

# --- карта захвата ---
# Разрешение запрашивается сразу при открытии устройства, в том числе при
# переподключении на ходу, — иначе после отвала карты агент продолжал бы
# работать на дефолтных 640x480, и шаблоны маркеров перестали бы совпадать.
CAPTURE_WIDTH  = int(os.environ.get("CAPTURE_WIDTH", "1920"))
CAPTURE_HEIGHT = int(os.environ.get("CAPTURE_HEIGHT", "1080"))
# Пауза между попытками найти/переоткрыть карту захвата. Раньше её отсутствие
# означало sys.exit(1) и перезапуск процесса раз в 5 секунд силами
# kiosk-autostart — heartbeat не успевал уйти ни разу.
CAPTURE_RETRY_SEC = float(os.environ.get("CAPTURE_RETRY_SEC", "10"))
# Сколько секунд подряд cap.read() должен возвращать пустоту, чтобы считать
# карту отвалившейся. Одиночные пропуски кадров — норма для USB-захвата,
# поэтому порог заметно больше периода кадра.
CAPTURE_STALL_SEC = float(os.environ.get("CAPTURE_STALL_SEC", "15"))

# --- детект маркеров ---
THRESHOLD    = 0.75
DETECT_SCALE = 0.25

# Проверяем каждый обработанный кадр. Раньше здесь стояло 3, а в STATE_VIDEO
# шаг ещё и умножался на 4 — при том, что счётчик кадров считает обработанные
# кадры, а не кадры камеры. Пока mpv занимает CPU декодированием рекламы,
# частота обработки падает в разы, и окно подтверждения растягивалось на
# секунды: маркер, живущий около секунды, до него не доживал ни разу.
# Сам детект (resize + matchTemplate) стоит копейки на фоне MJPEG-декода
# в cap.read(), который выполняется на каждом кадре в любом случае.
DETECT_EVERY_N = 1

# Сколько попаданий подряд подтверждают маркер.
DEBOUNCE_FRAMES = 2
# ...и не реже, чем раз в столько секунд. Ограничение по времени обязательно:
# счётчик, считающий только кадры, зависит от загрузки CPU и склеил бы два
# попадания, разделённые секундами простоя, в одно событие.
DEBOUNCE_MAX_GAP = float(os.environ.get("DEBOUNCE_MAX_GAP", "0.4"))

# После смены состояния столько секунд не проверяем противоположный маркер:
# предыдущий может ещё догорать на экране.
MARKER_COOLDOWN = float(os.environ.get("MARKER_COOLDOWN", "0.5"))

# MARKER_DEBUG=1 — печатать фактический fps обработки и отклики обоих маркеров.
# Без этих цифр отладка детекта на площадке превращается в гадание.
MARKER_DEBUG = os.environ.get("MARKER_DEBUG", "") not in ("", "0", "false", "False")

# --- терпимый детектор -----------------------------------------------------
# Основной детект (полный шаблон на DETECT_SCALE, порог THRESHOLD) проверен
# на площадке и не трогается. Но полноширинный шаблон не переживает
# рассогласование масштаба больше пары процентов: замер на синтетике дал
# 0.117 для marker2 при зуме 12 %. Карты захвата на разных автоматах отдают
# кадр чуть крупнее или мельче — и на таком автомате marker2 не ловится
# никогда, реклама не заканчивается.
# Поэтому параллельно работает второй детектор: центральный фрагмент шаблона
# (не зависит от обрезки краёв), вдвое более грубое разрешение со
# сглаживанием (терпит дрейф в пару пикселей) и лестница масштабов.
# Срабатывание любого из двух = маркер найден.
DETECT_TOLERANT = os.environ.get("DETECT_TOLERANT", "1") not in ("", "0", "false", "False")
DETECT_SCALES = tuple(
    float(x) for x in os.environ.get(
        "DETECT_SCALES", "1.0,0.95,1.05,0.9,1.1,0.85,1.15,0.8,1.2").split(",") if x.strip()
)
# Порог терпимого детектора. По умолчанию равен основному; на автомате, где
# MARKER_DEBUG показывает стабильные 0.65–0.74 на маркере, его можно опустить
# через /etc/ilsport/env, не трогая основной.
DETECT_TOLERANT_THRESHOLD = float(os.environ.get("DETECT_TOLERANT_THRESHOLD", str(THRESHOLD)))

# Предупреждение в лог и fault=ad_stuck в heartbeat, если реклама идёт дольше
# этого без marker2. Само по себе — диагностика: состояние остаётся "playing".
STUCK_WARN_SEC = float(os.environ.get("STUCK_WARN_SEC", "300"))
# Принудительный возврат в LIVE через столько секунд рекламы. 0 — выключено,
# и это значение по умолчанию: заказчик 21.08.2026 отказался от выхода по
# таймауту, реклама заканчивается только по marker2. Переменная оставлена
# как аварийный рубильник для конкретного автомата (/etc/ilsport/env),
# включать — только осознанным решением.
STUCK_EXIT_SEC = float(os.environ.get("STUCK_EXIT_SEC", "0"))

# --- запись экрана для подбора маркеров -------------------------------------
# Агент держит /dev/videoN монопольно, снаружи (ffmpeg) карту захвата не
# открыть, поэтому запись живёт внутри потока детекта. Запускается без
# перезапуска плеера: `touch ~/.cache/ilsport/record.request` (в файле можно
# указать длительность в секундах; `stop` — остановить). Файл одноразовый:
# агент удаляет его сразу после прочтения. Подробности — raspberry-pi-setup/README.md.
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "ilsport")
REC_DIR          = os.environ.get("REC_DIR", os.path.join(_CACHE_DIR, "rec"))
REC_REQUEST_PATH = os.environ.get("REC_REQUEST_PATH", os.path.join(_CACHE_DIR, "record.request"))
# Начать запись сразу при старте агента (секунды, 0 — выключено). Срабатывает
# при каждом перезапуске, пока стоит в /etc/ilsport/env, — для разовых замеров
# удобнее файл-запрос.
RECORD_ON_START_SEC = float(os.environ.get("RECORD_ON_START_SEC", "0"))
REC_DEFAULT_SEC = float(os.environ.get("REC_DEFAULT_SEC", "900"))
REC_MAX_SEC     = float(os.environ.get("REC_MAX_SEC", "3600"))
# Потолок одной записи на диске (видео + снимки): SD-карта не резиновая.
REC_MAX_MB      = float(os.environ.get("REC_MAX_MB", "1500"))
# Видео пишется в половинном разрешении: детект работает на DETECT_SCALE=0.25
# от кадра (480x270), и кадр 960x540, растянутый обратно, даёт те же 480x270.
# Для прогона через DEV_DETECT ничего не теряется, а размер падает вчетверо.
# Полное разрешение нужно только эталонам — для них PNG-снимки.
REC_FPS         = float(os.environ.get("REC_FPS", "5"))
REC_VIDEO_SCALE = float(os.environ.get("REC_VIDEO_SCALE", "0.5"))
REC_QUALITY     = int(os.environ.get("REC_QUALITY", "75"))
# Снимок полного кадра (PNG, без потерь) — когда экран сменился (средняя
# разница с последним снимком больше REC_SNAP_DIFF по шкале 0..255) и уже
# устоялся (разница с предыдущим кадром меньше REC_SNAP_SETTLE): иначе в
# эталон попадёт середина анимации перехода.
REC_SNAP_DIFF    = float(os.environ.get("REC_SNAP_DIFF", "8"))
REC_SNAP_SETTLE  = float(os.environ.get("REC_SNAP_SETTLE", "3"))
REC_SNAP_MIN_GAP = float(os.environ.get("REC_SNAP_MIN_GAP", "0.5"))
REC_SNAP_MAX     = int(os.environ.get("REC_SNAP_MAX", "600"))
# Кодирование PNG 1080p на Pi занимает 50–100 мс — пишет отдельный поток через
# ограниченную очередь; при переполнении кадр отбрасывается, детект не ждёт.
REC_QUEUE_MAX = int(os.environ.get("REC_QUEUE_MAX", "8"))
# Сколько последних записей хранить; старые удаляются при старте новой.
REC_KEEP      = int(os.environ.get("REC_KEEP", "3"))
