import time

STATE_LIVE  = "live"
STATE_VIDEO = "video"

# Неисправности агента. Состояние LIVE/VIDEO описывает, что происходит на
# экране, и к железу отношения не имеет — поэтому отказ оборудования живёт
# отдельным флагом в shared["fault"], а не ещё одним состоянием StateManager.
# Любой непустой fault агент репортит серверу как state="error": бэкенд такое
# значение принимает (AGENT_REPORTED_STATES) и не засчитывает в аптайм, зато
# машина перестаёт выглядеть выключенной.
FAULT_NO_CAPTURE   = "no_capture_device"   # карта захвата не найдена при старте
FAULT_CAPTURE_LOST = "capture_lost"        # карта отвалилась на ходу
FAULT_NO_MARKERS   = "no_markers"          # marker.png/marker2.png не читаются
FAULT_AD_STUCK     = "ad_stuck"            # реклама идёт дольше STUCK_WARN_SEC без marker2

# Аппаратные отказы репортятся как state="error": экрана в этот момент нет.
# ad_stuck — не отказ железа: реклама реально крутится, состояние остаётся
# "playing", а fault уходит рядом, чтобы дэшборд показал проблему, не
# перевирая, что на экране.
HARDWARE_FAULTS = frozenset({FAULT_NO_CAPTURE, FAULT_CAPTURE_LOST, FAULT_NO_MARKERS})


class StateManager:
    """
    Управляет переходами между состояниями LIVE и VIDEO.
    on_change_callback(old_state, new_state, duration_seconds) вызывается при каждой смене.
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
