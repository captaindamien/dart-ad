import os
import socket


def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


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

    return metrics
