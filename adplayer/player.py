import os
import time

from .api import get_playlist
from .state import STATE_VIDEO


def video_thread_fn(shared, stop_event, sm):
    mpv = shared["mpv"]
    last_playlist = []

    while not stop_event.is_set():
        current_playlist = get_playlist()

        if sm.state != STATE_VIDEO:
            if not mpv.is_paused:
                mpv.pause_and_hide()
            shared["current_video"] = None
            last_playlist = []
            time.sleep(0.1)
            continue

        if not current_playlist:
            time.sleep(0.5)
            continue

        if current_playlist != last_playlist:
            mpv.set_playlist(current_playlist)
            last_playlist = current_playlist
            print(f"[VIDEO] New playlist: {[os.path.basename(p) for p in current_playlist]}")

        if shared.get("video_restart"):
            mpv.restart_current()
            shared["video_restart"] = False

        if mpv.is_paused:
            mpv.play()

        fname = mpv.current_filename()
        if fname:
            shared["current_video"] = fname

        time.sleep(0.5)
