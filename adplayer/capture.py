import time

import cv2

from .config import DETECT_SCALE, DETECT_EVERY_N, THRESHOLD, DEBOUNCE_FRAMES
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


def _marker_found(small_gray, marker_small, threshold):
    if marker_small is None or small_gray is None:
        return False
    if (small_gray.shape[0] < marker_small.shape[0] or
            small_gray.shape[1] < marker_small.shape[1]):
        return False
    res = cv2.matchTemplate(small_gray, marker_small, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return max_val >= threshold


DETECT_EVERY_N_VIDEO = DETECT_EVERY_N * 4


def capture_thread_fn(cap_live, marker1_small, marker2_small, shared, stop_event, sm):
    frame_count       = 0
    debounce_count    = 0
    marker1_triggered = False

    while not stop_event.is_set():
        ret, frame = cap_live.read()
        if not ret:
            time.sleep(0.01)
            continue

        shared["live_frame"] = frame
        frame_count += 1

        detect_every = DETECT_EVERY_N if sm.state == STATE_LIVE else DETECT_EVERY_N_VIDEO
        if frame_count % detect_every != 0:
            continue

        small      = cv2.resize(frame, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if sm.state == STATE_LIVE:
            if marker1_triggered:
                # marker1 уже сработал — ждём marker2, игнорируем повторный marker1
                if _marker_found(gray_small, marker2_small, THRESHOLD):
                    debounce_count += 1
                    if debounce_count >= DEBOUNCE_FRAMES:
                        marker1_triggered = False
                        debounce_count = 0
                else:
                    debounce_count = 0
            else:
                if _marker_found(gray_small, marker1_small, THRESHOLD):
                    debounce_count += 1
                    if debounce_count >= DEBOUNCE_FRAMES:
                        sm.transition(STATE_VIDEO)
                        shared["video_restart"] = True
                        marker1_triggered = True
                        debounce_count = 0
                else:
                    debounce_count = 0
        else:
            if _marker_found(gray_small, marker2_small, THRESHOLD):
                debounce_count += 1
                if debounce_count >= DEBOUNCE_FRAMES:
                    sm.transition(STATE_LIVE)
                    marker1_triggered = False
                    debounce_count = 0
            else:
                debounce_count = 0
