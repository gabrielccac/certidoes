"""Isolated Actions browser comparison; exports reports, never profiles or PDFs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import time

from certidoes.github_payload import decrypt
from certidoes.sources import receita

CLOAK_WRAPPER = "0.6.4"
CLOAK_BINARY = "146.0.7680.177.5"


def validate_pdf(path: Path, expected_document: str) -> dict:
    from pypdf import PdfReader

    data = path.read_bytes()
    assert data.startswith(b"%PDF-") and len(data) > 1000, "Invalid certificate PDF"
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert expected_document in re.sub(r"\D", "", text), "Certificate document mismatch"
    return {"bytes": len(data), "pages": len(reader.pages), "sha256": hashlib.sha256(data).hexdigest(),
            "document_matches": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=("standard", "cloak", "cloak-native"), required=True)
    args = parser.parse_args()
    out = Path("runs/actions-browser-probe") / args.browser
    out.mkdir(parents=True, exist_ok=True)
    report = {"browser": args.browser, "os": platform.system(), "status": "running",
              "display": "xvfb-run" if platform.system() == "Linux" else "Windows desktop",
              "profile": "fresh CI profile", "events": []}
    started = time.monotonic()
    original_log = receita._log
    original_browser = receita._browser

    def log(event, **fields):
        report["events"].append({"event": event, **fields})
        original_log(event, **fields)

    def record_browser(page):
        browser = page.context.browser
        report["browser_version"] = browser.version if browser else None
        report["fingerprint"] = page.evaluate("""() => {
            const gl=document.createElement('canvas').getContext('webgl');
            const ext=gl&&gl.getExtension('WEBGL_debug_renderer_info');
            return {userAgent:navigator.userAgent,platform:navigator.platform,
                    language:navigator.language,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
                    screen:[screen.width,screen.height],webdriver:navigator.webdriver,
                    renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null};
        }""")
        print(json.dumps({"event":"probe_browser","browser":args.browser,
                          "version":report["browser_version"],**report["fingerprint"]}),flush=True)

    @contextmanager
    def browser(request):
        if args.browser.startswith("cloak"):
            from cloakbrowser import launch_persistent_context
            from cloakbrowser.download import ensure_binary

            binary = Path(ensure_binary(browser_version=CLOAK_BINARY))
            with binary.open("rb") as handle:
                report["binary_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
            options = {}
            launch_args = ["--window-size=1280,900"]
            if args.browser == "cloak-native":
                assert platform.system() == "Linux", "Native variant is Linux-only"
                options["stealth_args"] = False
                launch_args = ["--no-sandbox", "--fingerprint=31415", *launch_args]
            report["launch_args"] = launch_args
            context = launch_persistent_context(str(request.profile_dir.resolve()),
                headless=False, browser_version=CLOAK_BINARY, humanize=False,
                args=launch_args, **options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                record_browser(page)
                yield page
            finally:
                context.close()
        else:
            with original_browser(request) as page:
                record_browser(page)
                yield page

    try:
        ciphertext = os.environ.get("RECEITA_PAYLOAD", "")
        payload = (decrypt(ciphertext, os.environ["RECEITA_PAYLOAD_PRIVATE_KEY"]) if ciphertext else
                   {"documento": os.environ["LIVE_DOCUMENT"].strip(),
                    "data_nascimento": os.environ["LIVE_BIRTH"].strip()})
        document, birth = payload["documento"], payload["data_nascimento"]
        print(f"::add-mask::{document}", flush=True)
        print(f"::add-mask::{birth}", flush=True)
        os.environ["RECEITA_EXECUTOR"] = "local"
        receita._log = log
        receita._browser = browser
        if args.browser.startswith("cloak"):
            from importlib.metadata import version
            report.update(wrapper_version=version("cloakbrowser"), binary_pin=CLOAK_BINARY)
            assert report["wrapper_version"] == CLOAK_WRAPPER, "Unexpected CloakBrowser wrapper"
        request = receita.ReceitaRequest(document, birth, output_dir=out,
                    profile_dir=Path("tmp/actions-browser-probe") / args.browser, captcha_retries=1)
        result = receita.generate(request)
        report.update(validate_pdf(result.pdf_path, document))
        report.update(status="success", metadata=result.metadata)
        result.pdf_path.replace(out / "certificate.pdf")
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__,
                      stage=getattr(error, "stage", None), code=getattr(error, "code", None))
        if isinstance(error, receita.CertificateError):
            report["message"] = error.message
    finally:
        receita._log = original_log
        receita._browser = original_browser
        report["elapsed_s"] = round(time.monotonic() - started, 1)
        (out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "events"}), flush=True)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
