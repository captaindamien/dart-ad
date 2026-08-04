"""
Учёт показов рекламных роликов.

Границы показа определяются по опросу mpv в player.py, а не подпиской на
события mpv: IPC-сокет там работает в режиме «запрос-ответ» под общим локом,
и параллельный читатель событий ломал бы сопоставление ответов по request_id.
Опрос идёт раз в 0.5 с — для 20-секундного ролика это погрешность около 2.5%,
что для статистики показов приемлемо.

События копятся в файле на диске и досылаются пачками: при обрыве связи
или перезагрузке Pi статистика иначе теряется безвозвратно.
"""

import json
import os
import threading
import time
import uuid

from .config import (
    PLAYBACK_QUEUE_PATH,
    PLAYBACK_FLUSH_INTERVAL,
    PLAYBACK_BATCH_SIZE,
    PLAYBACK_QUEUE_MAX,
    PLAYBACK_MIN_SEC,
)

_queue_lock = threading.Lock()

# Откат позиции больше этого порога означает новый показ, а не перемотку
# назад внутри текущего: обычный шаг опроса даёт прирост ~0.5 с.
_RESTART_BACKSTEP_SEC = 1.0

# Сколько ждём, пока mpv применит seek 0 после команды рестарта. Команда
# асинхронная, и всё это время playback-time возвращает старую позицию.
_RESTART_WAIT_SEC = 3.0
# Позиция ниже этой считается доказательством, что seek уже применился.
_RESTART_SETTLED_SEC = 1.0

# Насколько близко к концу ролика показ засчитывается как полный.
_COMPLETE_TOLERANCE_SEC = 1.0


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


def _ensure_dir():
    os.makedirs(os.path.dirname(PLAYBACK_QUEUE_PATH), exist_ok=True)


