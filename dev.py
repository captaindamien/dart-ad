"""
dev.py — локальная эмуляция prod.py без capture card и второго монитора.

Заменители:
  - Capture card  → видеофайл в цикле (как «живой» поток)
  - Маркеры       → клавиши 1 и 2
  - Второй монитор → обычное окно

Клавиши:
  1       — симулировать marker1 (LIVE → VIDEO)
  2       — симулировать marker2 (VIDEO → LIVE)
  r       — сбросить в STATE_LIVE
  q / Esc — выход
"""

import cv2
import numpy as np
import threading
import time
import os

from core import StateManager, STATE_LIVE, STATE_VIDEO

BASE       = os.path.join(os.path.dirname(__file__), "public")
VIDEO_PATH = os.path.join(BASE, "0213.mp4")

# Отдельный видеофайл для «живого» потока. Если не задан — генерируем синий фон.
LIVE_VIDEO_PATH = None  # например: os.path.join(BASE, "live_source.mp4")


# ── Колбэк для бэкенда ─────────────────────────────────────────────────────────
def on_state_change(old, new, duration):
    """Точка интеграции с бэкендом. Сейчас просто логирует."""
    print(f"[BACKEND] {old} → {new}, duration={duration:.2f}s")
    # TODO: заменить на HTTP-запрос:
    # requests.post("https://your-backend/api/events", json={
    #     "from": old, "to": new, "duration": duration,
    #     "timestamp": time.time()
    # })


# ── Синтетический «живой» поток ────────────────────────────────────────────────
def make_live_source():
    """Возвращает VideoCapture или None (тогда используем генератор кадров)."""
    if LIVE_VIDEO_PATH and os.path.exists(LIVE_VIDEO_PATH):
        cap = cv2.VideoCapture(LIVE_VIDEO_PATH)
        if cap.isOpened():
            return cap
    return None


def generate_live_frame(t):
    """Генерирует синий фон с анимированным таймером — имитирует «живой» поток."""
    frame = np.zeros((480, 854, 3), dtype=np.uint8)
    frame[:] = (80, 40, 20)  # тёмно-синий
    secs = int(t) % 60
    cv2.putText(frame, f"LIVE SOURCE  {secs:02d}s", (30, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 255), 2, cv2.LINE_AA)
    return frame


# ── Поток видео (реклама) ──────────────────────────────────────────────────────
def video_thread_fn(cap_video, shared, stop_event):
    video_fps   = cap_video.get(cv2.CAP_PROP_FPS) or 30
    frame_delay = 1.0 / video_fps
    last_time   = 0.0

    while not stop_event.is_set():
        if shared["state"] != STATE_VIDEO:
            time.sleep(0.02)
            last_time = 0.0
            continue

        if shared.get("video_restart"):
            cap_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
            last_time = 0.0
            shared["video_restart"] = False

        now = time.time()
        if now - last_time < frame_delay:
            time.sleep(0.005)
            continue

        ret, frame = cap_video.read()
        if not ret:
            cap_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = cap_video.read()
        if ret:
            shared["video_frame"] = frame
        last_time = now


# ── Поток «живого» источника ───────────────────────────────────────────────────
def live_thread_fn(cap_live, shared, stop_event):
    t0 = time.time()
    fps_delay = 1.0 / 30
    last_time = 0.0

    while not stop_event.is_set():
        now = time.time()
        if now - last_time < fps_delay:
            time.sleep(0.005)
            continue
        last_time = now

        if cap_live is not None:
            ret, frame = cap_live.read()
            if not ret:
                cap_live.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap_live.read()
            if ret:
                shared["live_frame"] = frame
        else:
            shared["live_frame"] = generate_live_frame(time.time() - t0)


# ── HUD overlay ────────────────────────────────────────────────────────────────
def draw_hud(frame, sm):
    h, w = frame.shape[:2]
    overlay = frame.copy()

    state_label = "LIVE" if sm.state == STATE_LIVE else "VIDEO (AD)"
    state_color = (0, 200, 80) if sm.state == STATE_LIVE else (0, 80, 220)
    elapsed     = sm.time_in_state()

    # Полупрозрачная плашка
    cv2.rectangle(overlay, (0, 0), (w, 54), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    cv2.putText(frame, f"[DEV]  state: {state_label}   {elapsed:.1f}s   transitions: {sm.transitions}",
                (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.7, state_color, 2, cv2.LINE_AA)
    cv2.putText(frame, "1=AD  2=LIVE  r=reset  q=quit",
                (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1, cv2.LINE_AA)
    return frame


# ── main ───────────────────────────────────────────────────────────────────────
def main():
    cap_video = cv2.VideoCapture(VIDEO_PATH)
    if not cap_video.isOpened():
        print(f"Ошибка: не удалось открыть {VIDEO_PATH}")
        return

    cap_live = make_live_source()

    sm = StateManager(on_change_callback=on_state_change)

    shared = {
        "state":         sm.state,
        "live_frame":    None,
        "video_frame":   None,
        "video_restart": False,
    }

    stop_event = threading.Event()

    t_live  = threading.Thread(target=live_thread_fn,  args=(cap_live, shared, stop_event), daemon=True)
    t_video = threading.Thread(target=video_thread_fn, args=(cap_video, shared, stop_event), daemon=True)
    t_live.start()
    t_video.start()

    win = "dart-ad  [DEV MODE]"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 854, 480)

    print("\nDEV MODE запущен.")
    print("  1  → симулировать marker1  (LIVE → VIDEO)")
    print("  2  → симулировать marker2  (VIDEO → LIVE)")
    print("  r  → сбросить в STATE_LIVE")
    print("  q / Esc → выход\n")

    while True:
        shared["state"] = sm.state

        if sm.state == STATE_LIVE:
            frame = shared.get("live_frame")
        else:
            frame = shared.get("video_frame")

        if frame is not None:
            display = draw_hud(frame.copy(), sm)
            cv2.imshow(win, display)

        key = cv2.waitKey(16) & 0xFF

        if key == ord("1"):
            if sm.state == STATE_LIVE:
                sm.transition(STATE_VIDEO)
                shared["video_restart"] = True
            else:
                print("[DEV] уже в STATE_VIDEO")

        elif key == ord("2"):
            if sm.state == STATE_VIDEO:
                sm.transition(STATE_LIVE)
            else:
                print("[DEV] уже в STATE_LIVE")

        elif key == ord("r"):
            sm.transition(STATE_LIVE)

        elif key in (ord("q"), 27):
            break

    stop_event.set()
    cap_video.release()
    if cap_live is not None:
        cap_live.release()
    cv2.destroyAllWindows()
    print("Завершено.")


if __name__ == "__main__":
    main()
