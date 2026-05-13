"""
Обёртка над внешним процессом mpv для воспроизведения рекламных видео
с аппаратным декодированием (V4L2 M2M на Raspberry Pi 4).

mpv стартует один раз в --idle режиме, окно держится на нужном мониторе
с --ontop. В STATE_LIVE окно сворачивается + плеер в паузе, в STATE_VIDEO
разворачивается и проигрывает плейлист.

Команды передаются через unix-сокет в формате newline-delimited JSON
(см. https://mpv.io/manual/stable/#json-ipc).
"""

import json
import os
import socket
import subprocess
import threading
import time


class MpvPlayer:
    def __init__(self, monitor_x=0, socket_path="/tmp/ilsport-mpv.sock"):
        self.monitor_x   = monitor_x
        self.socket_path = socket_path

        self._proc      = None
        self._sock      = None
        self._lock      = threading.Lock()
        self._req_id    = 0
        self._is_paused = True

    def start(self):
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        args = [
            "mpv",
            "--idle=yes",
            "--force-window=yes",
            "--no-input-default-bindings",
            "--no-osc",
            "--no-osd-bar",
            "--cursor-autohide=no",
            "--no-audio",
            "--hwdec=auto-safe",
            "--vo=gpu",
            "--gpu-context=x11egl",
            "--loop-playlist=inf",
            "--keep-open=no",
            "--no-border",
            f"--geometry=+{self.monitor_x}+0",
            "--pause=yes",
            "--window-minimized=yes",
            f"--input-ipc-server={self.socket_path}",
        ]
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not os.path.exists(self.socket_path):
                time.sleep(0.1)
                continue
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.socket_path)
                self._sock = s
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"mpv IPC socket {self.socket_path} not ready")

    def _send(self, command, expect_response=False, timeout=1.0):
        with self._lock:
            if self._sock is None:
                return None
            self._req_id += 1
            req = {"command": command, "request_id": self._req_id}
            payload = (json.dumps(req) + "\n").encode()
            try:
                self._sock.sendall(payload)
            except OSError as e:
                print(f"[MPV] send error: {e}")
                self._sock = None
                return None

            if not expect_response:
                return None

            self._sock.settimeout(timeout)
            buf = b""
            try:
                while True:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        return None
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line:
                            continue
                        try:
                            resp = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if resp.get("request_id") == self._req_id:
                            return resp
            except (socket.timeout, OSError):
                return None
            finally:
                self._sock.settimeout(None)

    def set_playlist(self, paths):
        if not paths:
            self._send(["playlist-clear"])
            self._send(["stop"])
            return
        self._send(["loadfile", paths[0], "replace"])
        for p in paths[1:]:
            self._send(["loadfile", p, "append"])

    def play(self):
        self._send(["set_property", "window-minimized", False])
        self._send(["set_property", "fullscreen", True])
        self._send(["set_property", "pause", False])
        self._is_paused = False

    def pause_and_hide(self):
        self._send(["set_property", "pause", True])
        self._send(["set_property", "fullscreen", False])
        self._send(["set_property", "window-minimized", True])
        self._is_paused = True

    def restart_current(self):
        self._send(["seek", 0, "absolute"])

    def current_filename(self):
        resp = self._send(["get_property", "filename"], expect_response=True)
        if resp and resp.get("error") == "success":
            return resp.get("data")
        return None

    @property
    def is_paused(self):
        return self._is_paused

    def stop(self):
        try:
            self._send(["quit"])
        except Exception:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
