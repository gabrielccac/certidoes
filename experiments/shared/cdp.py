"""Real-Chrome CDP fetcher: headed Chrome via DevTools Protocol + Cloudflare
challenge solving (OpenCV clearance-tile click), restart fallback.

This is the infrastructure that makes zapimoveis / vivareal hydration work: a
real browser binary with a real fingerprint, driven over CDP (no automation
markers), that can solve the interactive checkbox challenge by matching the
clearance tile image on the ACTIVE page and clicking it.

Fits the ``Fetcher`` protocol (``get(url) -> FetchResult``) so the discover and
hydrate use cases work unchanged.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass

try:
    from garimpo.infrastructure.http.fetcher import FetchResult
except ModuleNotFoundError:
    # Local experiment copy: keep the browser helper usable without installing
    # the full Garimpo package.
    @dataclass(frozen=True)
    class FetchResult:
        status: int
        html: str
        final_url: str | None = None
        error: str | None = None

CHALLENGE_MARKERS = (
    "attention required", "just a moment", "verify you are human", "checking your browser",
    "um momento", "verificando", "acesso restrito", "captcha",
)
DENIED_MARKERS = ("access denied",)


def _is_challenge(title):
    low = (title or "").lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def _is_denied(title):
    low = (title or "").lower()
    return any(m in low for m in DENIED_MARKERS)


def _kill_process_tree(proc):
    """Stop only the Chrome process launched by this fetcher and its children."""
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            proc.kill()
    except Exception:
        pass


@dataclass
class CdpStats:
    clicks: int = 0
    restarts: int = 0
    challenges: int = 0
    denied: int = 0
    lazy_scroll_rounds: int = 0
    lazy_scroll_added: int = 0
    lazy_scroll_timed_out: int = 0


class CdpFetcher:
    """Drive headed real Chrome via CDP, navigate URLs, solve challenges."""

    def __init__(
        self,
        chrome_path: str,
        debug_port: int = 9222,
        profile_dir: str | None = None,
        clearance_template: str | None = None,
        source_name: str | None = None,
        challenge_poll_s: float = 8.0,
        solve_max_s: float = 20.0,
        restart_on_stuck: bool = True,
        lean_profile: bool = False,
        lazy_scroll_selector: str | None = None,
        lazy_scroll_poll_s: float = 1.0,
        lazy_scroll_timeout_s: float = 30.0,
        lazy_scroll_stable_polls: int = 2,
    ):
        self.chrome_path = chrome_path
        self.debug_port = debug_port
        self.profile_dir = profile_dir
        self.clearance_template = clearance_template
        self.source_name = source_name or "unknown"
        self.challenge_poll_s = challenge_poll_s
        self.solve_max_s = solve_max_s
        self.restart_on_stuck = restart_on_stuck
        self.lean_profile = lean_profile
        self.lazy_scroll_selector = lazy_scroll_selector
        self.lazy_scroll_poll_s = max(0.1, lazy_scroll_poll_s)
        self.lazy_scroll_timeout_s = max(self.lazy_scroll_poll_s, lazy_scroll_timeout_s)
        self.lazy_scroll_stable_polls = max(1, lazy_scroll_stable_polls)
        self.stats = CdpStats()
        self._proc: subprocess.Popen | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # ------------------------------------------------------------- lifecycle

    def _launch_chrome(self):
        profile_dir = os.path.abspath(self.profile_dir) if self.profile_dir else None
        if profile_dir:
            os.makedirs(profile_dir, exist_ok=True)
        cmd = [
            self.chrome_path,
            f"--remote-debugging-port={self.debug_port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check", "about:blank",
        ]
        if os.path.exists("/.dockerenv") or os.environ.get("GARIMPO_DOCKER"):
            cmd += ["--no-sandbox", "--disable-dev-shm-usage"]
        if self.lean_profile:
            cmd += [
                "--disable-extensions", "--disable-component-update",
                "--disable-background-networking", "--disable-sync",
                "--disable-translate", "--disable-default-apps",
            ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.debug_port}/json/version", timeout=2
                ) as r:
                    return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"Chrome debug port {self.debug_port} did not come up")

    def _ensure_chrome(self, relaunch=False):
        if relaunch:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:
                pass
            self._browser = self._context = self._page = self._playwright = None
            _kill_process_tree(self._proc)
            self._proc = None
            time.sleep(2)
        if self._proc is None:
            self._launch_chrome()

    def _connect(self):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.debug_port}"
        )
        self._context = self._browser.contexts[0]
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def start(self):
        self._ensure_chrome()
        if self._browser is None:
            self._connect()

    def close(self):
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        _kill_process_tree(self._proc)

    def evaluate(self, expression, arg=None):
        """Evaluate JavaScript in the current persistent CDP page."""
        self.start()
        return self._page.evaluate(expression) if arg is None else self._page.evaluate(expression, arg)

    # ------------------------------------------------------------ challenge

    def _poll_title(self, seconds):
        for _ in range(int(seconds / 0.5)):
            try:
                title = self._page.title() or ""
            except Exception:
                # Navigation can replace the execution context between the
                # title read and a challenge reload; keep polling the same tab.
                time.sleep(0.2)
                continue
            if not _is_challenge(title):
                return title
            time.sleep(0.5)
        try:
            return self._page.title() or ""
        except Exception:
            return ""

    def _get_content(self, retries=3):
        for _ in range(retries):
            try:
                return self._page.content() or ""
            except Exception:
                time.sleep(1.0)
        return ""

    def _find_tile(self):
        """OpenCV-match the clearance tile on the ACTIVE page. Returns center or None."""
        if not self.clearance_template or not os.path.exists(self.clearance_template):
            return None
        import cv2
        import numpy as np

        shot = self._page.screenshot()
        img = np.frombuffer(shot, dtype=np.uint8)
        screenshot = cv2.imdecode(img, cv2.IMREAD_COLOR)
        template = cv2.imread(str(self.clearance_template), cv2.IMREAD_COLOR)
        if screenshot is None or template is None:
            return None
        result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < 0.55:
            return None
        h, w = template.shape[:2]
        return {"center": (max_loc[0] + w // 2, max_loc[1] + h // 2), "confidence": max_val}

    def _click_tile(self):
        m = self._find_tile()
        if m is None:
            return False
        cx, cy = m["center"]
        self._page.mouse.move(cx, cy, steps=8)
        time.sleep(0.4)
        self._page.mouse.down()
        time.sleep(0.15)
        self._page.mouse.up()
        time.sleep(2.5)
        self.stats.clicks += 1
        return True

    def _solve_challenge(self):
        """Click the clearance tile (CV), poll. Returns True if resolved."""
        title = self._poll_title(8)
        if not _is_challenge(title):
            return True
        if not self._click_tile():
            # Some Cloudflare pages (including ImóvelWeb) render no visible
            # tile and complete the browser check automatically. Keep the
            # same CDP tab alive and poll before declaring it stuck.
            title = self._poll_title(self.solve_max_s)
            return not _is_challenge(title)
        title = self._poll_title(self.solve_max_s)
        return not _is_challenge(title)

    def _settle_lazy_listings(self):
        """Load listing cards until the last rendered card stops growing."""
        if not self.lazy_scroll_selector:
            return
        deadline = time.monotonic() + self.lazy_scroll_timeout_s
        stable_polls = 0
        while time.monotonic() < deadline:
            try:
                links = self._page.locator(self.lazy_scroll_selector)
                before = links.count()
                if before:
                    links.nth(before - 1).evaluate(
                        "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
                    )
                self.stats.lazy_scroll_rounds += 1
                time.sleep(self.lazy_scroll_poll_s)
                after = self._page.locator(self.lazy_scroll_selector).count()
            except Exception:
                return
            added = max(0, after - before)
            self.stats.lazy_scroll_added += added
            if added:
                stable_polls = 0
            else:
                stable_polls += 1
                if stable_polls >= self.lazy_scroll_stable_polls:
                    return
        self.stats.lazy_scroll_timed_out += 1

    # -------------------------------------------------------------- Fetcher

    def get(self, url, *, headers=None, cookies=None, timeout_s=None, _allow_restart=True) -> FetchResult:
        """Navigate to url, handle challenge, return FetchResult. Blocks are
        surfaced via non-200 status so the caller can retry/restart."""
        self.start()
        start = time.perf_counter()
        for attempt in range(3):
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception:
                if attempt == 2:
                    if self.restart_on_stuck and _allow_restart:
                        self.stats.restarts += 1
                        self._ensure_chrome(relaunch=True)
                        self._connect()
                        return self.get(
                            url, headers=headers, cookies=cookies, timeout_s=timeout_s,
                            _allow_restart=False,
                        )
                    return FetchResult(status=0, html="", final_url=url)
                time.sleep(1.5)

        title = self._poll_title(min(self.challenge_poll_s, 8))
        status = 200
        if _is_challenge(title):
            self.stats.challenges += 1
            clearance_started = time.perf_counter()
            print(
                f"[garimpo] cdp clearance source={self.source_name} status=started url={url}",
                flush=True,
            )
            if self._solve_challenge():
                title = self._poll_title(5)
            if _is_challenge(title):
                status = 0  # unresolved challenge -> caller restarts
                print(
                    f"[garimpo] cdp clearance source={self.source_name} status=failed "
                    f"url={url} elapsed_s={time.perf_counter() - clearance_started:.1f}",
                    flush=True,
                )
            else:
                print(
                    f"[garimpo] cdp clearance source={self.source_name} status=success "
                    f"url={url} clicks={self.stats.clicks} "
                    f"elapsed_s={time.perf_counter() - clearance_started:.1f}",
                    flush=True,
                )
        elif _is_denied(title):
            self.stats.denied += 1
            status = 403

        if status == 200:
            self._settle_lazy_listings()
        html = self._get_content() if status == 200 else ""
        if status == 0 and self.restart_on_stuck and _allow_restart:
            self.stats.restarts += 1
            self._ensure_chrome(relaunch=True)
            self._connect()
            return self.get(
                url, headers=headers, cookies=cookies, timeout_s=timeout_s,
                _allow_restart=False,
            )
        return FetchResult(status=status, html=html, final_url=url)
