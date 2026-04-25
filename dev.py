"""
dev.py — локальная эмуляция prod.py без capture card и второго монитора.

Полностью использует prod.py: синхронизация плейлиста с бэкендом,
загрузка видео, хартбит. Маркеры заменены клавишами.

Клавиши:
  1       — симулировать marker1 (LIVE → VIDEO / запустить рекламу)
  2       — симулировать marker2 (VIDEO → LIVE)
  r       — сбросить в STATE_LIVE
  q / Esc — выход
"""

import cv2
import numpy as np
import threading
import time
import os

import prod as _prod
from core import StateManager, STATE_LIVE, STATE_VIDEO
from prod import (
    video_thread_fn, on_state_change,
    playlist_lock,
    sync_loop, heartbeat_loop, heartbeat_event,
    ADS_DIR, MACHINE_TOKEN, SERVER_URL,
)

# Опционально: путь к видеофайлу для имитации «живого» источника.
# None — генерируем синий фон с таймером.
LIVE_VIDEO_PATH = None


# ── Синтетический «живой» поток ────────────────────────────────────────────────
def make_live_source():
    if LIVE_VIDEO_PATH and os.path.exists(LIVE_VIDEO_PATH):
        cap = cv2.VideoCapture(LIVE_VIDEO_PATH)
        if cap.isOpened():
            return cap
    return None


def generate_live_frame(t):
    frame = np.zeros((480, 854, 3), dtype=np.uint8)
    frame[:] = (80, 40, 20)
    secs = int(t) % 60
    cv2.putText(frame, f"LIVE SOURCE  {secs:02d}s", (30, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 255), 2, cv2.LINE_AA)
    return frame


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
def draw_hud(frame, sm, shared):
    h, w = frame.shape[:2]
    overlay = frame.copy()

    state_label = "LIVE" if sm.state == STATE_LIVE else "VIDEO (AD)"
    state_color = (0, 200, 80) if sm.state == STATE_LIVE else (0, 80, 220)
    elapsed = sm.time_in_state()

    cv2.rectangle(overlay, (0, 0), (w, 54), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    video_name = shared.get("current_video") or ""
    video_info = f"   [{video_name}]" if sm.state == STATE_VIDEO and video_name else ""

    with playlist_lock:
        pl_count = len(_prod.server_playlist)

    cv2.putText(frame,
                f"[DEV]  {state_label}   {elapsed:.1f}s   trans:{sm.transitions}   pl:{pl_count}{video_info}",
                (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.7, state_color, 2, cv2.LINE_AA)
    cv2.putText(frame, "1=AD  2=LIVE  r=reset  q=quit",
                (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1, cv2.LINE_AA)
    return frame


# ── main ───────────────────────────────────────────────────────────────────────
def main():
    print("=== dart-ad DEV MODE ===")
    print(f"  SERVER_URL   = {SERVER_URL}")
    print(f"  ADS_DIR      = {ADS_DIR}")
    print(f"  MACHINE_TOKEN= {'✓ set' if MACHINE_TOKEN else '✗ NOT SET — sync/heartbeat disabled'}")
    print()

    cap_live = make_live_source()

    sm = StateManager(on_change_callback=on_state_change)

    shared = {
        "live_frame":    None,
        "video_frame":   None,
        "video_restart": False,
        "current_video": None,
    }

    stop_event = threading.Event()

    threads = [
        threading.Thread(target=sync_loop,      args=(stop_event,),                  daemon=True, name="sync"),
        threading.Thread(target=heartbeat_loop, args=(shared, stop_event, sm),        daemon=True, name="heartbeat"),
        threading.Thread(target=live_thread_fn, args=(cap_live, shared, stop_event),  daemon=True, name="live"),
        threading.Thread(target=video_thread_fn, args=(shared, stop_event, sm),       daemon=True, name="video"),
    ]
    for t in threads:
        t.start()

    win = "dart-ad  [DEV MODE]"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 854, 480)

    print("Управление:")
    print("  1  → marker1 (LIVE → VIDEO / запустить рекламу)")
    print("  2  → marker2 (VIDEO → LIVE)")
    print("  r  → сбросить в STATE_LIVE")
    print("  q / Esc → выход")
    print()
    print("Ждём первой синхронизации плейлиста…")

    while True:
        frame = shared.get("live_frame") if sm.state == STATE_LIVE else shared.get("video_frame")

        if frame is not None:
            display = draw_hud(frame.copy(), sm, shared)
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
    heartbeat_event.set()  # разблокировать поток хартбита
    if cap_live is not None:
        cap_live.release()
    cv2.destroyAllWindows()
    print("Завершено.")


if __name__ == "__main__":
    main()
