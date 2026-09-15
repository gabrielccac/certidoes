from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path


class CdpChrome:
    """Small headed-Chrome/CDP session using the shared lean profile."""

    def __init__(self, chrome_path: str, *, debug_port: int, profile_dir: Path, kiosk_printing: bool = False, incognito: bool = False) -> None:
        self.chrome_path = chrome_path
        self.debug_port = debug_port
        self.profile_dir = Path(profile_dir)
        self.kiosk_printing = kiosk_printing
        self.incognito = incognito
        self._process: subprocess.Popen | None = None
        self._playwright = None
        self._browser = None
        self.page = None

    def start(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.chrome_path,
            f"--remote-debugging-port={self.debug_port}",
            f"--user-data-dir={self.profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-component-update",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--disable-default-apps",
            "about:blank",
        ]
        if self.kiosk_printing:
            command.insert(-1, "--kiosk-printing")
        if self.incognito:
            command.insert(-1, "--incognito")
        if Path("/.dockerenv").exists() or os.environ.get("CERTIDOES_DOCKER"):
            command[1:1] = ["--no-sandbox", "--disable-dev-shm-usage"]
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.debug_port}/json/version", timeout=2):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"Chrome debug port {self.debug_port} did not come up")

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.debug_port}"
        )
        context = self._browser.contexts[0]
        self.page = context.pages[0] if context.pages else context.new_page()
        return self.page

    def close(self) -> None:
        try:
            if self._playwright is not None:
                self._playwright.stop()
        finally:
            if self._process is not None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    self._process.kill()
                self._process = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
