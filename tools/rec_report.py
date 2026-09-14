#!/usr/bin/env python3
"""
Отчёт по записи экрана автомата (adplayer/recorder.py) — запускается на
рабочей машине, не на Pi.

    python3 tools/rec_report.py ~/rec/20260914-171502 \
        --templates public/marker2.png cand/online.png [--marker1 public/marker.png] \
        [--every 2] [--top 8] [--no-sheet] [--replay]

Что делает:
  * контактный лист снимков shots/ → <rec>/shots_sheet_N.jpg — по нему видно
    все различные экраны сессии и номера снимков;
  * для каждого эталона считает отклики боевого детектора (основной и
    терпимый — те же функции из adplayer.capture) на всех снимках и на каждом
    N-м кадре screen.avi. Положительные кадры для эталона — снимки, скопированные
    в <rec>/labels/<имя эталона>/ (имя = имя файла эталона без .png); всё
    остальное, кроме кадров в ±1.5 с вокруг положительных снимков, — отрицательные.
    Печатает min/median на положительных, max/p99 на отрицательных, запас до
    THRESHOLD и самые «опасные» отрицательные кадры;
  * marker1: максимум по всем кадрам — реклама не должна включаться посреди игры;
  * --replay: прогон записи через боевой capture_thread_fn на виртуальных
    часах (по временам из detect.csv), печатает каждый переход состояния и итог.
    Эталон выхода в прогоне — первый из --templates.
"""

import argparse
import csv
import glob
import os
import re
import sys
import threading

import cv2
import numpy as np

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, _ROOT)
# Чтобы прогон не подхватил чей-то файл-запрос и не начал писать новую запись.
os.environ.setdefault("REC_REQUEST_PATH", os.path.join(_ROOT, ".rec_report_no_request"))
os.environ.setdefault("RECORD_ON_START_SEC", "0")

from adplayer import capture, state as state_mod             # noqa: E402
from adplayer.capture import (                                # noqa: E402
    _marker_score, _prep_tolerant, _TolerantMatcher, _TOL_CORE,
)
from adplayer.config import (                                 # noqa: E402
    DETECT_SCALE, THRESHOLD, DETECT_TOLERANT_THRESHOLD, CAPTURE_WIDTH, CAPTURE_HEIGHT,
)

_SNAP_RE = re.compile(r"^(\d+)_t(\d+\.\d)_(\w+)\.png$")
# Окно вокруг положительного снимка, в котором кадры не считаются отрицательными.
_NEAR_POS_SEC = 1.5


def _core_for(small):
    h, w = small.shape
    return _TOL_CORE[2] if h / w < 0.25 else _TOL_CORE[1]


