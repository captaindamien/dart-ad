import cv2
import sys

def find_capture_device():
    """Найти карту видеозахвата среди доступных устройств."""
    for index in range(10):
        cap = cv2.VideoCapture(index)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                print(f"Найдено устройство: {index}")
                return cap, index
            cap.release()
    return None, -1

def main():
    device_index = int(sys.argv[1]) if len(sys.argv) > 1 else None

    if device_index is not None:
        cap = cv2.VideoCapture(device_index)
    else:
        cap, device_index = find_capture_device()

    if cap is None or not cap.isOpened():
        print("Не удалось открыть устройство захвата видео.")
        print("Укажите индекс вручную: python test.py <индекс>")
        sys.exit(1)

    # Установить разрешение 1080p если поддерживается
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Захват с устройства {device_index}: {w}x{h}")
    print("Нажмите 'q' или Esc для выхода, 'f' для полноэкранного режима.")

    cv2.namedWindow("Capture", cv2.WINDOW_NORMAL)
    fullscreen = False

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Нет сигнала...")
            continue

        cv2.imshow("Capture", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), 27):  # q или Esc
            break
        elif key == ord('f'):
            fullscreen = not fullscreen
            if fullscreen:
                cv2.setWindowProperty("Capture", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
            else:
                cv2.setWindowProperty("Capture", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
