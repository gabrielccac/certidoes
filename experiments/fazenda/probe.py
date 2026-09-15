"""Non-issuing Fazenda DF CDP smoke probe.

This intentionally stops before a document value or the ``Gerar PDF`` action
is submitted.  It validates the browser/profile boundary only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(ROOT / "experiments"))

from shared.cdp import CdpFetcher  # noqa: E402
from shared.profile import DEFAULT_PROFILE_DIR  # noqa: E402

URL = "https://ww1.receita.fazenda.df.gov.br/cidadao/certidoes/Certidao"
DOWNLOAD_PREF = "download.default_directory"
SOLVED_ASSET = ROOT / "solved.png"


def chrome_path() -> str:
    candidates = (
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Chrome not found; set CHROME_PATH")


def write_profile(profile: Path, download_dir: Path) -> None:
    """Copy the legacy Save-as-PDF/download preferences into the lean profile."""
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True)
    prefs = {
        "printing": {
            "print_preview_sticky_settings": {
                "appState": json.dumps(
                    {
                        "recentDestinations": [
                            {"id": "Save as PDF", "origin": "local", "account": ""}
                        ],
                        "selectedDestinationId": "Save as PDF",
                        "version": 2,
                        "isCssBackgroundEnabled": True,
                    }
                )
            }
        },
        "savefile": {"default_directory": str(download_dir)},
        "download": {"default_directory": str(download_dir), "prompt_for_download": False},
    }
    (default / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")


def read_profile_observations(profile: Path, download_dir: Path) -> dict[str, object]:
    prefs_path = profile / "Default" / "Preferences"
    if not prefs_path.is_file():
        return {"preferences_file": False}
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"preferences_file": True, "preferences_error": str(exc)}
    app_state = prefs.get("printing", {}).get("print_preview_sticky_settings", {}).get("appState", "")
    try:
        app_state = json.loads(app_state)
    except (TypeError, json.JSONDecodeError):
        app_state = {}
    return {
        "preferences_file": True,
        "download_default_matches": prefs.get("download", {}).get("default_directory") == str(download_dir),
        "download_prompt_disabled": prefs.get("download", {}).get("prompt_for_download") is False,
        "savefile_default_matches": prefs.get("savefile", {}).get("default_directory") == str(download_dir),
        "print_destination": app_state.get("selectedDestinationId"),
        "print_css_background": app_state.get("isCssBackgroundEnabled"),
    }


def find_solved_asset(page, asset: Path = SOLVED_ASSET) -> dict[str, object]:
    """Locate the supplied solved-state image in a live page screenshot."""
    screenshot = cv2.imdecode(
        np.frombuffer(page.screenshot(type="png"), dtype=np.uint8), cv2.IMREAD_COLOR
    )
    template = cv2.imread(str(asset), cv2.IMREAD_COLOR)
    if screenshot is None or template is None:
        raise FileNotFoundError(f"could not decode screenshot or template: {asset}")
    if template.shape[0] > screenshot.shape[0] or template.shape[1] > screenshot.shape[1]:
        return {"matched": False, "confidence": 0.0}
    scores = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
    _, confidence, _, location = cv2.minMaxLoc(scores)
    height, width = template.shape[:2]
    return {
        "matched": confidence >= 0.80,
        "confidence": round(float(confidence), 4),
        "x": int(location[0]),
        "y": int(location[1]),
        "width": width,
        "height": height,
    }


def run(debug_port: int, profile_dir: str | Path = DEFAULT_PROFILE_DIR) -> int:
    started = time.perf_counter()
    profile = Path(profile_dir).resolve()
    download_dir = Path(tempfile.mkdtemp(prefix="fazenda_cdp_downloads_"))
    write_profile(profile, download_dir)
    fetcher: CdpFetcher | None = None
    result: dict[str, object] = {
        "source": "fazenda-df",
        "url": URL,
        "submitted": False,
        "chrome_path": chrome_path(),
        "profile": str(profile),
        "download_dir": str(download_dir),
    }
    try:
        fetcher = CdpFetcher(
            result["chrome_path"],
            debug_port=debug_port,
            profile_dir=str(profile),
            source_name="fazenda-df",
            challenge_poll_s=2,
            solve_max_s=8,
            restart_on_stuck=False,
            lean_profile=True,
        )
        fetcher.start()
        page = fetcher._page
        assert page is not None
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        page.locator("mat-expansion-panel-header").first.wait_for(state="visible", timeout=30_000)
        result["page_load_seconds"] = round(time.perf_counter() - started, 2)
        result["title"] = page.title()
        result["final_url"] = page.url
        result["panel_count"] = page.locator("mat-expansion-panel-header").count()

        page.locator("mat-expansion-panel-header").first.click()
        page.locator("#documento").wait_for(state="visible", timeout=15_000)
        controls = {
            "document": page.locator("#documento").count() > 0,
            "purpose": page.locator("#finalidade").count() > 0,
            "certificate_type": page.locator('mat-select[formcontrolname="TipoCertidao"]').count() > 0,
            "person_radio": page.locator("mat-radio-button").filter(has_text="Pessoa Física").count() > 0,
        }
        assert all(controls.values()), controls
        # This is the only interaction with a person choice; no document is entered.
        page.locator("mat-radio-button").filter(has_text="Pessoa Física").first.click()
        result["controls"] = controls
        result["selected_person"] = page.locator("mat-radio-button").filter(has_text="Pessoa Física").first.get_attribute("class")
        result["generated_pdf_buttons"] = page.get_by_role("button", name="Gerar PDF").count()
        result["solved_asset"] = {"path": str(SOLVED_ASSET), "exists": SOLVED_ASSET.is_file()}
        # Auto-clear is valid; poll briefly before declaring that no solved marker is visible.
        solved = {"matched": False, "confidence": 0.0}
        deadline = time.perf_counter() + 8
        while time.perf_counter() < deadline:
            solved = find_solved_asset(page)
            if solved["matched"]:
                break
            page.wait_for_timeout(500)
        result["solved_asset"].update(solved)
        result["captcha_state"] = "auto_solved" if solved["matched"] else "not_detected"
        result["form_probe_seconds"] = round(time.perf_counter() - started, 2)

        # Print the untouched form to a throwaway file to verify CDP's print path.
        session = page.context.new_cdp_session(page)
        pdf = session.send("Page.printToPDF", {"printBackground": True})
        pdf_path = download_dir / "form_probe.pdf"
        pdf_path.write_bytes(base64.b64decode(pdf["data"]))
        result["print_to_pdf_bytes"] = pdf_path.stat().st_size
        result["print_path_uses_download_dir"] = pdf_path.parent == download_dir
        result["download_files"] = sorted(p.name for p in download_dir.iterdir())
        result["profile_observations"] = read_profile_observations(profile, download_dir)
        assert result["print_to_pdf_bytes"] > 0
        assert result["download_files"] == ["form_probe.pdf"]
        result["status"] = "passed"
    except Exception as exc:  # noqa: BLE001 - probe reports a compact failure
        result["status"] = "blocked"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        if fetcher is not None:
            fetcher.close()
        shutil.rmtree(download_dir, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug-port", type=int, default=9335)
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_DIR))
    args = parser.parse_args()
    return run(args.debug_port, args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
