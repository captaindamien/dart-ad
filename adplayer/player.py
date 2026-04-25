import os
import time

import cv2

from .api import get_playlist
from .state import STATE_VIDEO


def video_thread_fn(shared, stop_event, sm):
    idx           = 0
    cap           = None
    video_fps     = 30
    frame_delay   = 1.0 / video_fps
    last_time     = 0.0
    last_playlist = []

    while not stop_event.is_set():
        current_playlist = get_playlist()

        if sm.state != STATE_VIDEO:
            if cap is not None:
                cap.release()
                cap = None
            time.sleep(0.02)
            last_time = 0.0
            continue

        if not current_playlist:
            time.sleep(0.5)
            continue

        if current_playlist != last_playlist:
            if cap is not None:
                cap.release()
                cap = None
            idx = 0
            last_time = 0.0
            last_playlist = current_playlist
            print(f"[VIDEO] New playlist: {[os.path.basename(p) for p in current_playlist]}")

        if cap is None:
            if idx >= len(current_playlist):
                idx = 0
            cap = cv2.VideoCapture(current_playlist[idx])
            video_fps   = cap.get(cv2.CAP_PROP_FPS) or 30
            frame_delay = 1.0 / video_fps
            last_time   = 0.0
            shared["current_video"] = os.path.basename(current_playlist[idx])

        if shared.get("video_restart"):
            cap.release()
            cap = None
            last_time = 0.0
            shared["video_restart"] = False
            continue

        now = time.time()
        if now - last_time < frame_delay:
            time.sleep(0.005)
            continue

        ret, frame = cap.read()
        if not ret:
            cap.release()
            cap = None
            idx = (idx + 1) % len(current_playlist)
            last_time = 0.0
            continue

        shared["video_frame"] = frame
        last_time = now

    if cap is not None:
        cap.release()
