"""
Запись экрана автомата для подбора маркеров.

Зачем. Выход из рекламы ловится по эталонам шапки меню (public/exit/*.png),
а шапка меняется от режима к режиму и от модели автомата — режим без
своего эталона реклама не отпускает до конца игры. Чтобы добавить эталон,
нужен кадр с автомата в этом режиме. Снаружи карту захвата не открыть: агент держит /dev/videoN монопольно,
поэтому запись живёт прямо в потоке детекта.

Как запускается. Оператор в веб-терминале дэшборда (или по SSH):

    touch ~/.cache/ilsport/record.request          # REC_DEFAULT_SEC секунд
    echo 600 > ~/.cache/ilsport/record.request     # своя длительность
    echo stop > ~/.cache/ilsport/record.request    # остановить раньше

Файл одноразовый: агент удаляет его сразу после прочтения, перезапуск плеера
не нужен. Ещё вариант — RECORD_ON_START_SEC в /etc/ilsport/env: запись при
каждом старте агента.

Что пишется в REC_DIR/<YYYYmmdd-HHMMSS>/:
  screen.avi   — MJPG в половинном разрешении, REC_FPS кадров/с. Для прогона
                 через DEV_DETECT (dev.py растягивает кадр обратно).
  shots/*.png  — полный кадр без потерь на каждой смене экрана и на каждом
                 переходе состояния. Из них вырезаются новые эталоны.
  detect.csv   — на каждый обработанный кадр: отклики marker1 и лучшего
                 эталона выхода (с именем), fps, состояние, номер кадра видео
                 и имя снимка.
  meta.json    — параметры записи, версия агента, причина остановки, счётчики.

Ошибки записи не должны ронять детект: каждый публичный метод Recorder ловит
всё, пишет одну строку [REC] и выключает запись до следующего запроса.
"""

import csv
import json
import os
import queue
import shutil
import socket
import threading
import time

import cv2

from .config import (
    AGENT_VERSION, THRESHOLD, DETECT_SCALE, DETECT_TOLERANT, DETECT_TOLERANT_THRESHOLD,
    DETECT_SCALES, DEBOUNCE_FRAMES, DEBOUNCE_MAX_GAP,
    REC_DIR, REC_REQUEST_PATH, RECORD_ON_START_SEC, REC_DEFAULT_SEC, REC_MAX_SEC,
    REC_MAX_MB, REC_FPS, REC_VIDEO_SCALE, REC_QUALITY, REC_SNAP_DIFF, REC_SNAP_SETTLE,
    REC_SNAP_MIN_GAP, REC_SNAP_MAX, REC_QUEUE_MAX, REC_KEEP,
)

_MIN_SEC        = 10.0
_PROGRESS_EVERY = 60.0     # строка [REC] N s: … в лог
_CSV_FLUSH_SEC  = 5.0
# Столько свободного места сверх потолка записи должно оставаться на диске:
# заполненная под ноль SD-карта — это уже не диагностика, а новая поломка.
_FREE_MARGIN_MB = 500.0

CSV_FIELDS = (
    "t", "rec_t", "state", "in_state_s", "fps", "gap", "det_ms", "vid_frame", "snap",
    "hit1", "hit2",
    "m1_primary", "m1_tol", "m1_scale",
    "m2_primary", "m2_tol", "m2_scale", "exit_name",
)


def _mad(a, b):
    """Средняя абсолютная разница двух серых кадров одного размера, 0..255."""
    if a is None or b is None or a.shape != b.shape:
        return 255.0
    return float(cv2.absdiff(a, b).mean())


