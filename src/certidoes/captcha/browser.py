"""Browser lifecycle: Xvfb + real Chrome (user-data-dir) + CDP wait + cleanup.
No Playwright launch flags -> cleanest fingerprint; control via CDP attach.
"""
import os
import socket
import subprocess
import time
import urllib.request


def _free_display(start=97):
    d = start
    while os.path.exists(f"/tmp/.X{d}-lock"):
        d += 1
    return d


def _port_open(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


class ManagedBrowser:
    def __init__(self, profile_dir, cdp_port=9223, display=None, lang="pt-BR",
                 window="1280,900", chrome_bin="/usr/bin/google-chrome"):
        self.profile_dir = profile_dir
        self.cdp_port = cdp_port
        self.display = display or _free_display()
        self.lang = lang
        self.window = window
        self.chrome_bin = chrome_bin
        self.procs = []

    def start(self, timeout=40):
        if _port_open(self.cdp_port):
            raise RuntimeError("CDP port already in use; choose another --cdp-port")
        os.makedirs(self.profile_dir, exist_ok=True)
        env = dict(os.environ, DISPLAY=f":{self.display}", XDG_SESSION_TYPE="x11")
        env.pop("WAYLAND_DISPLAY", None)
        xv = subprocess.Popen(["Xvfb", f":{self.display}", "-screen", "0",
                               f"{self.window.split(',')[0]}x{self.window.split(',')[1]}x24"],
                              stdout=open(f"/tmp/xvfb{self.display}.log", "ab"),
                              stderr=subprocess.STDOUT)
        self.procs.append(xv)
        time.sleep(2)
        if xv.poll() is not None:
            self.stop()
            raise RuntimeError("Xvfb exited during startup")
        ch = subprocess.Popen(
            [self.chrome_bin, f"--user-data-dir={self.profile_dir}",
             "--ozone-platform=x11",
             f"--remote-debugging-port={self.cdp_port}",
             "--no-first-run", "--no-default-browser-check",
             "--disable-dev-shm-usage", "--renderer-process-limit=4",
             f"--window-size={self.window}",
             f"--lang={self.lang}", "about:blank"],
            env=env, stdout=open("/tmp/cdp-chrome-run.log", "ab"),
            stderr=subprocess.STDOUT)
        self.procs.append(ch)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/version",
                                       timeout=3).read()
                return f"http://127.0.0.1:{self.cdp_port}"
            except Exception:
                if ch.poll() is not None:
                    raise RuntimeError("chrome exited during startup")
                time.sleep(1)
        raise RuntimeError("CDP not ready in time")

    def alive(self):
        return _port_open(self.cdp_port)

    def stop(self):
        for pr in reversed(self.procs):
            try:
                pr.terminate()
                try:
                    pr.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pr.kill()
                    pr.wait(timeout=5)
            except Exception:
                pass
        self.procs = []
