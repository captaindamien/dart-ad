import cv2
import numpy as np

# Настройки
CAPTURE_CARD_INDEX = 0  # Индекс карты захвата (попробуйте 0, 1 или 2)
MARKER_PATH = '/Users/captaindamien/Desktop/github/dart-ad/public/marker.png'  # Путь к изображению-маркеру
MARKER_PATH1 = '/Users/captaindamien/Desktop/github/dart-ad/public/marker2.png'  # Путь к изображению-маркеру 2
RECORDED_VIDEO_PATH = '/Users/captaindamien/Desktop/github/dart-ad/public/0213.mp4'  # Видео на ПК №2
THRESHOLD = 0.8  # Порог точности совпадения (0.0 - 1.0)


def main():
    # 1. Захват потока с карты захвата (ПК №1)
    cap_live = cv2.VideoCapture(CAPTURE_CARD_INDEX)

    # 2. Загрузка записанного видео (ПК №2)
    cap_recorded = cv2.VideoCapture(RECORDED_VIDEO_PATH)

    # 3. Загрузка маркера
    marker = cv2.imread(MARKER_PATH, 0)
    if marker is None:
        print("Ошибка: Файл маркера не найден!")
        return
    w, h = marker.shape[::-1]
    marker1 = cv2.imread(MARKER_PATH1, 0)
    if marker is None:
        print("Ошибка: Файл маркера не найден!")
        return
    w, h = marker.shape[::-1]

    # Настройка полноэкранного режима на внешнем мониторе
    window_name = 'Output Monitor'
    cv2.namedWindow(window_name, cv2.WND_PROP_FULLSCREEN)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    print("Система запущена. Нажмите 'q' для выхода.")

    while True:
        # Читаем кадр с ПК №1
        ret_live, frame_live = cap_live.read()
        if not ret_live:
            break

        # Конвертируем в ч/б для поиска маркера
        gray_frame = cv2.cvtColor(frame_live, cv2.COLOR_BGR2GRAY)

        # Поиск маркера методом сопоставления шаблонов
        res = cv2.matchTemplate(gray_frame, marker, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= THRESHOLD)

        # Проверка: найден ли маркер?
        marker_found = len(loc[0]) > 0

        if marker_found:
            # Если маркер есть — выводим живое видео с ПК №1
            display_frame = frame_live
        else:
            # Если маркера нет — выводим записанное видео с ПК №2
            ret_rec, frame_rec = cap_recorded.read()

            # Если видео закончилось, запускаем его заново (зацикливание)
            if not ret_rec:
                cap_recorded.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret_rec, frame_rec = cap_recorded.read()

            display_frame = frame_rec

        # Вывод изображения на внешний монитор
        cv2.imshow(window_name, display_frame)

        # Выход по клавише 'q'
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap_live.release()
    cap_recorded.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
