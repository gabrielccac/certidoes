"""Run the official Receita flow and retain only redacted diagnostic evidence."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from certidoes.github_payload import decrypt
from certidoes.sources import receita
from experiments.receita_actions_probe.runner import validate_pdf


def network_metadata() -> dict:
    try:
        response = requests.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=10)
        response.raise_for_status()
        data = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
        return {key: data.get(key) for key in ("loc", "colo", "http", "tls")}
    except Exception as error:
        return {"probe_error": type(error).__name__}


def form_state(page, document: str) -> dict:
    return page.evaluate(r"""expected => {
      const ni=document.querySelector('input[name="niContribuinte"]');
      const birth=document.querySelector('input[name="dataNascimento"]');
      return {focus:document.hasFocus(), visibility:document.visibilityState,
        document_length:ni?.value.replace(/\D/g,'').length || 0,
        document_matches:!!ni && ni.value.replace(/\D/g,'')===expected,
        birth_length:birth?.value.length || 0,
        document_valid:ni?.validity.valid, birth_valid:birth?.validity.valid,
        viewport:[innerWidth,innerHeight], screen:[screen.width,screen.height],
        ready:document.readyState,
        portal_rejected_url:/requested URL was rejected/i.test(document.body?.innerText || ''),
        portal_hcaptcha_error:/106\s*-\s*\d{2}\/\d{2}\/\d{4}/.test(document.body?.innerText || '')};
    }""", document)


def payload_metadata(payload, document: str, birth: str) -> dict:
    if not isinstance(payload, dict):
        return {"object": False}
    return {"keys": sorted(payload),
            "document_matches": re.sub(r"\D", "", str(payload.get("ni", ""))) == document,
            "birth_matches": payload.get("dataNascimento") == birth,
            "contributor": payload.get("tipoContribuinte") in ("PF", "PJ", 0, 1)}


def challenge_metadata(body) -> dict:
    if not isinstance(body, dict):
        return {"object": False}
    result = {"keys": sorted(body)}
    for key in ("pass", "success"):
        if isinstance(body.get(key), bool):
            result[key] = body[key]
    token = body.get("generated_pass_UUID")
    if isinstance(token, str):
        result["generated_token_length"] = len(token)
    kind = body.get("request_type")
    if kind in ("image_label_binary", "image_label_area_select", "image_drag_drop"):
        result["request_type"] = kind
    result["challenge_present"] = isinstance(body.get("tasklist"), list) and bool(body["tasklist"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("headed", "headless", "front", "no-trace"), required=True)
    parser.add_argument("--single-sdk", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"mode": args.mode, "single_sdk": args.single_sdk,
              "os": platform.system(), "python": platform.python_version(),
              "playwright": version("playwright"), "commit": os.environ.get("GITHUB_SHA"),
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "profile": "existing" if (args.output / "profile").exists() else "fresh",
              "network": network_metadata(), "events": [], "states": [],
              "challenge_responses": [], "captcha_sdk_loads": [], "status": "running"}
    started = time.monotonic()
    original_browser, original_log, original_trace = receita._browser, receita._log, receita._trace_captcha
    active_page = None
    document, birth = "", ""

    def log(event, **fields):
        report["events"].append({"at_ms": round((time.monotonic() - started) * 1000),
                                  "event": event, **fields})
        original_log(event, **fields)
        if active_page is not None and event in ("page_ready", "form_submitted"):
            if event == "page_ready" and args.mode == "front":
                active_page.bring_to_front()
            if args.mode != "no-trace":
                try:
                    report["states"].append({"phase": event, **form_state(active_page, document)})
                except Exception as error:
                    report["states"].append({"phase": event, "probe_error": type(error).__name__})

    def verification_request(request):
        if request.url == receita.VERIFICAR_URL and request.method == "POST":
            try:
                report["submitted_payload"] = payload_metadata(request.post_data_json, document, birth)
            except Exception as error:
                report["payload_probe_error"] = type(error).__name__

    def verification_response(response):
        host = urlsplit(response.url).hostname or ""
        if host == "hcaptcha.com" or host.endswith(".hcaptcha.com"):
            if host == "js.hcaptcha.com" and urlsplit(response.url).path == "/1/api.js":
                report["captcha_sdk_loads"].append({"http": response.status,
                    "render_explicit": "render=explicit" in urlsplit(response.url).query})
            route = urlsplit(response.url).path.strip("/").split("/")[0]
            if route in ("checksiteconfig", "getcaptcha", "checkcaptcha"):
                try:
                    report["challenge_responses"].append({
                        "at_ms": round((time.monotonic() - started) * 1000),
                        "route": route, "http": response.status,
                        **challenge_metadata(response.json())})
                except Exception as error:
                    report["challenge_responses"].append({"route": route,
                        "http": response.status, "probe_error": type(error).__name__})
        if response.url == receita.VERIFICAR_URL and response.request.method == "POST":
            try:
                body = response.json()
                report["verification"] = {"http": response.status}
                for key in ("status", "statusValidacao"):
                    value = body.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z_-]{1,80}", value):
                        report["verification"][key] = value
            except Exception as error:
                report["response_probe_error"] = type(error).__name__

    @contextmanager
    def browser(request):
        nonlocal active_page
        with ExitStack() as stack:
            executable = receita._chrome_path(request.chrome_path)
            original_popen = subprocess.Popen
            stderr = stack.enter_context((args.output / "chrome-stderr.log").open("w"))

            def launch(command, **kwargs):
                if command[0] == executable:
                    kwargs["stderr"] = stderr
                    if args.mode == "headless":
                        command = [command[0], "--headless=new", *command[1:]]
                return original_popen(command, **kwargs)

            stack.enter_context(patch("subprocess.Popen", side_effect=launch))
            if args.mode == "headless":
                chrome = receita.CdpChrome(executable, debug_port=request.debug_port,
                                          profile_dir=request.profile_dir)
                stack.callback(chrome.close)
                page = chrome.start()
            else:
                page = stack.enter_context(original_browser(request))
            active_page = page
            if args.single_sdk:
                page.route("https://js.hcaptcha.com/1/api.js", lambda route: route.abort())
            report["browser_version"] = page.context.browser.version
            page.on("request", verification_request)
            page.on("response", verification_response)
            if args.mode != "no-trace":
                report["fingerprint"] = page.evaluate("""() => {
                  const gl=document.createElement('canvas').getContext('webgl');
                  const ext=gl&&gl.getExtension('WEBGL_debug_renderer_info');
                  return {user_agent:navigator.userAgent,language:navigator.language,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
                    platform:navigator.platform,webdriver:navigator.webdriver,
                    webgl:!!gl,renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null};
                }""")
            try:
                yield page
            except Exception:
                try:
                    report["failure_state"] = form_state(page, document)
                    page.screenshot(path=str(args.output / "failure.png"),
                                    mask=[page.locator("input,textarea")], timeout=3000)
                except Exception as error:
                    report["capture_error"] = type(error).__name__
                raise
            finally:
                active_page = None

    try:
        ciphertext = os.environ.get("RECEITA_PAYLOAD")
        payload = (decrypt(ciphertext, os.environ["RECEITA_PAYLOAD_PRIVATE_KEY"]) if ciphertext else
                   {"documento": os.environ["LIVE_DOCUMENT"], "data_nascimento": os.environ["LIVE_BIRTH"]})
        document, birth = payload["documento"], payload["data_nascimento"]
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::add-mask::{document}\n::add-mask::{birth}", flush=True)
        os.environ["RECEITA_EXECUTOR"] = "local"
        receita._browser, receita._log = browser, log
        if args.mode == "no-trace":
            receita._trace_captcha = lambda *arguments: None
        result = receita.generate(receita.ReceitaRequest(document, birth, output_dir=args.output,
                    profile_dir=args.output / "profile", captcha_retries=1))
        report.update(validate_pdf(result.pdf_path, document))
        result.pdf_path.replace(args.output / "certificate.pdf")
        report.update(status="success", metadata=result.metadata)
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__,
                      stage=getattr(error, "stage", None), code=getattr(error, "code", None))
    finally:
        receita._browser, receita._log, receita._trace_captcha = original_browser, original_log, original_trace
        stderr_path = args.output / "chrome-stderr.log"
        if stderr_path.exists():
            text = stderr_path.read_text(errors="replace")
            report["chrome_startup"] = {
                "sandbox_error": bool(re.search(r"no usable sandbox|failed to move to new namespace|setuid sandbox|user namespace.*not permitted", text, re.I)),
                "display_error": bool(re.search(r"missing X server|cannot open display|could not connect.*Wayland", text, re.I)),
                "missing_libraries": sorted(set(re.findall(r"(lib[\w.+-]+\.so(?:\.\d+)*): cannot open", text))),
            }
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({key: value for key, value in report.items() if key not in ("events", "states")}), flush=True)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
