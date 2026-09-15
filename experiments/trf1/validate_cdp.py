"""Smoke-test TRF1 page readiness through real Chrome + CDP.

This intentionally stops before filling a document or submitting the form.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from experiments.shared.cdp import CdpFetcher  # noqa: E402
from experiments.shared.profile import DEFAULT_PROFILE_DIR  # noqa: E402


TRF1_URL = "https://sistemas.trf1.jus.br/certidao/#/solicitacao"
DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _recaptcha_metadata(frame_urls: list[str]) -> dict[str, object]:
    keys: list[str] = []
    versions: list[str] = []
    for frame_url in frame_urls:
        query = parse_qs(urlparse(frame_url).query)
        keys.extend(query.get("k", []))
        versions.extend(query.get("v", []))
    return {
        "iframe_urls": frame_urls,
        "keys": sorted(set(keys)),
        "versions": sorted(set(versions)),
    }


def validate(
    chrome_path: str = DEFAULT_CHROME,
    timeout_ms: int = 20_000,
    profile_dir: str | Path = DEFAULT_PROFILE_DIR,
) -> dict[str, object]:
    profile_dir = str(Path(profile_dir).resolve())
    debug_port = _free_port()
    fetcher = CdpFetcher(
        chrome_path=chrome_path,
        debug_port=debug_port,
        profile_dir=profile_dir,
        source_name="trf1-experiment",
        lean_profile=True,
        restart_on_stuck=False,
    )
    started = time.perf_counter()
    result: dict[str, object] = {
        "source": "trf1",
        "url": TRF1_URL,
        "chrome_path": chrome_path,
        "debug_port": debug_port,
        "profile_dir": profile_dir,
    }
    try:
        fetcher.start()
        page = fetcher._page  # The experiment needs the attached CDP page only.
        page.goto(TRF1_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_selector("iframe[src*='recaptcha']", timeout=timeout_ms)
        frame_urls = [frame.url for frame in page.frames if "recaptcha" in frame.url]
        metadata = _recaptcha_metadata(frame_urls)
        result.update(
            {
                "status": "ready",
                "title": page.title(),
                "final_url": page.url,
                "iframe_count": len(frame_urls),
                **metadata,
                "elapsed_s": round(time.perf_counter() - started, 2),
            }
        )
        return result
    except Exception as exc:
        result.update(
            {
                "status": "blocked",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "elapsed_s": round(time.perf_counter() - started, 2),
            }
        )
        return result
    finally:
        fetcher.close()
        result["profile_retained"] = os.path.isdir(profile_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", default=os.environ.get("CHROME_PATH", DEFAULT_CHROME))
    parser.add_argument("--timeout-ms", type=int, default=20_000)
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_DIR))
    args = parser.parse_args()
    if not os.path.isfile(args.chrome):
        print(json.dumps({"status": "blocked", "error": f"Chrome not found: {args.chrome}"}))
        return 2
    result = validate(args.chrome, args.timeout_ms, args.profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
