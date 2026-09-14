import fcntl
import glob
import os
import re
import struct
import time

import cv2

from .api import heartbeat_event, get_playlist
from .config import (
    DETECT_SCALE, DETECT_EVERY_N, THRESHOLD, DEBOUNCE_FRAMES,
    DEBOUNCE_MAX_GAP, MARKER_COOLDOWN, MARKER_DEBUG, STUCK_WARN_SEC, STUCK_EXIT_SEC,
    DETECT_TOLERANT, DETECT_SCALES, DETECT_TOLERANT_THRESHOLD,
    DETECT_TOLERANT_EXIT, EXIT_SEARCH_BAND, EXIT_MARKERS_DIR, MARKER2_PATH,
    CAPTURE_WIDTH, CAPTURE_HEIGHT, CAPTURE_RETRY_SEC, CAPTURE_STALL_SEC,
)
from .state import STATE_LIVE, STATE_VIDEO, FAULT_CAPTURE_LOST, FAULT_AD_STUCK
from .recorder import Recorder

# OpenCV сначала пробует GStreamer, и каждая неудачная попытка открыть
# устройство печатает в stderr по четыре строки варнингов. Пока агент ждёт
# карту захвата, перебор идёт в цикле — на стенде это давало ~28 МБ лога
# в сутки. Явный CAP_V4L2 в open_capture() убирает GStreamer из цепочки,
# уровень логирования глушит остатки. Основной рубильник — переменная
# OPENCV_LOG_LEVEL, выставленная в main.py/dev.py до первого import cv2:
# cv2.utils.logging на OpenCV 4.6 из Bookworm варнинги videoio не гасит.
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:  # старая сборка OpenCV без cv2.utils.logging
    pass

_VIDEO_DEV_RE = re.compile(r"/dev/video(\d+)$")

# VIDIOC_QUERYCAP = _IOR('V', 0, struct v4l2_capability), sizeof == 104.
_VIDIOC_QUERYCAP               = 0x80685600
_V4L2_CAP_VIDEO_CAPTURE        = 0x00000001
_V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
_V4L2_CAP_VIDEO_M2M_MPLANE     = 0x00004000
_V4L2_CAP_VIDEO_M2M            = 0x00008000
_V4L2_CAP_DEVICE_CAPS          = 0x80000000


