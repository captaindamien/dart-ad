"""
prod.py — ILSport Dart Ad Player

Логика:
  LIVE  → обнаружен marker.png  → воспроизводит рекламные видео из плейлиста бэкенда
  VIDEO → обнаружен marker2.png → возвращается к живому видео

Переменные окружения (из /etc/ilsport/env):
  SERVER_URL          — URL бэкенда (напр. https://your-server.com)
  MACHINE_TOKEN       — токен машины (X-Machine-Token)
  ADS_DIR             — папка для видео (по умолчанию /opt/ilsport/ads)
  SYNC_INTERVAL       — интервал синхронизации плейлиста в секундах (по умолчанию 300)
  HEARTBEAT_INTERVAL  — интервал хартбита в секундах (по умолчанию 30)

Запуск:
  python prod.py [X_offset]
  X_offset — горизонтальное смещение второго монитора (по умолчанию 1440)
"""

import cv2
import numpy as np
import time
import sys
import os
import threading
import subprocess
import shutil
import socket
import json
import urllib.request

from core import StateManager, STATE_LIVE, STATE_VIDEO

# ── Конфигурация ────────────────────────────────────────────────────────────
MACHINE_TOKEN      = os.environ.get("MACHINE_TOKEN", "")
SERVER_URL         = os.environ.get("SERVER_URL", "http://localhost:3000").rstrip("/")
ADS_DIR            = os.environ.get("ADS_DIR", os.path.join(os.path.dirname(__file__), "public", "ads"))
SYNC_INTERVAL      = int(os.environ.get("SYNC_INTERVAL", "300"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
MONITOR_X_OFFSET   = int(sys.argv[1]) if len(sys.argv) > 1 else 1440

BASE         = os.path.join(os.path.dirname(__file__), "public")
MARKER1_PATH = os.path.join(BASE, "marker.png")
MARKER2_PATH = os.path.join(BASE, "marker2.png")

THRESHOLD       = 0.75
DEBOUNCE_FRAMES = 3
DETECT_SCALE    = 0.25
DETECT_EVERY_N  = 3

# ── Общее состояние ──────────────────────────────────────────────────────────
playlist_lock    = threading.Lock()
server_playlist  = []   # список абсолютных путей, отсортированных по display_order
heartbeat_event  = threading.Event()


# ── Системные метрики ────────────────────────────────────────────────────────
def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""

def get_system_metrics():
    metrics = {}
    # Uptime
    uptime_raw = _read_file("/proc/uptime").split()
    if uptime_raw:
        metrics["uptime_seconds"] = int(float(uptime_raw[0]))
    # RAM
    for line in _read_file("/proc/meminfo").splitlines():
        parts = line.split()
        if parts[0] == "MemTotal:":
            metrics["ram_total_mb"] = int(parts[1]) // 1024
        elif parts[0] == "MemAvailable:":
            metrics["ram_used_mb"] = metrics.get("ram_total_mb", 0) - int(parts[1]) // 1024
    # Disk
    try:
        st = os.statvfs("/")
        metrics["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
        metrics["disk_used_gb"]  = round((st.f_blocks - st.f_bfree) * st.f_frsize / 1e9, 1)
    except OSError:
        pass
    # Local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        metrics["local_ip"] = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    # CPU temp (Pi-specific)
    temp_raw = _read_file("/sys/class/thermal/thermal_zone0/temp").strip()
    if temp_raw:
        metrics["cpu_temp"] = round(int(temp_raw) / 1000, 1)
    return metrics


# ── Хартбит ──────────────────────────────────────────────────────────────────
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


# ── Синхронизация плейлиста ──────────────────────────────────────────────────
def _download_file(url, dest_path):
    req = urllib.request.Request(url, headers={"X-Machine-Token": MACHINE_TOKEN})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(resp, f)

def _sync_once():
    global server_playlist
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

    # Скачиваем недостающие файлы
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

    # Удаляем лишние файлы
    video_exts = ('.mp4', '.avi', '.mkv', '.mov')
    existing = {f for f in os.listdir(ADS_DIR) if f.lower().endswith(video_exts)}
    for fname in existing - needed:
        os.remove(os.path.join(ADS_DIR, fname))
        print(f"[SYNC] Removed {fname}")

    # Обновляем плейлист
    new_playlist = [
        os.path.join(ADS_DIR, item["filename"])
        for item in items
        if os.path.exists(os.path.join(ADS_DIR, item["filename"]))
    ]
    with playlist_lock:
        server_playlist = new_playlist
    print(f"[SYNC] Playlist ({len(new_playlist)}): {[os.path.basename(p) for p in new_playlist]}")

def sync_loop(stop_event):
    while not stop_event.is_set():
        try:
            _sync_once()
        except Exception as e:
            print(f"[SYNC] error: {type(e).__name__}: {e}")
        print(f"[SYNC] next sync in {SYNC_INTERVAL}s")
        stop_event.wait(timeout=SYNC_INTERVAL)


# ── Маркеры / захват ─────────────────────────────────────────────────────────
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

def marker_found(small_gray, marker_small, threshold):
    if marker_small is None or small_gray is None:
        return False
    if (small_gray.shape[0] < marker_small.shape[0] or
            small_gray.shape[1] < marker_small.shape[1]):
        return False
    res = cv2.matchTemplate(small_gray, marker_small, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return max_val >= threshold


# ── Поток воспроизведения видео ──────────────────────────────────────────────
def video_thread_fn(shared, stop_event, sm):
    idx         = 0
    cap         = None
    video_fps   = 30
    frame_delay = 1.0 / video_fps
    last_time   = 0.0
    last_playlist = []

    while not stop_event.is_set():
        with playlist_lock:
            current_playlist = list(server_playlist)

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

        # При смене плейлиста — сброс
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


# ── Поток захвата с карты ────────────────────────────────────────────────────
def capture_thread_fn(cap_live, marker1_small, marker2_small, shared, stop_event, sm):
    frame_count   = 0
    debounce_count = 0

    while not stop_event.is_set():
        ret, frame = cap_live.read()
        if not ret:
            time.sleep(0.01)
            continue

        shared["live_frame"] = frame
        frame_count += 1

        if frame_count % DETECT_EVERY_N != 0:
            continue

        small      = cv2.resize(frame, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if sm.state == STATE_LIVE:
            if marker_found(gray_small, marker1_small, THRESHOLD):
                debounce_count += 1
                if debounce_count >= DEBOUNCE_FRAMES:
                    sm.transition(STATE_VIDEO)
                    shared["video_restart"] = True
                    debounce_count = 0
            else:
                debounce_count = 0
        else:
            if marker_found(gray_small, marker2_small, THRESHOLD):
                debounce_count += 1
                if debounce_count >= DEBOUNCE_FRAMES:
                    sm.transition(STATE_LIVE)
                    debounce_count = 0
            else:
                debounce_count = 0


# ── Колбэк смены состояния ───────────────────────────────────────────────────
def on_state_change(old, new, duration):
    print(f"[STATE] {old} → {new}, duration={duration:.2f}s")
    heartbeat_event.set()  # немедленный хартбит при смене состояния


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    try:
        subprocess.Popen(['unclutter', '-idle', '0', '-root'])
    except FileNotFoundError:
        pass

    marker1 = cv2.imread(MARKER1_PATH, cv2.IMREAD_GRAYSCALE)
    marker2 = cv2.imread(MARKER2_PATH, cv2.IMREAD_GRAYSCALE)
    if marker1 is None:
        print(f"Ошибка: не найден маркер {MARKER1_PATH}"); sys.exit(1)
    if marker2 is None:
        print(f"Ошибка: не найден маркер {MARKER2_PATH}"); sys.exit(1)

    marker1_small = cv2.resize(marker1, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
    marker2_small = cv2.resize(marker2, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)

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

    sm     = StateManager(on_change_callback=on_state_change)
    shared = {"live_frame": None, "video_frame": None, "video_restart": False, "current_video": None}
    stop_event = threading.Event()

    threads = [
        threading.Thread(target=sync_loop,         args=(stop_event,),                                        daemon=True, name="sync"),
        threading.Thread(target=heartbeat_loop,    args=(shared, stop_event, sm),                             daemon=True, name="heartbeat"),
        threading.Thread(target=capture_thread_fn, args=(cap_live, marker1_small, marker2_small, shared, stop_event, sm), daemon=True, name="capture"),
        threading.Thread(target=video_thread_fn,   args=(shared, stop_event, sm),                             daemon=True, name="video"),
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
