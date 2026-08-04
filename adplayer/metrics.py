import os
import socket


def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


# Загрузка CPU считается как дельта между двумя замерами /proc/stat,
# поэтому нужно помнить предыдущий. Первый вызов после старта вернуть
# ничего не может — там ещё не с чем сравнивать.
_prev_cpu = None


def _cpu_percent():
    line = _read_file("/proc/stat").split("\n", 1)[0]
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None

    try:
        values = [int(v) for v in parts[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None

    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    total = sum(values)

    global _prev_cpu
    prev = _prev_cpu
    _prev_cpu = (total, idle)
    if prev is None:
        return None

    total_delta = total - prev[0]
    idle_delta = idle - prev[1]
    if total_delta <= 0:
        return None

    busy = 100.0 * (total_delta - idle_delta) / total_delta
    return round(max(0.0, min(100.0, busy)), 1)


def get_system_metrics():
    metrics = {}

    uptime_raw = _read_file("/proc/uptime").split()
    if uptime_raw:
        metrics["uptime_seconds"] = int(float(uptime_raw[0]))

    for line in _read_file("/proc/meminfo").splitlines():
        parts = line.split()
        if parts[0] == "MemTotal:":
            metrics["ram_total_mb"] = int(parts[1]) // 1024
        elif parts[0] == "MemAvailable:":
            metrics["ram_used_mb"] = metrics.get("ram_total_mb", 0) - int(parts[1]) // 1024

    try:
        st = os.statvfs("/")
        metrics["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
        metrics["disk_used_gb"]  = round((st.f_blocks - st.f_bfree) * st.f_frsize / 1e9, 1)
    except OSError:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        metrics["local_ip"] = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    temp_raw = _read_file("/sys/class/thermal/thermal_zone0/temp").strip()
    if temp_raw:
        metrics["cpu_temp"] = round(int(temp_raw) / 1000, 1)

    cpu = _cpu_percent()
    if cpu is not None:
        metrics["cpu_percent"] = cpu

    return metrics