def _v4l2_caps(path):
    """device_caps узла, либо None, если спросить не удалось."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, buf)
    except OSError:
        return None
    finally:
        os.close(fd)
    caps, dev_caps = struct.unpack_from("<II", buf, 84)
    return dev_caps if caps & _V4L2_CAP_DEVICE_CAPS else caps


def _looks_like_capture(path):
    """
    Стоит ли вообще открывать узел через OpenCV. На Pi 5 семнадцать встроенных
    узлов (pispbe, rpi-hevc-dec) — M2M-кодеки: картинку не отдают, а cap.read()
    на каждом висит до select() timeout, и полный перебор без карты захвата
    занимал 40–60 секунд. Спрашиваем ядро напрямую и пропускаем всё, что не
    умеет захват. Не смогли спросить — пробуем открыть, как раньше.
    """
    caps = _v4l2_caps(path)
    if caps is None:
        return True
    if caps & (_V4L2_CAP_VIDEO_M2M | _V4L2_CAP_VIDEO_M2M_MPLANE):
        return False
    # Только одноплоскостной захват. Узлы pispbe-output* на Pi 5 отдают
    # 0x4201000 = CAPTURE_MPLANE + STREAMING без бита M2M и проходили бы
    # фильтр, а OpenCV на каждом висел до select() timeout. При этом V4L2-
    # бэкенд OpenCV многоплоскостные узлы не поддерживает вовсе, так что
    # требование VIDEO_CAPTURE — не ограничение, а точное условие пригодности.
    # USB-карты захвата (UVC) отдают именно его.
    return bool(caps & _V4L2_CAP_VIDEO_CAPTURE)


def _video_indices():
    """
    Реальные /dev/videoN вместо слепого перебора 0..9.

    На Pi 5 встроенные устройства (pispbe, rpi-hevc-dec) занимают номера
    19–35, и USB-карта, попавшая выше девятого, старым кодом не находилась
    вообще. USB-устройства идут первыми, узлы без захвата отсеиваются по
    QUERYCAP ещё до открытия.
    """
    usb, other = [], []
    for path in glob.glob("/dev/video*"):
        m = _VIDEO_DEV_RE.match(path)
        if not m or not _looks_like_capture(path):
            continue
        idx = int(m.group(1))
        link = os.path.realpath(f"/sys/class/video4linux/video{idx}")
        (usb if "usb" in link else other).append(idx)
    return sorted(usb) + sorted(other)


def open_capture(index):
    """
    Открыть устройство и сразу настроить его. Настройка живёт здесь, а не в
    main.py, потому что переподключение на ходу обязано выставить те же
    разрешение и глубину буфера: на дефолтных 640x480 шаблоны маркеров,
    снятые с 1920x1080, не совпадут никогда.
    """
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None
    ret, frame = cap.read()
    if not ret or frame is None:
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAPTURE_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def find_capture_device(skip_first=False):
    indices = _video_indices()
    if skip_first:
        indices = indices[1:]
    for i in indices:
        cap = open_capture(i)
        if cap is not None:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"  [device {i}] {w}x{h} — используется")
            return cap, i
    return None, -1


def reopen_capture(old_cap):
    """Освободить умершее устройство и попробовать найти его заново."""
    try:
        old_cap.release()
    except Exception:
        pass
    cap, _ = find_capture_device(skip_first=False)
    return cap


def load_markers(path1, exit_dir=EXIT_MARKERS_DIR, fallback_exit=MARKER2_PATH):
    """
    -> (marker1_small, [(name, small), ...]) — эталон входа и набор эталонов
    выхода из public/exit/*.png (имя = имя файла без расширения). Если каталога
    нет — старый одиночный marker2.png, чтобы наполовину обновлённый checkout
    не остался без выхода из рекламы вовсе.
    """
    marker1 = cv2.imread(path1, cv2.IMREAD_GRAYSCALE)
    if marker1 is None:
        raise FileNotFoundError(f"Маркер не найден: {path1}")
    paths = sorted(glob.glob(os.path.join(exit_dir, "*.png")))
    if not paths and os.path.exists(fallback_exit):
        paths = [fallback_exit]
    exits = []
    for p in paths:
        g = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if g is None:
            print(f"[WARN] эталон выхода не читается, пропущен: {p}")
            continue
        exits.append((os.path.splitext(os.path.basename(p))[0],
                      cv2.resize(g, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)))
    if not exits:
        raise FileNotFoundError(f"Эталоны выхода не найдены: {exit_dir}/*.png")
    m1 = cv2.resize(marker1, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
    print(f"[MARKERS] вход={os.path.basename(path1)}; выход ({len(exits)}): "
          + ", ".join(f"{n} {m.shape[1]}x{m.shape[0]}" for n, m in exits))
    return m1, exits


def exit_band(gray_small):
    """Верхняя доля кадра, в которой ищутся эталоны выхода (шапка всегда сверху)."""
    if EXIT_SEARCH_BAND >= 1.0:
        return gray_small
    return gray_small[:max(1, int(gray_small.shape[0] * EXIT_SEARCH_BAND))]


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


# Параметры терпимого детектора (см. DETECT_TOLERANT в config.py).
_TOL_DS   = 0.5    # относительно DETECT_SCALE: 0.25 → 0.125 от кадра
_TOL_BLUR = 1.0    # σ гауссова сглаживания, px
_TOL_CORE = {1: (0.6, 0.7), 2: (0.45, 1.0)}  # доля ширины/высоты; полоса шапки — высота целиком.
# Для полосы фрагмент уже: подбор на синтетике дал min 0.66 при зуме наружу
# и 0.87 внутрь против 0.54/0.83 у 60 % — узкая полоса с текстом теряет
# детали при уменьшении, и чем короче фрагмент, тем меньше накопленный дрейф.


def _core_for(small):
    """Полоса (шапка) или полноэкранный эталон — по пропорциям."""
    h, w = small.shape
    return _TOL_CORE[2] if h / w < 0.25 else _TOL_CORE[1]


def _prep_tolerant(gray):
    g = cv2.resize(gray, (0, 0), fx=_TOL_DS, fy=_TOL_DS, interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(g, (0, 0), _TOL_BLUR)


def _core(t, fx, fy):
    h, w = t.shape
    cw, ch = max(8, int(w * fx)), max(8, int(h * fy))
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return t[y0:y0 + ch, x0:x0 + cw]


class _TolerantMatcher:
    """
    Центральный фрагмент эталона на лестнице масштабов. Шаблоны под каждый
    масштаб считаются один раз при старте — в кадре ничего не ресайзится,
    кроме единственного _prep_tolerant. Масштаб последнего попадания
    пробуется первым: у конкретного автомата рассогласование постоянное, и на
    кадре с маркером обычно хватает одного matchTemplate.
    """

    def __init__(self, marker_small, core_fx, core_fy):
        core = _core(_prep_tolerant(marker_small), core_fx, core_fy)
        self.templates = []
        for s in DETECT_SCALES:
            if abs(s - 1.0) < 1e-6:
                t = core
            else:
                t = cv2.resize(core, (0, 0), fx=s, fy=s,
                               interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
            self.templates.append((s, t))
        self.last_scale = 1.0

    def score(self, gray_tol, quick=False):
        """quick — только масштаб последнего попадания: для проверок, где
        маркер уже ловился на этом автомате и полная лестница — лишняя работа."""
        best, best_s = 0.0, 1.0
        order = sorted(self.templates, key=lambda st: abs(st[0] - self.last_scale))
        if quick:
            order = order[:1]
        for s, t in order:
            sc = _marker_score(gray_tol, t)
            if sc > best:
                best, best_s = sc, s
            if best >= DETECT_TOLERANT_THRESHOLD:
                break
        if best >= DETECT_TOLERANT_THRESHOLD:
            self.last_scale = best_s
        return best, best_s


NO_EXIT = (False, 0.0, 0.0, 1.0, "")   # «выход не считался» для CSV/отладки


class _Detector:
    """
    Вход (marker1): основной детект (полный шаблон, DETECT_SCALE, THRESHOLD)
    плюс терпимый; найден, если сработал любой из двух, основной первым.

    Выход: набор эталонов шапки, ищутся только в верхней доле кадра
    (exit_band), берётся лучший. Терпимый детектор для них по умолчанию
    выключен (DETECT_TOLERANT_EXIT): на записи с автомата он давал до 0.80
    на кадрах без шапки, а основной — не больше 0.28.
    """

    def __init__(self, marker1_small, exit_markers):
        self.marker1 = marker1_small
        self.exits   = list(exit_markers)          # [(name, small)]
        self.tol1    = _TolerantMatcher(marker1_small, *_core_for(marker1_small)) if DETECT_TOLERANT else None
        self.tol_exit = ({n: _TolerantMatcher(m, *_core_for(m)) for n, m in self.exits}
                         if DETECT_TOLERANT_EXIT else {})
        self.gray = None
        self.gray_tol = None
        self.band = None
        self.band_tol = None

    def prepare(self, gray_small):
        self.gray = gray_small
        self.gray_tol = _prep_tolerant(gray_small) if self.tol1 is not None else None
        self.band = exit_band(gray_small)
        self.band_tol = _prep_tolerant(self.band) if self.tol_exit else None

    def score(self, which, quick=False):
        """marker1 -> (hit, primary, tolerant, tolerant_scale); tolerant = 0.0, если не считался."""
        assert which == 1
        primary = _marker_score(self.gray, self.marker1)
        if primary >= THRESHOLD or self.tol1 is None:
            return primary >= THRESHOLD, primary, 0.0, 1.0
        t_sc, t_s = self.tol1.score(self.gray_tol, quick=quick)
        return t_sc >= DETECT_TOLERANT_THRESHOLD, primary, t_sc, t_s

    def score_exit(self):
        """
        -> (best, per): best = (hit, primary, tolerant, scale, name) лучшего
        эталона, per = [(name, primary), ...] по всем — для диагностики
        ad_stuck, где важно видеть, какой шапки не хватает.
        """
        per = [(n, _marker_score(self.band, m)) for n, m in self.exits]
        name, primary = max(per, key=lambda x: x[1]) if per else ("", 0.0)
        if primary >= THRESHOLD or not self.tol_exit:
            return (primary >= THRESHOLD, primary, 0.0, 1.0, name), per
        best = (False, primary, 0.0, 1.0, name)
        for n, _ in per:
            t_sc, t_s = self.tol_exit[n].score(self.band_tol)
            if t_sc > best[2]:
                best = (t_sc >= DETECT_TOLERANT_THRESHOLD, primary, t_sc, t_s, n)
            if best[0]:
                break
        return best, per


def _warn_if_template_fills_frame(gray_small, markers):
    """
    Эталон размером с кадр вырождает matchTemplate в сравнение экрана целиком:
    карта откликов 1x1, никакой устойчивости к сдвигу кадрирования. Так всё ещё
    устроен marker.png — заставка «DARTSLIVE 3» на весь экран. marker2.png уже
    фрагмент (шапка меню), и предупреждение по нему не печатается.
    Терпимый детектор (_TolerantMatcher) это компенсирует.
    """
    band = exit_band(gray_small)
    for name, m in markers:
        if m is None:
            continue
        region = band if name.startswith("exit/") else gray_small
        if m.shape[0] > region.shape[0] or m.shape[1] > region.shape[1]:
            print(f"[WARN] {name}: шаблон {m.shape[1]}x{m.shape[0]} больше области поиска "
                  f"{region.shape[1]}x{region.shape[0]} — не совпадёт никогда"
                  + (" (см. EXIT_SEARCH_BAND)" if name.startswith("exit/") else ""))
        elif (m.shape[0] >= gray_small.shape[0] and m.shape[1] >= gray_small.shape[1]):
            print(f"[WARN] {name}: шаблон {m.shape[1]}x{m.shape[0]} совпадает с кадром "
                  f"{gray_small.shape[1]}x{gray_small.shape[0]} при DETECT_SCALE={DETECT_SCALE} — "
                  f"сравнивается весь экран, сдвиг кадрирования сломает детект")


class _Debouncer:
    """
    Подтверждение маркера: DEBOUNCE_FRAMES попаданий подряд, идущих не реже
    чем раз в max_gap секунд.

    Ограничение по времени здесь принципиально. Чистый счётчик кадров зависит
    от загрузки CPU: в STATE_VIDEO частота обработки падает в разы, и окно
    подтверждения растягивалось на секунды — маркер, живущий около секунды,
    не проходил его никогда. Но и фиксированный max_gap опасен: при просадке
    ниже ~2.5 fps два соседних кадра уже дальше 0.4 с друг от друга, и
    подтверждение не наступало вовсе. Поэтому max_gap передаётся снаружи и
    растёт вместе с реальным интервалом между кадрами.
    """

    def __init__(self):
        self.count    = 0
        self.last_hit = 0.0

    def hit(self, now, max_gap=DEBOUNCE_MAX_GAP):
        if self.count and now - self.last_hit > max_gap:
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

    def flush(self, now, state, r1, r2, per, gap, det_ms):
        elapsed = now - self.since
        fps = self.frames / elapsed if elapsed > 0 else 0.0
        fmt = lambda r: f"{r[1]:.3f}" + (f"/tol {r[2]:.3f}@{r[3]:.2f}" if r[2] else "")
        others = " ".join(f"{n} {p:.2f}" for n, p in per if n != r2[4])
        print(f"[DETECT] state={state} fps={fps:.1f} det={det_ms:.0f}ms marker1={fmt(r1)} "
              f"exit={r2[4]} {fmt(r2)} | {others} "
              f"(порог {THRESHOLD}/{DETECT_TOLERANT_THRESHOLD}, gap {gap:.2f}s)")
        self.frames = 0
        self.since  = now


# Сколько секунд после входа в рекламу не реагировать на marker1: заставка,
# которая её запустила, ещё догорает на экране и иначе тут же засчиталась бы
# как «новый цикл».
_MARKER1_REARM_SEC = 3.0
# Как часто обновлять fault_detail у залипшей рекламы (длительность растёт).
_STUCK_REPORT_EVERY = 30.0


def capture_thread_fn(cap_live, marker1_small, exit_markers, shared, stop_event, sm,
                      reopen=None):
    """
    reopen(cap) -> новый cap или None. Если передан, поток сам переживает
    выдернутую на ходу карту захвата: помечает shared["fault"], репортит
    серверу state=error и пытается переоткрыть устройство, вместо того чтобы
    молча крутить пустой read() с застывшим последним кадром на экране.
    dev.py вызывает функцию без reopen — там источник кадров синтетический.

    exit_markers — [(name, small)] из load_markers(): набор эталонов шапки,
    любой из них заканчивает рекламу.

    Залипание в рекламе (шапка так и не пришла) здесь не «лечится»
    таймаутом — заказчик от этого отказался. Вместо этого:
      * маркеры ищет пара детекторов: основной и терпимый к масштабу (_Detector);
      * окно подтверждения растёт вместе с интервалом между кадрами;
      * marker1 во время рекламы перезапускает ролик — новый цикл автомата
        означает, что прошлый выход был пропущен, и это считается;
      * после STUCK_WARN_SEC серверу уходит fault=ad_stuck с диагностикой,
        чтобы проблему было видно в дэшборде, а не через сутки по логам;
      * STUCK_EXIT_SEC — выключенный по умолчанию аварийный рубильник.
    Без роликов в плейлисте рекламный режим не включается вовсе, а если
    плейлист опустел посреди рекламы — возвращаемся в трансляцию.

    Здесь же живёт запись экрана (Recorder): по файлу-запросу пишет видео,
    снимки и отклики детектора — из них подбираются эталоны для режимов
    автомата, которых набор эталонов ещё не знает. Пока идёт запись, оба
    маркера считаются на каждом кадре независимо от состояния: шапка нужна в
    CSV и в LIVE, а лишний matchTemplate стоит копейки и только на время записи.
    """
    det            = _Detector(marker1_small, exit_markers)
    rec            = Recorder()
    frame_count    = 0
    deb_live       = _Debouncer()   # ждём marker1, чтобы уйти в рекламу
    deb_video      = _Debouncer()   # ждём шапку (любой эталон выхода), чтобы вернуться к трансляции
    deb_rearm      = _Debouncer()   # marker1 во время рекламы — новый цикл
    rearm_armed    = True           # одна экспозиция marker1 = один перезапуск
    noads_logged   = 0.0            # чтобы «плейлист пуст» не сыпалось на каждый кадр
    cooldown_until = 0.0
    checked_sizes  = False
    dbg            = _DebugMeter() if MARKER_DEBUG else None
    last_good      = time.time()

    # Реальный интервал между обработанными кадрами (EMA) — от него зависит
    # окно подтверждения маркера. Стартовое значение — типичный LIVE-fps.
    interval       = 1.0 / 15.0
    prev_frame_ts  = None

    # Диагностика залипания: копится за один сеанс рекламы, сбрасывается в LIVE.
    stuck_warned   = False
    last_stuck_rep = 0.0
    exit_session_max = {}           # имя эталона -> max отклик за сеанс рекламы
    missed_marker2 = 0
    rec_transitions = sm.transitions   # снимок на каждом переходе состояния

    def leave_video_diag():
        nonlocal stuck_warned, last_stuck_rep, missed_marker2
        stuck_warned, last_stuck_rep, missed_marker2 = False, 0.0, 0
        exit_session_max.clear()
        if shared.get("fault") == FAULT_AD_STUCK:
            shared["fault"] = None
            shared["fault_detail"] = None
            heartbeat_event.set()

    while not stop_event.is_set():
        ret, frame = cap_live.read()
        if not ret:
            stalled = time.time() - last_good
            if stalled < CAPTURE_STALL_SEC:
                # Одиночные пропуски кадров для USB-захвата нормальны.
                time.sleep(0.01)
                continue
            if shared.get("fault") != FAULT_CAPTURE_LOST:
                shared["fault"] = FAULT_CAPTURE_LOST
                shared["fault_detail"] = f"нет кадров {stalled:.0f}s"
                heartbeat_event.set()
                print(f"[CAPTURE] нет кадров {stalled:.0f}s — карта захвата отвалилась")
            if reopen is None:
                time.sleep(0.5)
                continue
            new_cap = reopen(cap_live)
            if new_cap is None:
                stop_event.wait(CAPTURE_RETRY_SEC)
                continue
            cap_live = new_cap
            shared["cap"] = new_cap
            last_good = time.time()
            checked_sizes = False   # разрешение могло смениться — проверить заново
            print("[CAPTURE] карта захвата вернулась")
            continue

        if shared.get("fault") == FAULT_CAPTURE_LOST:
            shared["fault"] = None
            shared["fault_detail"] = None
            heartbeat_event.set()
        last_good = time.time()

        shared["live_frame"] = frame
        frame_count += 1
        if dbg is not None:
            dbg.tick()

        if frame_count % DETECT_EVERY_N != 0:
            continue

        now = time.time()
        if prev_frame_ts is not None:
            interval = 0.9 * interval + 0.1 * (now - prev_frame_ts)
        prev_frame_ts = now
        gap = max(DEBOUNCE_MAX_GAP, 3.0 * interval)

        small      = cv2.resize(frame, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if not checked_sizes:
            checked_sizes = True
            _warn_if_template_fills_frame(
                gray_small, [("marker.png", marker1_small)] + [(f"exit/{n}", m) for n, m in exit_markers])

        in_live = sm.state == STATE_LIVE
        in_video_for = 0.0 if in_live else sm.time_in_state()

        t_det = time.perf_counter()
        det.prepare(gray_small)
        per = []
        if in_live:
            r1 = det.score(1)
            r2, per = det.score_exit() if rec.active else (None, [])
        else:
            r2, per = det.score_exit()
            for n, p in per:
                exit_session_max[n] = max(exit_session_max.get(n, 0.0), p)
            if r2[2]:
                exit_session_max[r2[4]] = max(exit_session_max.get(r2[4], 0.0), r2[2])
            # marker1 здесь нужен только для «нового цикла»; в рекламу мы уже
            # вошли по нему, масштаб известен — полная лестница не нужна.
            r1 = det.score(1, quick=True)
        hit1 = r1[0]
        hit2 = r2[0] if r2 is not None else False
        det_ms = (time.perf_counter() - t_det) * 1000.0

        if dbg is not None and dbg.due(now):
            if r2 is None:
                r2_dbg, per_dbg = det.score_exit()
            else:
                r2_dbg, per_dbg = r2, per
            dbg.flush(now, sm.state, r1, r2_dbg, per_dbg, gap, det_ms)

        # Запись экрана: запрос проверяется раз в секунду, кадр уходит в
        # запись до любых `continue` ниже, чтобы CSV покрывал каждый кадр.
        rec.poll(now)
        if rec.active:
            rec.feed(now, frame, gray_small, sm.state, in_video_for, interval, gap, det_ms,
                     r1, r2 if r2 is not None else NO_EXIT, hit1, hit2,
                     forced_snapshot=(sm.transitions != rec_transitions))
            rec_transitions = sm.transitions

        # --- диагностика и аварийный выход из залипшей рекламы -----------------
        if not in_live and in_video_for > STUCK_WARN_SEC:
            if not stuck_warned:
                stuck_warned = True
                print(f"[WARN] в рекламе уже {in_video_for:.0f}s без шапки — "
                      f"проверь детект (MARKER_DEBUG=1)")
            if now - last_stuck_rep >= _STUCK_REPORT_EVERY:
                first = shared.get("fault") != FAULT_AD_STUCK
                shared["fault"] = FAULT_AD_STUCK
                # Сервер режет fault_detail до 200 символов — имена коротко.
                top = sorted(exit_session_max.items(), key=lambda kv: -kv[1])[:6]
                shared["fault_detail"] = (f"{in_video_for:.0f}s без шапки; exit max: "
                                          + ", ".join(f"{n[:14]} {v:.2f}" for n, v in top)
                                          + f"; marker1 повторно x{missed_marker2}")
                last_stuck_rep = now
                if first:
                    heartbeat_event.set()
            if STUCK_EXIT_SEC > 0 and in_video_for > STUCK_EXIT_SEC:
                print(f"[WARN] STUCK_EXIT_SEC={STUCK_EXIT_SEC:.0f}: принудительный выход "
                      f"из рекламы после {in_video_for:.0f}s")
                sm.transition(STATE_LIVE)
                leave_video_diag()
                deb_live.reset(); deb_video.reset(); deb_rearm.reset()
                cooldown_until = now + MARKER_COOLDOWN
                continue

        if now < cooldown_until:
            deb_live.reset(); deb_video.reset(); deb_rearm.reset()
            continue

        # --- нет роликов — нет и рекламного режима ------------------------------
        # Пустой плейлист (первая установка, рекламу сняли, ролик ещё не
        # докачался) раньше не мешал уйти в STATE_VIDEO: mpv показывать было
        # нечего, а главный цикл в этом состоянии переставал рисовать живой
        # кадр — нижний дисплей замирал на заставке до конца игры. Теперь без
        # роликов автомат ведёт себя так, будто агента нет: трансляция идёт.
        have_ads = bool(get_playlist())
        if not in_live and not have_ads:
            print("[STATE] плейлист опустел во время рекламы — возвращаюсь в трансляцию")
            sm.transition(STATE_LIVE)
            leave_video_diag()
            deb_live.reset(); deb_video.reset(); deb_rearm.reset()
            cooldown_until = now + MARKER_COOLDOWN
            continue

        # --- основной переход по своему маркеру --------------------------------
        # Выход из рекламы по шапке проверяется первым и имеет приоритет:
        # экран меню может одновременно давать высокий отклик и на marker1
        # (общая шапка DARTSLIVE), и если бы ветка «новый цикл» стояла раньше,
        # она перехватывала бы кадр с меню и реклама не заканчивалась бы никогда.
        target_hit = hit1 if in_live else hit2
        deb = deb_live if in_live else deb_video
        if target_hit:
            if deb.hit(now, gap):
                if in_live and not have_ads:
                    if now - noads_logged > 10.0:
                        noads_logged = now
                        print("[STATE] marker1, но плейлист пуст — остаюсь в трансляции")
                else:
                    if not in_live:
                        print(f"[DETECT] выход из рекламы по exit/{r2[4]}: primary={r2[1]:.3f}"
                              + (f" tol={r2[2]:.3f}@{r2[3]:.2f}" if r2[2] else ""))
                    sm.transition(STATE_VIDEO if in_live else STATE_LIVE)
                    if in_live:
                        shared["video_restart"] = True
                    else:
                        leave_video_diag()
                deb_live.reset(); deb_video.reset(); deb_rearm.reset()
                cooldown_until = now + MARKER_COOLDOWN
            continue
        deb.reset()

        # --- marker1 во время рекламы: автомат начал новый цикл ----------------
        # Сюда попадаем только если кадр точно не шапка. Заставка живёт на
        # экране около секунды — за одну экспозицию срабатываем один раз и
        # взводимся заново, только когда marker1 пропал.
        if not in_live and in_video_for > _MARKER1_REARM_SEC and hit1:
            if rearm_armed and deb_rearm.hit(now, gap):
                rearm_armed = False
                missed_marker2 += 1
                shared["video_restart"] = True
                print(f"[DETECT] marker1 во время рекламы (x{missed_marker2}) — "
                      f"прошлая шапка, вероятно, пропущена; ролик перезапущен")
                if shared.get("fault") == FAULT_AD_STUCK:
                    last_stuck_rep = 0.0   # обновить fault_detail на следующем кадре
                deb_rearm.reset()
                cooldown_until = now + MARKER_COOLDOWN
        else:
            deb_rearm.reset()
            if not hit1:
                rearm_armed = True

    rec.stop("shutdown")
