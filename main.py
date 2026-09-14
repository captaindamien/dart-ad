"""
ILSport Dart Ad Player — точка входа для продакшена.

Логика:
  LIVE  → обнаружен marker.png  → воспроизводит рекламные видео из плейлиста
  VIDEO → обнаружен marker2.png → возвращается к живому видео

Рекламные видео декодируются и выводятся внешним процессом mpv с HW-ускорением
(V4L2 M2M на Raspberry Pi 4). Live-картинка с карты захвата по-прежнему
рендерится через cv2.imshow. mpv-окно с --ontop перекрывает cv2-окно в STATE_VIDEO.

Помимо картинки агент отдаёт на сервер телеметрию (heartbeat) и статистику
показов рекламы. Показы копятся в файле на диске и досылаются пачками, поэтому
обрыв связи или перезагрузка их не теряют.

Порядок запуска намеренно таков: сначала heartbeat и синхронизация плейлиста,
и только потом железо. Агент обязан быть виден в дэшборде даже тогда, когда
карта захвата не подключена, — см. _wait_for_capture().

Переменные окружения (из /etc/ilsport/env):
  SERVER_URL              — URL бэкенда (напр. https://your-server.com)
  MACHINE_TOKEN           — токен машины (X-Machine-Token)
  ADS_DIR                 — папка для видео (по умолчанию ./public/ads)
  SYNC_INTERVAL           — интервал синхронизации плейлиста, с (по умолчанию 300)
  HEARTBEAT_INTERVAL      — интервал хартбита, с (по умолчанию 15). Хартбит
                            уходит и вне графика: при смене состояния, при
                            смене текущего ролика и при отказе оборудования
  CAPTURE_RETRY_SEC       — пауза между попытками найти карту захвата, с (10)
  CAPTURE_STALL_SEC       — сколько секунд без кадров считать отвалом карты (15)
  PLAYBACK_QUEUE_PATH     — файл очереди показов (~/.cache/ilsport/playback_queue.jsonl)
  PLAYBACK_FLUSH_INTERVAL — как часто досылать накопленные показы, с (60)
  PLAYBACK_BATCH_SIZE     — размер пачки (100, сервер принимает до 200)
  PLAYBACK_QUEUE_MAX      — потолок очереди в событиях (20000)
  PLAYBACK_MIN_SEC        — показ короче этого не засчитывается (1.0)

Запуск:
  python main.py [X_offset]
  X_offset — горизонтальное смещение второго монитора (по умолчанию 1440)
"""

import signal
import sys
import time
import threading
import subprocess

import os
# До первого import cv2: варнинги videoio читаются из окружения при загрузке
# библиотеки, cv2.utils.logging на OpenCV 4.6 (Bookworm) их не глушит.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import cv2
import numpy as np

from adplayer.config import MARKER1_PATH, MARKER2_PATH, ADS_DIR, SERVER_URL, CAPTURE_RETRY_SEC
from adplayer.state import (
    StateManager, STATE_LIVE, STATE_VIDEO, FAULT_NO_CAPTURE, FAULT_NO_MARKERS,
)
from adplayer.api import sync_loop, heartbeat_loop, heartbeat_event
from adplayer.capture import (
    find_capture_device, load_markers, capture_thread_fn, reopen_capture,
)
from adplayer.player import video_thread_fn
from adplayer.playback import sender_loop, flush_once
from adplayer.mpv_player import MpvPlayer

MONITOR_X_OFFSET = int(sys.argv[1]) if len(sys.argv) > 1 else 1440

# update.sh обновляет агента через `pkill -f "python3 .*main.py"`, то есть
# SIGTERM. По умолчанию Python на нём завершается немедленно, минуя finally, —
# mpv.stop() не вызывался и внешний плеер оставался осиротевшим fullscreen-окном
# поверх экрана, которого новый агент уже не контролирует.
_shutdown = threading.Event()


def _on_signal(signum, _frame):
    print(f"[MAIN] сигнал {signum} — завершаюсь")
    _shutdown.set()


def on_state_change(old, new, duration):
    print(f"[STATE] {old} → {new}, duration={duration:.2f}s")
    heartbeat_event.set()


def _set_fault(shared, fault, detail=None):
    """Смена неисправности всегда дёргает внеплановый хартбит — дэшборд не должен
    ждать до минуты, чтобы узнать, что автомат остался без карты захвата."""
    if shared.get("fault") != fault:
        shared["fault"] = fault
        shared["fault_detail"] = detail
        heartbeat_event.set()