def read_request(path=REC_REQUEST_PATH):
    """
    Прочитать и удалить файл-запрос. Возвращает None (файла нет),
    ("stop",) или ("start", seconds).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(64).strip().lower()
    except FileNotFoundError:
        return None
    try:
        os.unlink(path)
    except OSError:
        pass
    if text in ("stop", "0"):
        return ("stop",)
    if not text:
        return ("start", REC_DEFAULT_SEC)
    try:
        sec = float(text)
    except ValueError:
        print(f"[REC] запрос: не понял {text!r} — беру {REC_DEFAULT_SEC:.0f}s")
        return ("start", REC_DEFAULT_SEC)
    return ("start", min(max(sec, _MIN_SEC), REC_MAX_SEC))


class _Session:
    """Одна запись: каталог, VideoWriter, CSV, поток-писатель, потолки."""

    def __init__(self, now, seconds, why):
        self.t0       = now
        self.seconds  = seconds
        self.why      = why
        self.stop_why = None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        self.dir      = os.path.join(REC_DIR, stamp)
        self.shot_dir = os.path.join(self.dir, "shots")
        os.makedirs(self.shot_dir, exist_ok=False)

        self.video_path  = os.path.join(self.dir, "screen.avi")
        self._writer     = None          # создаётся по первому кадру: нужен его размер
        self._video_size = None
        self.vid_frames  = 0
        self.vid_bytes   = 0
        self._last_vid_t = 0.0

        self.snaps       = 0
        self.snap_bytes  = 0
        self._last_snap_t     = -1e9
        self._last_snap_small = None
        self._prev_small      = None

        self.dropped   = 0
        self.error     = None
        self._lock     = threading.Lock()
        self._q        = queue.Queue(maxsize=REC_QUEUE_MAX)
        self._thread   = threading.Thread(target=self._writer_loop, daemon=True, name="rec-writer")
        self._thread.start()

        self._csv_f    = open(os.path.join(self.dir, "detect.csv"), "w", newline="", encoding="utf-8")
        self._csv      = csv.writer(self._csv_f)
        self._csv.writerow(CSV_FIELDS)
        self.csv_rows  = 0
        self._csv_flushed = now
        self._last_progress = now
        self._last_size_check = now

        self.write_meta()

    # --- вызывается из потока детекта ------------------------------------

    def feed(self, now, frame, gray_small, row):
        """row — dict с полями CSV кроме vid_frame/snap; их заполняем здесь."""
        rec_t = now - self.t0
        vid_idx, snap_name = "", ""

        if now - self._last_vid_t >= 1.0 / REC_FPS - 0.01:
            if self._put(("vid", frame)):
                vid_idx = self.vid_frames
                self.vid_frames += 1
                self._last_vid_t = now

        if self._want_snapshot(now, gray_small, forced=row.pop("_forced", False)):
            snap_name = f"{self.snaps:04d}_t{rec_t:07.1f}_{row['state']}.png"
            if self._put(("png", os.path.join(self.shot_dir, snap_name), frame)):
                self.snaps += 1
                self._last_snap_t = now
                self._last_snap_small = gray_small
            else:
                snap_name = ""
        self._prev_small = gray_small

        row.update(rec_t=f"{rec_t:.3f}", vid_frame=vid_idx, snap=snap_name)
        self._csv.writerow([row.get(k, "") for k in CSV_FIELDS])
        self.csv_rows += 1
        if now - self._csv_flushed >= _CSV_FLUSH_SEC:
            self._csv_f.flush()
            self._csv_flushed = now

        if now - self._last_progress >= _PROGRESS_EVERY:
            self._last_progress = now
            print(f"[REC] {rec_t:.0f}s: видео {self.vid_frames} кадров/{self.vid_bytes / 1e6:.0f}MB, "
                  f"снимков {self.snaps}/{self.snap_bytes / 1e6:.0f}MB, csv {self.csv_rows} строк, "
                  f"потеряно {self.dropped}")

    def _want_snapshot(self, now, gray_small, forced):
        if self.snaps >= REC_SNAP_MAX:
            return False
        if forced or self._last_snap_small is None:
            return True
        if now - self._last_snap_t < REC_SNAP_MIN_GAP:
            return False
        return (_mad(gray_small, self._last_snap_small) > REC_SNAP_DIFF and
                _mad(gray_small, self._prev_small) < REC_SNAP_SETTLE)

    def _put(self, item):
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def check_limits(self, now):
        """Причина остановки или None. Размер видео смотрим раз в секунду."""
        if self.error is not None:
            return "error"
        if now - self.t0 >= self.seconds:
            return "max_sec"
        if now - self._last_size_check >= 1.0:
            self._last_size_check = now
            try:
                self.vid_bytes = os.path.getsize(self.video_path)
            except OSError:
                pass
            if (self.vid_bytes + self.snap_bytes) / 1e6 >= REC_MAX_MB:
                return "max_mb"
        return None

    def close(self, why):
        self.stop_why = why
        # Писатель дочитывает очередь до конца; если он уже умер, очередь
        # никто не разбирает — ждать put() бесконечно нельзя.
        try:
            self._q.put(None, timeout=2.0)
        except queue.Full:
            pass
        self._thread.join(timeout=5.0)
        with self._lock:
            if self._writer is not None:
                self._writer.release()
                self._writer = None
        try:
            self._csv_f.close()
        except Exception:
            pass
        try:
            self.vid_bytes = os.path.getsize(self.video_path)
        except OSError:
            pass
        self.write_meta()

    def write_meta(self):
        meta = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.t0)),
            "why": self.why, "stop_why": self.stop_why, "seconds_requested": self.seconds,
            "duration_s": round(time.time() - self.t0, 1),
            "agent_version": AGENT_VERSION, "host": socket.gethostname(),
            "video": {"path": "screen.avi", "fps": REC_FPS, "scale": REC_VIDEO_SCALE,
                      "quality": REC_QUALITY, "size": self._video_size,
                      "frames": self.vid_frames, "bytes": self.vid_bytes},
            "shots": {"count": self.snaps, "bytes": self.snap_bytes,
                      "diff": REC_SNAP_DIFF, "settle": REC_SNAP_SETTLE},
            "csv_rows": self.csv_rows, "dropped": self.dropped,
            "error": self.error,
            "detect": {"THRESHOLD": THRESHOLD, "DETECT_SCALE": DETECT_SCALE,
                       "DETECT_TOLERANT": DETECT_TOLERANT,
                       "DETECT_TOLERANT_THRESHOLD": DETECT_TOLERANT_THRESHOLD,
                       "DETECT_SCALES": list(DETECT_SCALES),
                       "DEBOUNCE_FRAMES": DEBOUNCE_FRAMES, "DEBOUNCE_MAX_GAP": DEBOUNCE_MAX_GAP},
        }
        tmp = os.path.join(self.dir, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(self.dir, "meta.json"))

    # --- поток-писатель ----------------------------------------------------

    def _open_writer(self, frame):
        h, w = frame.shape[:2]
        size = (max(2, int(round(w * REC_VIDEO_SCALE))), max(2, int(round(h * REC_VIDEO_SCALE))))
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        writer = None
        # Встроенный MJPEG-AVI не зависит от сборки FFmpeg/GStreamer и честно
        # применяет VIDEOWRITER_PROP_QUALITY. Если такого бэкенда нет — обычный.
        mjpeg_api = getattr(cv2, "CAP_OPENCV_MJPEG", None)
        candidates = [(self.video_path, fourcc, REC_FPS, size)]
        if mjpeg_api is not None:
            candidates.insert(0, (self.video_path, mjpeg_api, fourcc, REC_FPS, size))
        for args in candidates:
            try:
                w_ = cv2.VideoWriter(*args)
            except (cv2.error, TypeError):
                continue
            if w_.isOpened():
                writer = w_
                break
            w_.release()
        if writer is None:
            raise RuntimeError(f"cv2.VideoWriter не открылся: {self.video_path}")
        try:
            writer.set(cv2.VIDEOWRITER_PROP_QUALITY, REC_QUALITY)
        except (cv2.error, AttributeError):
            pass
        self._video_size = list(size)
        return writer, size

    def _writer_loop(self):
        try:
            while True:
                item = self._q.get()
                if item is None:
                    return
                kind = item[0]
                if kind == "vid":
                    with self._lock:
                        if self._writer is None:
                            self._writer, vsize = self._open_writer(item[1])
                        small = cv2.resize(item[1], tuple(self._video_size),
                                           interpolation=cv2.INTER_AREA)
                        self._writer.write(small)
                elif kind == "png":
                    path, frame = item[1], item[2]
                    if not cv2.imwrite(path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                        raise RuntimeError(f"imwrite не удался: {path}")
                    try:
                        self.snap_bytes += os.path.getsize(path)
                    except OSError:
                        pass
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"


class Recorder:
    """
    Фасад для потока детекта. Все методы безопасны: исключение внутри
    превращается в строку [REC] и остановку записи, наружу не выходит.
    """

    def __init__(self):
        self._session    = None
        self._next_poll  = 0.0
        self._env_armed  = RECORD_ON_START_SEC > 0
        if self._env_armed:
            print(f"[REC] RECORD_ON_START_SEC={RECORD_ON_START_SEC:.0f}: запись начнётся с первого "
                  f"кадра (и будет начинаться при каждом старте, пока переменная стоит в env)")

    @property
    def active(self):
        return self._session is not None

    def poll(self, now):
        try:
            self._poll(now)
        except Exception as e:
            self._fail(e)

    def _poll(self, now):
        if self._env_armed:
            self._env_armed = False
            self._start(now, min(RECORD_ON_START_SEC, REC_MAX_SEC), "env")
        if now < self._next_poll:
            return
        self._next_poll = now + 1.0

        req = read_request()
        if req is not None:
            if req[0] == "stop":
                if self._session is not None:
                    self.stop("request")
                else:
                    print("[REC] запрос stop, но запись не идёт")
            elif self._session is not None:
                print(f"[REC] запрос start проигнорирован: запись уже идёт "
                      f"({now - self._session.t0:.0f}s из {self._session.seconds:.0f})")
            else:
                self._start(now, req[1], "request")

        if self._session is not None:
            why = self._session.check_limits(now)
            if why:
                self.stop(why)

    def _start(self, now, seconds, why):
        os.makedirs(REC_DIR, exist_ok=True)
        self._prune_old()
        free_mb = shutil.disk_usage(REC_DIR).free / 1e6
        need_mb = REC_MAX_MB + _FREE_MARGIN_MB
        if free_mb < need_mb:
            print(f"[REC] отказ: свободно {free_mb:.0f}MB, нужно {need_mb:.0f}MB "
                  f"(REC_MAX_MB={REC_MAX_MB:.0f} + запас {_FREE_MARGIN_MB:.0f})")
            return
        self._session = _Session(now, seconds, why)
        print(f"[REC] старт: dir={self._session.dir} why={why} sec={seconds:.0f} "
              f"fps={REC_FPS:g} scale={REC_VIDEO_SCALE:g} q={REC_QUALITY} потолок={REC_MAX_MB:.0f}MB")

    def _prune_old(self):
        try:
            dirs = sorted(d for d in os.listdir(REC_DIR)
                          if os.path.isdir(os.path.join(REC_DIR, d)))
        except OSError:
            return
        for d in dirs[:max(0, len(dirs) - (REC_KEEP - 1))]:
            shutil.rmtree(os.path.join(REC_DIR, d), ignore_errors=True)
            print(f"[REC] удалил старую запись {d} (REC_KEEP={REC_KEEP})")

    def feed(self, now, frame, gray_small, state, in_state_s, interval, gap, det_ms,
             r1, r2, hit1, hit2, forced_snapshot=False):
        """r1/r2 — кортежи (hit, primary, tolerant, scale) из _Detector.score."""
        if self._session is None or frame is None:
            return
        try:
            row = {
                "t": f"{now:.3f}", "state": state, "in_state_s": f"{in_state_s:.1f}",
                "fps": f"{(1.0 / interval) if interval > 0 else 0:.1f}", "gap": f"{gap:.2f}",
                "det_ms": f"{det_ms:.1f}",
                "hit1": int(bool(hit1)), "hit2": int(bool(hit2)),
                "m1_primary": f"{r1[1]:.3f}", "m1_tol": f"{r1[2]:.3f}", "m1_scale": f"{r1[3]:.2f}",
                "m2_primary": f"{r2[1]:.3f}", "m2_tol": f"{r2[2]:.3f}", "m2_scale": f"{r2[3]:.2f}",
                "exit_name": r2[4] if len(r2) > 4 else "",
                "_forced": forced_snapshot,
            }
            self._session.feed(now, frame, gray_small, row)
        except Exception as e:
            self._fail(e)

    def stop(self, why):
        s, self._session = self._session, None
        if s is None:
            return
        try:
            s.close(why)
            print(f"[REC] стоп: {why}" + (f" ({s.error})" if s.error else "") +
                  f" после {time.time() - s.t0:.0f}s: видео {s.vid_frames} кадров/"
                  f"{s.vid_bytes / 1e6:.0f}MB, снимков {s.snaps}/{s.snap_bytes / 1e6:.0f}MB, "
                  f"csv {s.csv_rows} строк, потеряно {s.dropped}, dir={s.dir}")
        except Exception as e:
            print(f"[REC] ошибка при остановке: {type(e).__name__}: {e}")

    def _fail(self, e):
        print(f"[REC] ошибка: {type(e).__name__}: {e} — запись выключена до следующего запроса")
        s, self._session = self._session, None
        if s is not None:
            try:
                s.error = s.error or f"{type(e).__name__}: {e}"
                s.close("error")
            except Exception:
                pass
