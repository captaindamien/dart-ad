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

BASE = os.path.join(os.path.dirname(__file__), "public")
MARKER1_PATH = os.path.join(BASE, "marker.png")
MARKER2_PATH = os.path.join(BASE, "marker2.png")
VIDEO_PATH   = os.path.join(BASE, "0213.mp4")

THRESHOLD        = 0.75  # Порог совпадения маркера (0–1)
DEBOUNCE_FRAMES  = 3     # Сколько кадров подряд должен быть виден маркер

# X-смещение второго монитора (можно передать аргументом)
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


def marker_found(gray_frame, marker, threshold):
    """Возвращает True если маркер найден в кадре."""
    if marker is None or gray_frame is None:
        return False
    if (gray_frame.shape[0] < marker.shape[0] or
            gray_frame.shape[1] < marker.shape[1]):
        return False
    res = cv2.matchTemplate(gray_frame, marker, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return max_val >= threshold


def main():
    # --- Загрузка маркеров ---
    marker1 = cv2.imread(MARKER1_PATH, cv2.IMREAD_GRAYSCALE)
    marker2 = cv2.imread(MARKER2_PATH, cv2.IMREAD_GRAYSCALE)
    if marker1 is None:
        print(f"Ошибка: не найден маркер {MARKER1_PATH}")
        sys.exit(1)
    if marker2 is None:
        print(f"Ошибка: не найден маркер {MARKER2_PATH}")
        sys.exit(1)
    print(f"Маркер 1: {marker1.shape[1]}x{marker1.shape[0]} px")
    print(f"Маркер 2: {marker2.shape[1]}x{marker2.shape[0]} px")

    # --- Карта захвата ---
    print("Поиск устройства захвата...")
    cap_live, dev_idx = find_capture_device(skip_first=False)
    if cap_live is None:
        print("Ошибка: карта захвата не найдена. Укажите индекс: python full_test.py <x_offset> <device_index>")
        sys.exit(1)
    cap_live.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap_live.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

    # --- Видеофайл ---
    cap_video = cv2.VideoCapture(VIDEO_PATH)
    if not cap_video.isOpened():
        print(f"Ошибка: не удалось открыть {VIDEO_PATH}")
        sys.exit(1)
    video_fps   = cap_video.get(cv2.CAP_PROP_FPS) or 30
    frame_delay = 1.0 / video_fps
    print(f"Видео: {VIDEO_PATH}, FPS={video_fps:.1f}")

    # --- Окно на втором мониторе ---
    win = "AD Display"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    # Показываем пустой кадр чтобы окно появилось, затем перемещаем
    blank = np.zeros((1080, 1920, 3), dtype=np.uint8)
    cv2.imshow(win, blank)
    cv2.waitKey(1)
    cv2.moveWindow(win, MONITOR_X_OFFSET, 0)
    cv2.waitKey(200)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    print(f"\nЗапущено. Монитор X={MONITOR_X_OFFSET}. Нажмите 'q' для выхода.\n")

    state              = STATE_LIVE
    current_video_frame = None
    last_video_time    = 0.0
    debounce_count     = 0       # счётчик последовательных обнаружений маркера

    while True:
        ret_live, frame_live = cap_live.read()
        if not ret_live:
            print("Нет сигнала с карты захвата...")
            time.sleep(0.05)
            continue

        gray_live = cv2.cvtColor(frame_live, cv2.COLOR_BGR2GRAY)

        # ── Состояние LIVE: показываем живое видео, ждём marker1 ──────────────
        if state == STATE_LIVE:
            if marker_found(gray_live, marker1, THRESHOLD):
                debounce_count += 1
                if debounce_count >= DEBOUNCE_FRAMES:
                    print("marker.png найден → воспроизведение 0213.mp4")
                    state = STATE_VIDEO
                    debounce_count = 0
                    cap_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    last_video_time = 0.0
                    current_video_frame = None
            else:
                debounce_count = 0

            display_frame = frame_live

        # ── Состояние VIDEO: показываем 0213.mp4, ждём marker2 ───────────────
        else:
            if marker_found(gray_live, marker2, THRESHOLD):
                debounce_count += 1
                if debounce_count >= DEBOUNCE_FRAMES:
                    print("marker2.png найден → возврат к живому видео")
                    state = STATE_LIVE
                    debounce_count = 0
            else:
                debounce_count = 0

            # Читаем следующий кадр видео в нужный момент времени
            now = time.time()
            if now - last_video_time >= frame_delay:
                ret_vid, frame_vid = cap_video.read()
                if not ret_vid:                   # конец видео — зацикливаем
                    cap_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret_vid, frame_vid = cap_video.read()
                if ret_vid:
                    current_video_frame = frame_vid
                last_video_time = now

            display_frame = current_video_frame if current_video_frame is not None else frame_live

        cv2.imshow(win, display_frame)
        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
            break

    cap_live.release()
    cap_video.release()
    cv2.destroyAllWindows()
    print("Завершено.")


if __name__ == "__main__":
    main()