def _wait_for_capture(shared, stop_event):
    """
    Ждём карту захвата вместо sys.exit(1).

    Раньше её отсутствие убивало процесс, kiosk-autostart поднимал его заново
    через 5 секунд, и так бесконечно: поток хартбита не успевал отправить ни
    одного запроса. В дэшборде включённая, но не подключённая к автомату Pi
    выглядела ровно как выключенная — отличить «стоит на столе» от «сгорела»
    было нечем. Теперь процесс живёт, репортит state=error и сам подхватывает
    карту, как только её воткнут: перезагрузка после монтажа не нужна.

    Возвращает None только при завершении работы.
    """
    announced = False
    while not _shutdown.is_set() and not stop_event.is_set():
        cap, _ = find_capture_device(skip_first=False)
        if cap is not None:
            _set_fault(shared, None)
            return cap
        if not announced:
            announced = True
            _set_fault(shared, FAULT_NO_CAPTURE, "устройство /dev/video* с картинкой не найдено")
            print(f"[CAPTURE] карта захвата не найдена — жду, повтор каждые "
                  f"{CAPTURE_RETRY_SEC:.0f}s (в дэшборде машина видна как error)")
        _shutdown.wait(CAPTURE_RETRY_SEC)
    return None


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT,  _on_signal)

    sm         = StateManager(on_change_callback=on_state_change)
    shared     = {"live_frame": None, "video_restart": False, "current_video": None,
                  "mpv": None, "cap": None, "fault": None, "fault_detail": None}
    stop_event = threading.Event()

    # Инфраструктурные потоки поднимаются до любого железа:
    #   heartbeat — чтобы машина была видна в дэшборде даже без карты захвата;
    #   sync      — чтобы ролики успели скачаться, пока Pi ждёт подключения
    #               к автомату. Иначе первый же marker после установки уводил
    #               в рекламу с ещё пустым плейлистом, и экран замирал на
    #               последнем живом кадре на всё время загрузки.
    for t in (
        threading.Thread(target=heartbeat_loop, args=(shared, stop_event, sm), daemon=True, name="heartbeat"),
        threading.Thread(target=sync_loop,      args=(stop_event,),            daemon=True, name="sync"),
    ):
        t.start()

    try:
        subprocess.Popen(['unclutter', '-idle', '0', '-root'])
    except FileNotFoundError:
        pass

    try:
        marker1_small, marker2_small = load_markers(MARKER1_PATH, MARKER2_PATH)
    except FileNotFoundError as e:
        # Без маркеров агент бесполезен, но исчезнувшая из дэшборда машина хуже,
        # чем машина в состоянии error: во втором случае хотя бы видно, что чинить.
        # Файлы вернёт ближайший ilsport-update (git reset --hard), после чего
        # kiosk-autostart перезапустит процесс.
        print(f"Ошибка: {e}")
        _set_fault(shared, FAULT_NO_MARKERS, str(e)[:200])
        _shutdown.wait()
        stop_event.set()
        return

    print("Поиск устройства захвата…")
    cap_live = _wait_for_capture(shared, stop_event)
    if cap_live is None:
        stop_event.set()
        return
    shared["cap"] = cap_live

    win = "AD Display"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.imshow(win, np.zeros((1080, 1920, 3), dtype=np.uint8))
    cv2.waitKey(1)
    cv2.moveWindow(win, MONITOR_X_OFFSET, 0)
    cv2.waitKey(200)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cv2.waitKey(200)
    try:
        subprocess.run(['wmctrl', '-r', win, '-b', 'add,above,fullscreen'],
                       check=False, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("[WARN] wmctrl не установлен — панель задач может остаться видимой")

    print("Запускаю mpv…")
    mpv = MpvPlayer(monitor_x=MONITOR_X_OFFSET)
    mpv.start()
    shared["mpv"] = mpv

    threads = [
        threading.Thread(target=capture_thread_fn,
                         args=(cap_live, marker1_small, marker2_small, shared, stop_event, sm),
                         kwargs={"reopen": reopen_capture},
                         daemon=True, name="capture"),
        threading.Thread(target=video_thread_fn,   args=(shared, stop_event, sm), daemon=True, name="video"),
        threading.Thread(target=sender_loop,       args=(stop_event,),            daemon=True, name="playback"),
    ]
    for t in threads:
        t.start()

    print(f"\nЗапущено. SERVER_URL={SERVER_URL}, ADS_DIR={ADS_DIR}, монитор X={MONITOR_X_OFFSET}. Нажмите 'q' для выхода.\n")

    try:
        while not _shutdown.is_set():
            if sm.state == STATE_LIVE:
                frame = shared["live_frame"]
                if frame is not None:
                    cv2.imshow(win, frame)
            if cv2.waitKey(16) & 0xFF in (ord("q"), 27):
                break
    finally:
        stop_event.set()
        heartbeat_event.set()
        # Даём циклу плеера закрыть текущий показ, затем досылаем очередь:
        # без этого статистика последнего сеанса ушла бы только после
        # следующего запуска агента.
        time.sleep(0.7)
        try:
            flush_once()
        except Exception as e:
            print(f"[PLAYBACK] финальная отправка не удалась: {e}")
        mpv.stop()
        # Освобождаем именно текущее устройство: после переподключения на ходу
        # capture_thread_fn кладёт в shared["cap"] новый объект, а локальная
        # cap_live указывает на уже мёртвый.
        (shared.get("cap") or cap_live).release()
        cv2.destroyAllWindows()
        print("Завершено.")


if __name__ == "__main__":
    main()
