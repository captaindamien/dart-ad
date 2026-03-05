"""
full_test.py — Система рекламного дисплея

Логика:
  LIVE  → обнаружен marker.png  → воспроизводит 0213.mp4
  VIDEO → обнаружен marker2.png → возвращается к живому видео

Запуск:
  python full_test.py [X_offset]
  X_offset — горизонтальное смещение второго монитора (по умолчанию 1440)
  Пример: python full_test.py 1920
"""

import cv2
import numpy as np
import time
import sys
import os
import threading
import subprocess

BASE = os.path.join(os.path.dirname(__file__), "public")
MARKER1_PATH = os.path.join(BASE, "marker.png")
MARKER2_PATH = os.path.join(BASE, "marker2.png")
VIDEO_PATH   = os.path.join(BASE, "0213.mp4")

THRESHOLD       = 0.75  # Порог совпадения маркера (0–1)
DEBOUNCE_FRAMES = 3     # Сколько кадров подряд должен быть виден маркер
DETECT_SCALE    = 0.25  # Масштаб кадра для template matching (1920→480, 1080→270)
DETECT_EVERY_N  = 3     # Проверять маркер раз в N кадров захвата

MONITOR_X_OFFSET = int(sys.argv[1]) if len(sys.argv) > 1 else 1440

STATE_LIVE  = "live"
STATE_VIDEO = "video"


def find_capture_device(skip_first: bool = False):
    """Ищет карту видеозахвата. skip_first=True — пропускает индекс 0 (встроенная камера)."""
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
    """Возвращает True если маркер найден в уменьшенном кадре."""
    if marker_small is None or small_gray is None:
        return False
    if (small_gray.shape[0] < marker_small.shape[0] or
            small_gray.shape[1] < marker_small.shape[1]):
        return False
    res = cv2.matchTemplate(small_gray, marker_small, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return max_val >= threshold


def video_thread_fn(cap_video, shared, stop_event):
    """
    Поток декодирования видео: читает кадры из файла и кладёт их в shared["video_frame"].
    Работает независимо от главного потока — если read() зависнет, display не остановится.
    """
    video_fps   = cap_video.get(cv2.CAP_PROP_FPS) or 30
    frame_delay = 1.0 / video_fps
    last_time   = 0.0

    while not stop_event.is_set():
        state = shared["state"]

        if state != STATE_VIDEO:
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
        if not ret:  # конец видео — зацикливаем
            cap_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = cap_video.read()
        if ret:
            shared["video_frame"] = frame
        last_time = now


def capture_thread_fn(cap_live, marker1_small, marker2_small, shared, stop_event):
    """
    Поток захвата: читает кадры с карты захвата, обнаруживает маркеры.
    Не блокирует главный поток — display и video работают независимо.
    """
    frame_count = 0
    debounce_count = 0

    while not stop_event.is_set():
        ret, frame = cap_live.read()
        if not ret:
            time.sleep(0.01)
            continue

        # Сохраняем последний живой кадр для отображения
        shared["live_frame"] = frame
        frame_count += 1

        # Template matching только раз в DETECT_EVERY_N кадров
        if frame_count % DETECT_EVERY_N != 0:
            continue

        small = cv2.resize(frame, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        state = shared["state"]

        if state == STATE_LIVE:
            if marker_found(gray_small, marker1_small, THRESHOLD):
                debounce_count += 1
                if debounce_count >= DEBOUNCE_FRAMES:
                    print("marker.png найден → воспроизведение 0213.mp4")
                    shared["state"] = STATE_VIDEO
                    shared["video_restart"] = True
                    debounce_count = 0
            else:
                debounce_count = 0
        else:  # STATE_VIDEO
            if marker_found(gray_small, marker2_small, THRESHOLD):
                debounce_count += 1
                if debounce_count >= DEBOUNCE_FRAMES:
                    print("marker2.png найден → возврат к живому видео")
                    shared["state"] = STATE_LIVE
                    debounce_count = 0
            else:
                debounce_count = 0


def main():
    # Скрываем курсор мыши
    try:
        subprocess.Popen(['unclutter', '-idle', '0', '-root'])
    except FileNotFoundError:
        print("Предупреждение: unclutter не установлен. Установите: sudo apt install unclutter")

    # --- Загрузка маркеров ---
    marker1 = cv2.imread(MARKER1_PATH, cv2.IMREAD_GRAYSCALE)
    marker2 = cv2.imread(MARKER2_PATH, cv2.IMREAD_GRAYSCALE)
    if marker1 is None:
        print(f"Ошибка: не найден маркер {MARKER1_PATH}")
        sys.exit(1)
    if marker2 is None:
        print(f"Ошибка: не найден маркер {MARKER2_PATH}")
        sys.exit(1)

    # Уменьшаем маркеры пропорционально DETECT_SCALE
    marker1_small = cv2.resize(marker1, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
    marker2_small = cv2.resize(marker2, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
    print(f"Маркер 1: {marker1.shape[1]}x{marker1.shape[0]} → {marker1_small.shape[1]}x{marker1_small.shape[0]} px")
    print(f"Маркер 2: {marker2.shape[1]}x{marker2.shape[0]} → {marker2_small.shape[1]}x{marker2_small.shape[0]} px")

    # --- Карта захвата ---
    print("Поиск устройства захвата...")
    cap_live, dev_idx = find_capture_device(skip_first=False)
    if cap_live is None:
        print("Ошибка: карта захвата не найдена.")
        sys.exit(1)
    cap_live.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap_live.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap_live.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # минимальный буфер → всегда свежий кадр

    # --- Видеофайл ---
    cap_video = cv2.VideoCapture(VIDEO_PATH)
    if not cap_video.isOpened():
        print(f"Ошибка: не удалось открыть {VIDEO_PATH}")
        sys.exit(1)
    video_fps = cap_video.get(cv2.CAP_PROP_FPS) or 30
    print(f"Видео: {VIDEO_PATH}, FPS={video_fps:.1f}")

    # --- Окно на втором мониторе ---
    win = "AD Display"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    blank = np.zeros((1080, 1920, 3), dtype=np.uint8)
    cv2.imshow(win, blank)
    cv2.waitKey(1)
    cv2.moveWindow(win, MONITOR_X_OFFSET, 0)
    cv2.waitKey(200)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    print(f"\nЗапущено. Монитор X={MONITOR_X_OFFSET}. Нажмите 'q' для выхода.\n")

    # --- Общее состояние между потоками ---
    shared = {
        "live_frame":    None,
        "video_frame":   None,
        "state":         STATE_LIVE,
        "video_restart": False,
    }

    stop_event = threading.Event()

    t_capture = threading.Thread(
        target=capture_thread_fn,
        args=(cap_live, marker1_small, marker2_small, shared, stop_event),
        daemon=True,
    )
    t_video = threading.Thread(
        target=video_thread_fn,
        args=(cap_video, shared, stop_event),
        daemon=True,
    )
    t_capture.start()
    t_video.start()

    # --- Главный цикл: только display ---
    while True:
        state = shared["state"]

        if state == STATE_LIVE:
            frame = shared["live_frame"]
            if frame is not None:
                cv2.imshow(win, frame)
        else:  # STATE_VIDEO
            frame = shared["video_frame"]
            if frame is not None:
                cv2.imshow(win, frame)

        if cv2.waitKey(16) & 0xFF in (ord("q"), 27):  # ~60 Hz отображение
            break

    stop_event.set()
    cap_live.release()
    cap_video.release()
    cv2.destroyAllWindows()
    print("Завершено.")


if __name__ == "__main__":
    main()
