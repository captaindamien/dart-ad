import os
import time

from .api import get_playlist, get_video_id, heartbeat_event
from .playback import PlaybackTracker
from .state import STATE_VIDEO


def _ask(mpv, method):
    """
    Плеер может быть моком без этого метода, а IPC-запрос — отвалиться по
    таймауту. Ни то, ни другое не должно ронять цикл воспроизведения.
    """
    fn = getattr(mpv, method, None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def video_thread_fn(shared, stop_event, sm):
    mpv = shared["mpv"]
    last_playlist = []
    tracker = PlaybackTracker(
        resolve_video_id=get_video_id,
        resolve_duration=lambda: _ask(mpv, "duration"),
    )
    shared["playback_tracker"] = tracker

    def set_current_video(name):
        # Хартбит вне графика: иначе смена ролика внутри плейлиста доедет до
        # дашборда только со следующим плановым, то есть с задержкой до минуты.
        if shared.get("current_video") != name:
            shared["current_video"] = name
            heartbeat_event.set()

    while not stop_event.is_set():
        current_playlist = get_playlist()

        if sm.state != STATE_VIDEO:
            if not mpv.is_paused:
                mpv.pause_and_hide()
            # Уход в трансляцию обрывает показ на середине — засчитываем как
            # недосмотр. Позицию дочитываем здесь же: последняя опрошенная
            # отстаёт на полсекунды, и показ выходил systematically короче.
            if tracker.is_open():
                tracker.interrupt(position=_ask(mpv, "playback_time"))
            else:
                tracker.interrupt()
            set_current_video(None)
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
            # Команда seek асинхронная: трекер должен дождаться её применения,
            # иначе оформит старую позицию паузы отдельным показом.
            tracker.expect_restart()

        if mpv.is_paused:
            mpv.play()

        fname = mpv.current_filename()
        if fname:
            set_current_video(fname)
        # Позиция нужна трекеру, чтобы отличить повторный показ того же ролика
        # от продолжения текущего — при плейлисте из одного файла имя не меняется.
        tracker.update(fname, _ask(mpv, "playback_time"))

        time.sleep(0.5)

    # При остановке агента незакрытый показ всё равно попадает в очередь.
    tracker.interrupt(position=_ask(mpv, "playback_time") if tracker.is_open() else None)
