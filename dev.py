"""
dev.py — локальная эмуляция без capture card и второго монитора.

Полностью использует пакет adplayer: синхронизация плейлиста с бэкендом,
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

from adplayer.state import StateManager, STATE_LIVE, STATE_VIDEO
from adplayer.api import sync_loop, heartbeat_loop, heartbeat_event, get_playlist
from adplayer.player import video_thread_fn
from adplayer.playback import sender_loop
from adplayer.config import ADS_DIR, MACHINE_TOKEN, SERVER_URL

# Опционально: путь к видеофайлу для имитации «живого» источника.
# None — генерируется синий фон с таймером.
LIVE_VIDEO_PATH = None


class MockMpvPlayer:
    """
    Мок MpvPlayer для локального dev-режима: повторяет публичный API
    adplayer.mpv_player.MpvPlayer, но вместо внешнего процесса mpv декодирует
    видео через OpenCV и пишет кадры в shared["video_frame"], которые dev.py
    рисует в общем cv2-окне.
    """

    def __init__(self, shared):
        self._shared      = shared
        self._lock        = threading.Lock()
        self._playlist    = []
        self._idx         = 0
        self._cap         = None
        self._current     = None
        self._is_paused   = True
        self._restart     = False
        self._stop_event  = threading.Event()
        self._thread      = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="mock-mpv")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None

    def set_playlist(self, paths):
        paths = list(paths)
        with self._lock:
            if paths == self._playlist:
                return
            self._playlist = paths
            self._idx      = 0
            self._open_current_locked()

    def play(self):
        self._is_paused = False

    def pause_and_hide(self):
        self._is_paused = True

    def restart_current(self):
        self._restart = True

    def current_filename(self):
        return self._current

    def playback_time(self):
        """Позиция в ролике — по ней учёт показов отличает повтор от продолжения."""
        with self._lock:
            if self._cap is None:
                return None
            pos_ms = self._cap.get(cv2.CAP_PROP_POS_MSEC)
        return pos_ms / 1000.0 if pos_ms and pos_ms > 0 else 0.0

    def duration(self):
        """Длина ролика — по ней учёт показов отличает досмотр от обрыва."""
        with self._lock:
            if self._cap is None:
                return None
            frames = self._cap.get(cv2.CAP_PROP_FRAME_COUNT)
            fps    = self._cap.get(cv2.CAP_PROP_FPS)
        if not frames or not fps or fps <= 0:
            return None
        return frames / fps

    @property
    def is_paused(self):
        return self._is_paused

    def _open_current_locked(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if not self._playlist:
            self._current = None
            return
        path = self._playlist[self._idx]
        # _current обнуляется только при неудаче: если сбрасывать его до
        # открытия файла, поток плеера успевает прочитать None на зацикливании
        # и оформляет лишний показ. Настоящий mpv так не делает.
        cap  = cv2.VideoCapture(path)
        if cap.isOpened():
            self._cap     = cap
            self._current = os.path.basename(path)
            print(f"[MOCK-MPV] open {self._current}")
        else:
            self._current = None
            print(f"[MOCK-MPV] не удалось открыть {path}")

    def _run(self):
        fps_delay = 1.0 / 30
        last_time = 0.0
        while not self._stop_event.is_set():
            now = time.time()
            if now - last_time < fps_delay:
                time.sleep(0.005)
                continue
            last_time = now

            with self._lock:
                if self._is_paused or self._cap is None:
                    continue

                if self._restart:
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    self._restart = False

                ret, frame = self._cap.read()
                if not ret:
                    if self._playlist:
                        self._idx = (self._idx + 1) % len(self._playlist)
                        self._open_current_locked()
                    continue

                self._shared["video_frame"] = frame


def on_state_change(old, new, duration):
    print(f"[STATE] {old} → {new}, duration={duration:.2f}s")
    heartbeat_event.set()


def _make_live_source():
    if LIVE_VIDEO_PATH and os.path.exists(LIVE_VIDEO_PATH):
        cap = cv2.VideoCapture(LIVE_VIDEO_PATH)
        if cap.isOpened():
            return cap
    return None


def _generate_live_frame(t):
    frame = np.zeros((480, 854, 3), dtype=np.uint8)
    frame[:] = (80, 40, 20)
    secs = int(t) % 60
    cv2.putText(frame, f"LIVE SOURCE  {secs:02d}s", (30, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 255), 2, cv2.LINE_AA)
    return frame


def live_thread_fn(cap_live, shared, stop_event):
    t0        = time.time()
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
            shared["live_frame"] = _generate_live_frame(time.time() - t0)


def _draw_hud(frame, sm, shared):
    h, w = frame.shape[:2]
    overlay = frame.copy()

    state_label = "LIVE" if sm.state == STATE_LIVE else "VIDEO (AD)"
    state_color = (0, 200, 80) if sm.state == STATE_LIVE else (0, 80, 220)
    elapsed = sm.time_in_state()

    cv2.rectangle(overlay, (0, 0), (w, 54), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    video_name = shared.get("current_video") or ""
    video_info = f"   [{video_name}]" if sm.state == STATE_VIDEO and video_name else ""
    pl_count   = len(get_playlist())

    cv2.putText(frame,
                f"[DEV]  {state_label}   {elapsed:.1f}s   trans:{sm.transitions}   pl:{pl_count}{video_info}",
                (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.7, state_color, 2, cv2.LINE_AA)
    cv2.putText(frame, "1=AD  2=LIVE  r=reset  q=quit",
                (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1, cv2.LINE_AA)
    return frame


def main():
    print("=== dart-ad DEV MODE ===")
    print(f"  SERVER_URL   = {SERVER_URL}")
    print(f"  ADS_DIR      = {ADS_DIR}")
    print(f"  MACHINE_TOKEN= {'✓ set' if MACHINE_TOKEN else '✗ NOT SET — sync/heartbeat disabled'}")
    print()

    cap_live   = _make_live_source()
    sm         = StateManager(on_change_callback=on_state_change)
    shared     = {"live_frame": None, "video_frame": None, "video_restart": False, "current_video": None}
    mpv        = MockMpvPlayer(shared)
    shared["mpv"] = mpv
    mpv.start()
    stop_event = threading.Event()

    threads = [
        threading.Thread(target=sync_loop,       args=(stop_event,),                  daemon=True, name="sync"),
        threading.Thread(target=heartbeat_loop,  args=(shared, stop_event, sm),        daemon=True, name="heartbeat"),
        threading.Thread(target=live_thread_fn,  args=(cap_live, shared, stop_event),  daemon=True, name="live"),
        threading.Thread(target=video_thread_fn, args=(shared, stop_event, sm),        daemon=True, name="video"),
        # Без этого потока показы копились бы в файле очереди и никогда не уходили.
        threading.Thread(target=sender_loop,     args=(stop_event,),                  daemon=True, name="playback"),
    ]
    for t in threads:
        t.start()

    win = "dart-ad  [DEV MODE]"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 854, 480)

    print("Управление: 1=AD  2=LIVE  r=reset  q/Esc=quit")
    print("Ждём первой синхронизации плейлиста…")

    while True:
        frame = shared.get("live_frame") if sm.state == STATE_LIVE else shared.get("video_frame")

        if frame is not None:
            display = _draw_hud(frame.copy(), sm, shared)
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
    heartbeat_event.set()
    mpv.stop()
    if cap_live is not None:
        cap_live.release()
    cv2.destroyAllWindows()
    print("Завершено.")


if __name__ == "__main__":
    main()