def enqueue(event):
    """Дописывает событие в очередь на диске."""
    try:
        _ensure_dir()
        with _queue_lock:
            with open(PLAYBACK_QUEUE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[PLAYBACK] не удалось записать событие: {e}")


def _read_all():
    try:
        with open(PLAYBACK_QUEUE_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # битая строка после внезапного выключения — пропускаем
    return events


def _rewrite(events):
    _ensure_dir()
    tmp = PLAYBACK_QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, PLAYBACK_QUEUE_PATH)


class PlaybackTracker:
    """
    Держит текущий показ и закрывает его при смене ролика,
    перезапуске того же ролика или уходе из режима рекламы.

    Длительность считается по позиции внутри ролика, а не по настенным часам:
    так пауза плеера или подтормаживание цикла не раздувают показ. Но именно
    поэтому позиция старта запоминается отдельно — показ, открытый на середине
    ролика, иначе засчитал бы себе и всё, что проигралось до него.
    """

    def __init__(self, resolve_video_id=None, resolve_duration=None):
        self._resolve_video_id = resolve_video_id or (lambda _: None)
        self._resolve_duration = resolve_duration or (lambda: None)
        self._current = None  # {uid, filename, started_at, start_pos, last_time, duration}
        self._restart_deadline = None  # ждём, пока плеер применит seek 0

    def is_open(self):
        return self._current is not None

    def expect_restart(self):
        """
        Плеер отправил команду «начать ролик заново».

        Команда асинхронная, и до её применения плеер отдаёт позицию, на
        которой стоял на паузе. Открыть показ прямо сейчас — значит через
        полсекунды увидеть откат позиции и оформить эту старую позицию
        отдельным «досмотренным» показом, которого не было.
        """
        self._current = None
        self._restart_deadline = time.time() + _RESTART_WAIT_SEC

    def _safe_duration(self):
        try:
            value = self._resolve_duration()
        except Exception:
            return None
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    def _open(self, filename, position, start_pos=None):
        pos = position or 0.0
        start = pos if start_pos is None else start_pos
        self._current = {
            "uid": str(uuid.uuid4()),
            "filename": filename,
            "start_pos": start,
            # Настенное время, когда показ начался: сейчас минус то, что уже
            # проигралось от start_pos.
            "started_at": time.time() - (pos - start),
            "last_time": pos,
            "duration": self._safe_duration(),
        }

    def _completed(self, cur, natural):
        """
        Досмотрен ли ролик. Повод закрытия — плохой признак сам по себе:
        имя файла пропадает и при таймауте IPC, а прерывание на последней
        секунде фактически является полным показом. Поэтому решает позиция
        относительно длины ролика, а повод остаётся запасным вариантом.
        """
        length = cur["duration"]
        if length:
            return cur["last_time"] >= length - _COMPLETE_TOLERANCE_SEC
        return bool(natural)

    def _close(self, natural, position=None):
        cur = self._current
        self._current = None
        if not cur:
            return

        if position is not None and position > cur["last_time"]:
            cur["last_time"] = position

        duration = cur["last_time"] - cur["start_pos"]
        if duration < PLAYBACK_MIN_SEC:
            return  # мелькнуло при переключении — не показ

        ended_at = cur["started_at"] + duration
        enqueue({
            "event_uid": cur["uid"],
            "video_id": self._resolve_video_id(cur["filename"]),
            "filename": cur["filename"],
            "started_at": _iso(cur["started_at"]),
            "ended_at": _iso(ended_at),
            "duration_sec": round(duration, 2),
            "completed": self._completed(cur, natural),
        })

    def update(self, filename, position):
        """Вызывается из цикла плеера. filename=None — реклама не идёт."""
        if not filename:
            self._restart_deadline = None
            self._close(natural=True)
            return

        if self._restart_deadline is not None:
            if position is None or position < _RESTART_SETTLED_SEC:
                self._restart_deadline = None
                self._open(filename, position, start_pos=0.0)
            elif time.time() >= self._restart_deadline:
                # Seek не применился — учитываем показ от фактической позиции,
                # это честнее, чем не учитывать его вовсе.
                self._restart_deadline = None
                self._open(filename, position)
            return

        cur = self._current
        if cur is None:
            self._open(filename, position)
            return

        if filename != cur["filename"]:
            # Ролик сменился сам — предыдущий доиграл.
            self._close(natural=True)
            self._open(filename, position)
            return

        # Длину плеер сообщает не сразу после загрузки файла. Переспрашиваем и
        # тогда, когда позиция ушла за неё: значит, при открытии показа плеер
        # ещё отдавал длину предыдущего ролика.
        if cur["duration"] is None or cur["last_time"] > cur["duration"] + _COMPLETE_TOLERANCE_SEC:
            cur["duration"] = self._safe_duration()

        if position is not None:
            if position + _RESTART_BACKSTEP_SEC < cur["last_time"]:
                # Тот же файл начался заново — зацикленный плейлист.
                self._close(natural=True)
                self._open(filename, position, start_pos=0.0)
                return
            cur["last_time"] = position

    def interrupt(self, position=None):
        """Показ прерван переключением в режим трансляции."""
        self._restart_deadline = None
        self._close(natural=False, position=position)


def _post_batch(events):
    """Отправляет пачку. Возвращает True, только если сервер её принял."""
    import urllib.error
    import urllib.request
    from .config import MACHINE_TOKEN, SERVER_URL

    body = json.dumps({"events": events}).encode()
    req = urllib.request.Request(
        f"{SERVER_URL}/api/display/playback-events",
        data=body,
        headers={"X-Machine-Token": MACHINE_TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        d = data.get("data", {})
        print(f"[PLAYBACK] отправлено {len(events)}: принято {d.get('accepted')}, "
              f"дубликатов {d.get('deduped')}, не сопоставлено {d.get('unresolved')}")
        return True
    except urllib.error.HTTPError as e:
        # 4xx (кроме 429) — сервер не примет эти события и после повтора:
        # держать их в очереди вечно бессмысленно.
        if 400 <= e.code < 500 and e.code != 429:
            print(f"[PLAYBACK] пачка отклонена сервером ({e.code}), отбрасываем")
            return True
        print(f"[PLAYBACK] ошибка отправки {e.code}, повторим позже")
        return False
    except Exception as e:
        print(f"[PLAYBACK] сеть недоступна ({type(e).__name__}), повторим позже")
        return False


def flush_once():
    """Досылает накопленное. Отправленное удаляется из очереди."""
    with _queue_lock:
        events = _read_all()

    if not events:
        return

    # Хвост важнее головы: свежие показы полезнее месячной давности.
    if len(events) > PLAYBACK_QUEUE_MAX:
        dropped = len(events) - PLAYBACK_QUEUE_MAX
        events = events[-PLAYBACK_QUEUE_MAX:]
        print(f"[PLAYBACK] очередь переполнена, отброшено {dropped} старых событий")

    remaining = list(events)
    while remaining:
        batch = remaining[:PLAYBACK_BATCH_SIZE]
        if not _post_batch(batch):
            break
        remaining = remaining[PLAYBACK_BATCH_SIZE:]

    with _queue_lock:
        # За время отправки могли добавиться новые события — дописываем их.
        current = _read_all()
        fresh = current[len(events):]
        _rewrite(remaining + fresh)


def sender_loop(stop_event):
    while not stop_event.is_set():
        stop_event.wait(timeout=PLAYBACK_FLUSH_INTERVAL)
        if stop_event.is_set():
            break
        try:
            flush_once()
        except Exception as e:
            print(f"[PLAYBACK] sender error: {type(e).__name__}: {e}")
