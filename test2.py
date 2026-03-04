import cv2
import sys

VIDEO_PATH = "./public/0213.mp4"


def main():
    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        print(f"Не удалось открыть: {VIDEO_PATH}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    delay = int(1000 / fps)

    window_name = "Output Monitor"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    # Показать первый кадр — окно должно появиться прежде чем его двигать
    ret, frame = cap.read()
    if not ret:
        print("Не удалось прочитать видео")
        sys.exit(1)
    cv2.imshow(window_name, frame)
    cv2.waitKey(1)

    # Переместить окно на второй монитор (правее основного)
    # Если не работает — попробуй изменить X: это ширина твоего основного экрана
    screen_x_offset = 1440  # для MacBook 14" (2560/2 = 1280 в logical pixels, или 1440)
    cv2.moveWindow(window_name, screen_x_offset, 0)
    cv2.waitKey(200)  # подождать чтобы окно переместилось

    # Теперь делаем полноэкранный на том мониторе где оказалось окно
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    print(f"Воспроизведение на мониторе (X offset={screen_x_offset})")
    print("q / Esc — выход")
    print("Если видео на неправильном мониторе — запусти: python test2.py <X_offset>")
    print("Например: python test2.py 1920  или  python test2.py 2560")

    if len(sys.argv) > 1:
        x = int(sys.argv[1])
        cv2.moveWindow(window_name, x, 0)
        cv2.waitKey(200)
        cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    while True:
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue

        cv2.imshow(window_name, frame)

        key = cv2.waitKey(delay) & 0xFF
        if key in (ord('q'), 27):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
