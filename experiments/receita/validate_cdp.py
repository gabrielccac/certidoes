"""Validate Receita's hCaptcha page through a real Chrome CDP session.

This deliberately stops before any certificate API call and never prints the
token. It is an experiment, not the production Receita crawler.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import time


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS_DIR = ROOT / "experiments"
if str(EXPERIMENTS_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(EXPERIMENTS_DIR))

from shared.cdp import CdpFetcher  # noqa: E402
from shared.profile import DEFAULT_PROFILE_DIR  # noqa: E402


URL = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cpf"
WIDGET_SELECTOR = "[data-hcaptcha-widget-id]"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chrome_path() -> str:
    configured = os.environ.get("CHROME_PATH")
    candidates = [
        configured,
        shutil.which("chrome"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("Chrome not found; set CHROME_PATH to chrome.exe")


def _page_state(fetcher: CdpFetcher) -> dict:
    return fetcher.evaluate(
        """
        () => ({
          url: location.href,
          title: document.title,
          readyState: document.readyState,
          widgetIds: [...document.querySelectorAll('[data-hcaptcha-widget-id]')]
            .map((el) => el.getAttribute('data-hcaptcha-widget-id'))
            .filter(Boolean),
          hcaptchaPresent: !!window.hcaptcha,
          hcaptchaExecuteReady: !!window.hcaptcha && typeof window.hcaptcha.execute === 'function',
          hcaptchaFrames: [...document.querySelectorAll('iframe')]
            .map((frame) => frame.src || '')
            .filter((src) => src.includes('hcaptcha.com'))
            .length,
        })
        """
    )


def _token_probe(fetcher: CdpFetcher, widget_id: str) -> dict:
    result = fetcher.evaluate(
        """
        async ({widgetId, timeoutMs}) => {
          if (!window.hcaptcha || typeof window.hcaptcha.execute !== 'function') {
            return {status: 'not-ready'};
          }
          try {
            const value = await Promise.race([
              window.hcaptcha.execute(widgetId, {async: true}),
              new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), timeoutMs)),
            ]);
            const token = value && typeof value === 'object' ? value.response : value;
            return {status: token ? 'token-generated' : 'empty-token', length: token ? token.length : 0};
          } catch (error) {
            return {status: 'error', error: String(error && error.message || error).slice(0, 160)};
          }
        }
        """,
        {"widgetId": widget_id, "timeoutMs": 20000},
    )
    # A token is intentionally reduced to its length; it is never persisted.
    return result if isinstance(result, dict) else {"status": "invalid-probe-result"}


def _profile_observation(profile_dir: Path) -> dict:
    files = [path for path in profile_dir.rglob("*") if path.is_file()] if profile_dir.exists() else []
    cookie_candidates = (
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    )
    cookies = next((path for path in cookie_candidates if path.exists()), None)
    return {
        "profile_exists": profile_dir.exists(),
        "file_count": len(files),
        "cookies_db_path": str(cookies.relative_to(profile_dir)) if cookies else None,
        "cookies_db_bytes": cookies.stat().st_size if cookies else 0,
    }


def run(wait_seconds: float = 30.0, profile_dir: str | Path = DEFAULT_PROFILE_DIR) -> dict:
    profile_dir = Path(profile_dir).resolve()
    port = _free_port()
    started = time.perf_counter()
    fetcher: CdpFetcher | None = None
    report: dict = {
        "source": "receita",
        "url": URL,
        "chrome_path": None,
        "debug_port": port,
        "profile_dir": str(profile_dir),
        "lean_profile": True,
        "status": "failed",
    }
    try:
        chrome = _chrome_path()
        report["chrome_path"] = chrome
        fetcher = CdpFetcher(
            chrome_path=chrome,
            debug_port=port,
            profile_dir=str(profile_dir),
            source_name="receita-experiment",
            lean_profile=True,
            restart_on_stuck=False,
        )
        navigation_started = time.perf_counter()
        result = fetcher.get(URL)
        report["navigation_seconds"] = round(time.perf_counter() - navigation_started, 3)
        report["fetch_status"] = result.status
        report["final_url"] = result.final_url

        deadline = time.monotonic() + wait_seconds
        state = _page_state(fetcher)
        while not state["widgetIds"] and time.monotonic() < deadline:
            time.sleep(0.5)
            state = _page_state(fetcher)
        report["page"] = state
        report["cookies_before_close"] = len(fetcher._context.cookies())  # noqa: SLF001

        if not state["widgetIds"]:
            report["status"] = "blocked-widget-missing"
        elif not state["hcaptchaExecuteReady"]:
            report["status"] = "blocked-hcaptcha-not-ready"
        else:
            report["token_probe"] = _token_probe(fetcher, state["widgetIds"][0])
            report["status"] = "validated" if report["token_probe"]["status"] in {
                "token-generated", "empty-token"
            } else "token-probe-blocked"
        report["profile_before_close"] = _profile_observation(profile_dir)
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        if fetcher is not None:
            fetcher.close()
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["profile_after_close"] = _profile_observation(profile_dir)
        report["profile_retained"] = profile_dir.exists()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-seconds", type=float, default=30.0)
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_DIR))
    args = parser.parse_args()
    report = run(wait_seconds=max(0.0, args.wait_seconds), profile_dir=args.profile)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
