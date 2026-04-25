"""
ILSport Dart Ad Player — точка входа для продакшена.

Логика:
  LIVE  → обнаружен marker.png  → воспроизводит рекламные видео из плейлиста
  VIDEO → обнаружен marker2.png → возвращается к живому видео

Переменные окружения (из /etc/ilsport/env):
  SERVER_URL          — URL бэкенда (напр. https://your-server.com)
  MACHINE_TOKEN       — токен машины (X-Machine-Token)
  ADS_DIR             — папка для видео (по умолчанию ./public/ads)
  SYNC_INTERVAL       — интервал синхронизации плейлиста в секундах (по умолчанию 300)
  HEARTBEAT_INTERVAL  — интервал хартбита в секундах (по умолчанию 30)

Запуск:
  python main.py [X_offset]
  X_offset — горизонтальное смещение второго монитора (по умолчанию 1440)
"""

import sys
import threading
import subprocess

import cv2
import numpy as np

from adplayer.config import MARKER1_PATH, MARKER2_PATH, ADS_DIR, SERVER_URL
from adplayer.state import StateManager, STATE_LIVE, STATE_VIDEO
from adplayer.api import sync_loop, heartbeat_loop, heartbeat_event
from adplayer.capture import find_capture_device, load_markers, capture_thread_fn
from adplayer.player import video_thread_fn

MONITOR_X_OFFSET = int(sys.argv[1]) if len(sys.argv) > 1 else 1440


def on_state_change(old, new, duration):
    print(f"[STATE] {old} → {new}, duration={duration:.2f}s")
    heartbeat_event.set()


def main():
    try:
        subprocess.Popen(['unclutter', '-idle', '0', '-root'])
    except FileNotFoundError:
        pass

    try:
        marker1_small, marker2_small = load_markers(MARKER1_PATH, MARKER2_PATH)
    except FileNotFoundError as e:
        print(f"Ошибка: {e}"); sys.exit(1)

    print("Поиск устройства захвата…")
    cap_live, _ = find_capture_device(skip_first=False)
    if cap_live is None:
        print("Ошибка: карта захвата не найдена."); sys.exit(1)
    cap_live.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap_live.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap_live.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    win = "AD Display"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.imshow(win, np.zeros((1080, 1920, 3), dtype=np.uint8))
    cv2.waitKey(1)
    cv2.moveWindow(win, MONITOR_X_OFFSET, 0)
    cv2.waitKey(200)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    sm         = StateManager(on_change_callback=on_state_change)
    shared     = {"live_frame": None, "video_frame": None, "video_restart": False, "current_video": None}
    stop_event = threading.Event()

    threads = [
        threading.Thread(target=sync_loop,         args=(stop_event,),                                                   daemon=True, name="sync"),
        threading.Thread(target=heartbeat_loop,    args=(shared, stop_event, sm),                                         daemon=True, name="heartbeat"),
        threading.Thread(target=capture_thread_fn, args=(cap_live, marker1_small, marker2_small, shared, stop_event, sm), daemon=True, name="capture"),
        threading.Thread(target=video_thread_fn,   args=(shared, stop_event, sm),                                         daemon=True, name="video"),
    ]
    for t in threads:
        t.start()

    print(f"\nЗапущено. SERVER_URL={SERVER_URL}, ADS_DIR={ADS_DIR}, монитор X={MONITOR_X_OFFSET}. Нажмите 'q' для выхода.\n")

    while True:
        frame = shared["live_frame"] if sm.state == STATE_LIVE else shared["video_frame"]
        if frame is not None:
            cv2.imshow(win, frame)
        if cv2.waitKey(16) & 0xFF in (ord("q"), 27):
            break

    stop_event.set()
    heartbeat_event.set()
    cap_live.release()
    cv2.destroyAllWindows()
    print("Завершено.")


if __name__ == "__main__":
    main()
