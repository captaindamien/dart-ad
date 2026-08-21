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

AGENT_VERSION      = "1.2.1"

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

# Предупреждение в лог, если реклама идёт дольше этого без marker2.
# Только диагностика: принудительного возврата в LIVE нет, выход из рекламы
# остаётся исключительно по маркеру.
STUCK_WARN_SEC = float(os.environ.get("STUCK_WARN_SEC", "300"))
