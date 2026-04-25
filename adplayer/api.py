import os
import json
import shutil
import threading
import urllib.request

from .config import MACHINE_TOKEN, SERVER_URL, ADS_DIR, SYNC_INTERVAL, HEARTBEAT_INTERVAL
from .metrics import get_system_metrics
from .state import STATE_VIDEO

_playlist_lock   = threading.Lock()
_server_playlist = []
heartbeat_event  = threading.Event()


def get_playlist():
    with _playlist_lock:
        return list(_server_playlist)


def _download_file(url, dest_path):
    req = urllib.request.Request(url, headers={"X-Machine-Token": MACHINE_TOKEN})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(resp, f)


def _sync_once():
    global _server_playlist
    req = urllib.request.Request(
        f"{SERVER_URL}/api/display/playlist",
        headers={"X-Machine-Token": MACHINE_TOKEN},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    items = data.get("data", {}).get("playlist", [])
    items.sort(key=lambda x: x.get("display_order", 0))

    os.makedirs(ADS_DIR, exist_ok=True)
    needed = {item["filename"] for item in items}

    for item in items:
        fname = item["filename"]
        fpath = os.path.join(ADS_DIR, fname)
        if not os.path.exists(fpath):
            url = item["download_url"]
            print(f"[SYNC] Downloading {fname} from {url}…")
            tmp = fpath + ".tmp"
            try:
                _download_file(url, tmp)
                os.replace(tmp, fpath)
                print(f"[SYNC] OK {fname}")
            except Exception as e:
                print(f"[SYNC] Failed {fname}: {type(e).__name__}: {e}")
                if os.path.exists(tmp):
                    os.remove(tmp)

    video_exts = ('.mp4', '.avi', '.mkv', '.mov')
    existing = {f for f in os.listdir(ADS_DIR) if f.lower().endswith(video_exts)}
    for fname in existing - needed:
        os.remove(os.path.join(ADS_DIR, fname))
        print(f"[SYNC] Removed {fname}")

    new_playlist = [
        os.path.join(ADS_DIR, item["filename"])
        for item in items
        if os.path.exists(os.path.join(ADS_DIR, item["filename"]))
    ]
    with _playlist_lock:
        _server_playlist = new_playlist
    print(f"[SYNC] Playlist ({len(new_playlist)}): {[os.path.basename(p) for p in new_playlist]}")


def sync_loop(stop_event):
    while not stop_event.is_set():
        try:
            _sync_once()
        except Exception as e:
            print(f"[SYNC] error: {type(e).__name__}: {e}")
        print(f"[SYNC] next sync in {SYNC_INTERVAL}s")
        stop_event.wait(timeout=SYNC_INTERVAL)


def _do_send_heartbeat(state, current_video):
    if not MACHINE_TOKEN:
        return
    try:
        payload = {"state": state, "current_video": current_video}
        payload.update(get_system_metrics())
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{SERVER_URL}/api/display/heartbeat",
            data=body,
            headers={"X-Machine-Token": MACHINE_TOKEN, "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        print(f"[HB] state={state}, video={current_video}")
    except Exception as e:
        print(f"[HB] error: {e}")


def heartbeat_loop(shared, stop_event, sm):
    while not stop_event.is_set():
        heartbeat_event.wait(timeout=HEARTBEAT_INTERVAL)
        heartbeat_event.clear()
        if stop_event.is_set():
            break
        state = "playing" if sm.state == STATE_VIDEO else "idle"
        current_video = shared.get("current_video") if sm.state == STATE_VIDEO else None
        threading.Thread(target=_do_send_heartbeat, args=(state, current_video), daemon=True).start()
