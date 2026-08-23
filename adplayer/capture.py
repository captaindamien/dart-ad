import time

import cv2

from .config import (
    DETECT_SCALE, DETECT_EVERY_N, THRESHOLD, DEBOUNCE_FRAMES,
    DEBOUNCE_MAX_GAP, MARKER_COOLDOWN, MARKER_DEBUG, STUCK_WARN_SEC,
)
from .state import STATE_LIVE, STATE_VIDEO


def find_capture_device(skip_first=False):
    start = 1 if skip_first else 0
    for i in range(start, 10):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                print(f"  [device {i}] {w}x{h} — используется")
                return cap, i
            cap.release()
    return None, -1


def load_markers(path1, path2):
    marker1 = cv2.imread(path1, cv2.IMREAD_GRAYSCALE)
    marker2 = cv2.imread(path2, cv2.IMREAD_GRAYSCALE)
    if marker1 is None:
        raise FileNotFoundError(f"Маркер не найден: {path1}")
    if marker2 is None:
        raise FileNotFoundError(f"Маркер не найден: {path2}")
    m1 = cv2.resize(marker1, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
    m2 = cv2.resize(marker2, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
    return m1, m2


def _marker_score(small_gray, marker_small):
    """
    Отклик шаблона в кадре, 0..1. Возвращаем именно величину, а не готовый
    вердикт: без неё подбор THRESHOLD на площадке — гадание (см. MARKER_DEBUG).
    """
    if marker_small is None or small_gray is None:
        return 0.0
    if (small_gray.shape[0] < marker_small.shape[0] or
            small_gray.shape[1] < marker_small.shape[1]):
        return 0.0
    res = cv2.matchTemplate(small_gray, marker_small, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return float(max_val)


def _warn_if_template_fills_frame(gray_small, markers):
    """
    Эталон размером с кадр вырождает matchTemplate в сравнение экрана целиком:
    карта откликов 1x1, никакой устойчивости к сдвигу кадрирования. Так всё ещё
    устроен marker.png — заставка «DARTSLIVE 3» на весь экран. marker2.png уже
    фрагмент (шапка меню), и предупреждение по нему не печатается.
    """
    for name, m in markers:
        if m is None:
            continue
        if (m.shape[0] >= gray_small.shape[0] and m.shape[1] >= gray_small.shape[1]):
            print(f"[WARN] {name}: шаблон {m.shape[1]}x{m.shape[0]} совпадает с кадром "
                  f"{gray_small.shape[1]}x{gray_small.shape[0]} при DETECT_SCALE={DETECT_SCALE} — "
                  f"сравнивается весь экран, сдвиг кадрирования сломает детект")


class _Debouncer:
    """
    Подтверждение маркера: DEBOUNCE_FRAMES попаданий подряд, идущих не реже
    чем раз в DEBOUNCE_MAX_GAP секунд.

    Ограничение по времени здесь принципиально. Чистый счётчик кадров зависит
    от загрузки CPU: в STATE_VIDEO частота обработки падает в разы, и окно
    подтверждения растягивалось на секунды — маркер, живущий около секунды,
    не проходил его никогда.
    """

    def __init__(self):
        self.count    = 0
        self.last_hit = 0.0

    def hit(self, now):
        if self.count and now - self.last_hit > DEBOUNCE_MAX_GAP:
            self.count = 0
        self.count   += 1
        self.last_hit = now
        return self.count >= DEBOUNCE_FRAMES

    def reset(self):
        self.count = 0


class _DebugMeter:
    """Фактический fps обработки и отклики обоих маркеров, раз в period секунд."""

    def __init__(self, period=2.0):
        self.period = period
        self.frames = 0
        self.since  = time.time()

    def tick(self):
        self.frames += 1

    def due(self, now):
        return now - self.since >= self.period

    def flush(self, now, state, score1, score2):
        elapsed = now - self.since
        fps = self.frames / elapsed if elapsed > 0 else 0.0
        print(f"[DETECT] state={state} fps={fps:.1f} "
              f"marker1={score1:.3f} marker2={score2:.3f} (порог {THRESHOLD})")
        self.frames = 0
        self.since  = now


def capture_thread_fn(cap_live, marker1_small, marker2_small, shared, stop_event, sm):
    frame_count    = 0
    deb_live       = _Debouncer()   # ждём marker1, чтобы уйти в рекламу
    deb_video      = _Debouncer()   # ждём marker2, чтобы вернуться к трансляции
    cooldown_until = 0.0
    checked_sizes  = False
    stuck_warned   = False
    dbg            = _DebugMeter() if MARKER_DEBUG else None

    while not stop_event.is_set():
        ret, frame = cap_live.read()
        if not ret:
            time.sleep(0.01)
            continue

        shared["live_frame"] = frame
        frame_count += 1
        if dbg is not None:
            dbg.tick()

        if frame_count % DETECT_EVERY_N != 0:
            continue

        small      = cv2.resize(frame, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if not checked_sizes:
            checked_sizes = True
            _warn_if_template_fills_frame(
                gray_small, (("marker.png", marker1_small), ("marker2.png", marker2_small)))

        now     = time.time()
        in_live = sm.state == STATE_LIVE
        target  = marker1_small if in_live else marker2_small
        deb     = deb_live      if in_live else deb_video

        score = _marker_score(gray_small, target)

        if dbg is not None and dbg.due(now):
            score1 = score if in_live else _marker_score(gray_small, marker1_small)
            score2 = _marker_score(gray_small, marker2_small) if in_live else score
            dbg.flush(now, sm.state, score1, score2)

        # Реклама идёт подозрительно долго — скорее всего marker2 не детектится.
        # Только сообщаем: принудительного возврата в LIVE здесь нет.
        if not in_live and sm.time_in_state() > STUCK_WARN_SEC:
            if not stuck_warned:
                stuck_warned = True
                print(f"[WARN] в рекламе уже {sm.time_in_state():.0f}s без marker2 — "
                      f"проверь детект (MARKER_DEBUG=1)")
        else:
            stuck_warned = False

        if now < cooldown_until:
            deb.reset()
            continue

        if score >= THRESHOLD:
            if deb.hit(now):
                sm.transition(STATE_VIDEO if in_live else STATE_LIVE)
                if in_live:
                    shared["video_restart"] = True
                deb_live.reset()
                deb_video.reset()
                cooldown_until = now + MARKER_COOLDOWN
        else:
            deb.reset()
