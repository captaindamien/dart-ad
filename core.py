import time

STATE_LIVE  = "live"
STATE_VIDEO = "video"


class StateManager:
    """
    Управляет переходами между состояниями LIVE и VIDEO.
    on_change_callback(old_state, new_state, duration_seconds) вызывается при каждой смене —
    сюда подключается бэкенд (HTTP, WebSocket, etc.).
    """

    def __init__(self, on_change_callback=None):
        self.state = STATE_LIVE
        self._on_change = on_change_callback
        self._state_start = time.time()
        self.transitions = 0

    def transition(self, new_state):
        if new_state == self.state:
            return
        duration = time.time() - self._state_start
        old = self.state
        self.state = new_state
        self._state_start = time.time()
        self.transitions += 1
        print(f"[STATE] {old} → {new_state}  (было {duration:.1f}s)")
        if self._on_change:
            self._on_change(old, new_state, duration)

    def time_in_state(self):
        return time.time() - self._state_start