class Template:
    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        gray = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise SystemExit(f"не читается эталон: {path}")
        self.full  = gray
        self.small = cv2.resize(gray, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
        self.tol   = _TolerantMatcher(self.small, *_core_for(self.small))

    def score(self, gray_small, gray_tol):
        primary = _marker_score(gray_small, self.small)
        tol, scale = self.tol.score(gray_tol)
        return primary, tol, scale


def _prep(frame):
    if frame.shape[1] < CAPTURE_WIDTH or frame.shape[0] < CAPTURE_HEIGHT:
        frame = cv2.resize(frame, (CAPTURE_WIDTH, CAPTURE_HEIGHT), interpolation=cv2.INTER_LINEAR)
    small = cv2.resize(frame, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
    gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return gray_small, _prep_tolerant(gray_small)


def _read_csv(rec):
    path = os.path.join(rec, "detect.csv")
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        pass
    return rows


def _snap_meta(name):
    m = _SNAP_RE.match(name)
    if not m:
        return None, None, None
    return int(m.group(1)), float(m.group(2)), m.group(3)


def contact_sheet(rec, shots, cols=6, rows=8, thumb=(320, 180)):
    per = cols * rows
    out = []
    for si in range(0, len(shots), per):
        chunk = shots[si:si + per]
        sheet = np.full((rows * (thumb[1] + 22), cols * thumb[0], 3), 30, np.uint8)
        for i, path in enumerate(chunk):
            img = cv2.imread(path)
            if img is None:
                continue
            t = cv2.resize(img, thumb, interpolation=cv2.INTER_AREA)
            r, c = divmod(i, cols)
            y, x = r * (thumb[1] + 22), c * thumb[0]
            sheet[y:y + thumb[1], x:x + thumb[0]] = t
            idx, rec_t, st = _snap_meta(os.path.basename(path))
            label = f"#{idx} t={rec_t:.0f}s {st}" if idx is not None else os.path.basename(path)
            cv2.putText(sheet, label, (x + 4, y + thumb[1] + 16), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (255, 255, 255), 1, cv2.LINE_AA)
        p = os.path.join(rec, f"shots_sheet_{si // per + 1}.jpg")
        cv2.imwrite(p, sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
        out.append(p)
    return out


def iter_frames(rec, shots, every):
    """(source, label, rec_t, frame) — снимки и каждый every-й кадр видео."""
    for path in shots:
        img = cv2.imread(path)
        if img is None:
            continue
        idx, rec_t, _ = _snap_meta(os.path.basename(path))
        yield "shot", os.path.basename(path), rec_t if rec_t is not None else -1.0, img

    rows = _read_csv(rec)
    t_by_frame = {}
    for r in rows:
        if r.get("vid_frame"):
            t_by_frame[int(r["vid_frame"])] = float(r["rec_t"])
    video = os.path.join(rec, "screen.avi")
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"[warn] нет видео {video}")
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or 5.0
    i = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if i % every == 0:
            yield "video", f"frame {i}", t_by_frame.get(i, i / fps), frame
        i += 1
    cap.release()


def report(rec, templates, marker1, every, top):
    shots = sorted(glob.glob(os.path.join(rec, "shots", "*.png")))
    labels = {t.name: {os.path.basename(p) for p in
                       glob.glob(os.path.join(rec, "labels", t.name, "*.png"))}
              for t in templates}
    pos_times = {t.name: sorted(_snap_meta(n)[1] for n in labels[t.name] if _snap_meta(n)[1] is not None)
                 for t in templates}

    scores = {t.name: {"pos": [], "neg": [], "near": 0} for t in templates}
    m1_all = []
    n = 0
    for src, label, rec_t, frame in iter_frames(rec, shots, every):
        n += 1
        gs, gt = _prep(frame)
        for t in templates:
            primary, tol, scale = t.score(gs, gt)
            best = max(primary, tol)
            is_pos = (src == "shot" and label in labels[t.name])
            # Кадры рядом с положительным снимком — та же шапка, только не
            # размеченная (снимок на переходе состояния дублирует предыдущий,
            # видео вокруг него тоже её показывает). В отрицательные их не берём.
            near_pos = any(abs(rec_t - pt) <= _NEAR_POS_SEC for pt in pos_times[t.name])
            if is_pos:
                scores[t.name]["pos"].append((best, primary, tol, scale, label, rec_t))
            elif near_pos:
                scores[t.name]["near"] += 1
            else:
                scores[t.name]["neg"].append((best, primary, tol, scale, label, rec_t))
        if marker1 is not None:
            p, tl, sc = marker1.score(gs, gt)
            m1_all.append((max(p, tl), p, tl, sc, label, rec_t))

    print(f"\nКадров проверено: {n} (снимков {len(shots)}, видео каждый {every}-й кадр)")
    print(f"Порог: основной {THRESHOLD}, терпимый {DETECT_TOLERANT_THRESHOLD}\n")
    for t in templates:
        pos = sorted(scores[t.name]["pos"], reverse=True)
        neg = sorted(scores[t.name]["neg"], reverse=True)
        print(f"=== {t.name}  ({t.full.shape[1]}x{t.full.shape[0]}, positives: labels/{t.name}/ → {len(pos)})")
        if pos:
            vals = [p[0] for p in pos]
            print(f"  положительные: min {min(vals):.3f}  median {np.median(vals):.3f}  max {max(vals):.3f}")
            below = [p for p in pos if p[0] < THRESHOLD]
            for b in below:
                print(f"    ниже порога: {b[4]} best={b[0]:.3f} (primary {b[1]:.3f}, tol {b[2]:.3f}@{b[3]:.2f})")
        else:
            print("  положительных нет — скопируй снимки с этой шапкой в labels/{}/".format(t.name))
        if neg:
            vals = [p[0] for p in neg]
            print(f"  отрицательные: max {max(vals):.3f}  p99 {np.percentile(vals, 99):.3f}  "
                  f"median {np.median(vals):.3f}  (n={len(neg)}, ещё {scores[t.name]['near']} "
                  f"кадров в ±{_NEAR_POS_SEC:g}s от положительных не учтены)")
            if pos:
                margin = min(p[0] for p in pos) - max(vals)
                verdict = "OK" if (min(p[0] for p in pos) >= 0.85 and max(vals) <= 0.6) else "СЛАБО"
                print(f"  запас (pos_min − neg_max): {margin:+.3f}  → {verdict}")
            print(f"  самые опасные отрицательные (top {top}):")
            for b in neg[:top]:
                print(f"    {b[4]:<28} t={b[5]:7.1f}s best={b[0]:.3f} (primary {b[1]:.3f}, tol {b[2]:.3f}@{b[3]:.2f})")
        print()

    if m1_all:
        m1_all.sort(reverse=True)
        print(f"=== marker1 ({marker1.path}): max {m1_all[0][0]:.3f} по всем кадрам")
        for b in m1_all[:top]:
            print(f"    {b[4]:<28} t={b[5]:7.1f}s best={b[0]:.3f} (primary {b[1]:.3f}, tol {b[2]:.3f}@{b[3]:.2f})")
        print()


# --- прогон через боевой capture_thread_fn на виртуальных часах --------------

class _Clock:
    def __init__(self, t0):
        self.t = t0

    def time(self):
        return self.t

    def perf_counter(self):
        return self.t

    def sleep(self, d):
        self.t += max(0.0, d)


class _Source:
    """Кадры screen.avi; часы прыгают на время кадра из detect.csv."""

    def __init__(self, rec, clock, stop_event):
        self.cap = cv2.VideoCapture(os.path.join(rec, "screen.avi"))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 5.0
        self.t_by_frame = {}
        for r in _read_csv(rec):
            if r.get("vid_frame"):
                self.t_by_frame[int(r["vid_frame"])] = float(r["rec_t"])
        self.i = 0
        self.clock = clock
        self.stop_event = stop_event

    def read(self):
        ret, frame = self.cap.read()
        if not ret:
            self.stop_event.set()
            return False, None
        self.clock.t = self.t_by_frame.get(self.i, self.i / self.fps)
        self.i += 1
        if frame.shape[1] < CAPTURE_WIDTH:
            frame = cv2.resize(frame, (CAPTURE_WIDTH, CAPTURE_HEIGHT), interpolation=cv2.INTER_LINEAR)
        return True, frame

    def release(self):
        self.cap.release()


def replay(rec, marker1, exit_template):
    clock = _Clock(0.0)
    capture.time = clock
    state_mod.time = clock
    capture.get_playlist = lambda: ["ad.mp4"]

    transitions = []

    def on_change(old, new, dur):
        transitions.append((clock.t, old, new, dur))
        print(f"  t={clock.t:7.1f}s  {old} → {new}  (было {dur:.1f}s)")

    sm = state_mod.StateManager(on_change_callback=on_change)
    stop_event = threading.Event()
    src = _Source(rec, clock, stop_event)
    shared = {"live_frame": None, "video_restart": False, "current_video": None, "fault": None}
    print("Прогон записи через capture_thread_fn (виртуальные часы):")
    capture.capture_thread_fn(src, marker1.small, exit_template.small, shared, stop_event, sm)
    exits = sum(1 for t in transitions if t[2] == state_mod.STATE_LIVE)
    print(f"Итого: {len(transitions)} переходов, {exits} выходов из рекламы, "
          f"{len(transitions) - exits} входов; длительность записи {clock.t:.0f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rec")
    ap.add_argument("--templates", nargs="+", default=[os.path.join(_ROOT, "public", "marker2.png")])
    ap.add_argument("--marker1", default=os.path.join(_ROOT, "public", "marker.png"))
    ap.add_argument("--every", type=int, default=1, help="каждый N-й кадр видео")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--replay", action="store_true")
    a = ap.parse_args()

    templates = [Template(p) for p in a.templates]
    marker1 = Template(a.marker1) if a.marker1 and os.path.exists(a.marker1) else None

    if not a.no_sheet:
        shots = sorted(glob.glob(os.path.join(a.rec, "shots", "*.png")))
        for p in contact_sheet(a.rec, shots):
            print(f"контактный лист: {p}")
    report(a.rec, templates, marker1, a.every, a.top)
    if a.replay:
        if marker1 is None:
            raise SystemExit("--replay требует --marker1")
        replay(a.rec, marker1, templates[0])


if __name__ == "__main__":
    main()
