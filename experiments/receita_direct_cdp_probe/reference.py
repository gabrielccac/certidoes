"""Exercise Receita's form with SeleniumBase Pure CDP, without Playwright."""
from __future__ import annotations

import argparse
import asyncio
import base64
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
from types import SimpleNamespace

from certidoes.github_payload import decrypt
from certidoes.sources import receita
from experiments.receita_actions_probe.runner import validate_pdf
from experiments.receita_environment_probe.probe import payload_metadata

SDK_URL = "https://js.hcaptcha.com/1/api.js"


def verification_metadata(status: int, body: dict) -> dict:
    result = {"http": status}
    for key in ("status", "statusValidacao"):
        value = body.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_-]{1,80}", value):
            result[key] = value
    return result


def run(request: receita.ReceitaRequest, *, android_port: int | None, single_sdk: bool, report: dict, chrome_factory=None) -> Path:
    import mycdp as cdp
    if chrome_factory is None:
        from seleniumbase import sb_cdp
        chrome_factory = sb_cdp.Chrome

    document, kind, contributor = receita._normalize(request)
    payload = receita._payload(request, document, kind, contributor)
    options = ({"host": "127.0.0.1", "port": android_port} if android_port else {
        "headless": False, "headed": True, "sandbox": not Path("/.dockerenv").exists(),
        "browser_executable_path": receita._chrome_path(request.chrome_path),
        "user_data_dir": str(request.profile_dir.resolve())})
    sb = chrome_factory(**options)
    try:
        loop = sb.get_event_loop()

        def send(command):
            return loop.run_until_complete(sb.page.send(command))

        def pump(seconds=0.2):
            loop.run_until_complete(asyncio.sleep(seconds))

        report["browser_version"] = send(cdp.browser.get_version())[1]
        responses, pending = {}, {}

        def on_request(event):
            if event.request.url == receita.VERIFICAR_URL:
                try:
                    report["submitted_payload"] = payload_metadata(
                        json.loads(event.request.post_data), document, request.data_nascimento)
                    headers = {key.lower(): str(value) for key, value in event.request.headers.items()}
                    report["submitted_token_length"] = len(headers.get("x-captcha-token", ""))
                    report["request_mobile_header"] = headers.get("sec-ch-ua-mobile")
                except Exception as error:
                    report["request_probe_error"] = type(error).__name__

        def on_response(event):
            url = event.response.url
            if "/servico/certidoes/api/" in url:
                # Path and status only: shows which Receita step answered, never the body.
                path = url.split("/servico/certidoes/api/", 1)[1].split("?", 1)[0]
                report.setdefault("api_calls", []).append([re.sub(r"\d{4,}", "<n>", path)[:60], int(event.response.status)])
            if url.startswith(SDK_URL):
                report["sdk_loads"].append({"http": int(event.response.status),
                    "explicit": "render=explicit" in url})
            if url in (receita.VERIFICAR_URL, receita.EMISSAO_URL):
                pending[event.request_id] = (url, int(event.response.status))

        async def on_finished(event):
            if event.request_id in pending:
                url, status = pending.pop(event.request_id)
                try:
                    body, encoded = await sb.page.send(cdp.network.get_response_body(event.request_id))
                    if encoded:
                        body = base64.b64decode(body).decode()
                    responses[url] = (status, json.loads(body))
                    if url == receita.VERIFICAR_URL:
                        report["verification"] = verification_metadata(status, responses[url][1])
                except Exception as error:
                    report["response_probe_error"] = type(error).__name__

        sb.add_handler(cdp.network.RequestWillBeSent, on_request)
        sb.add_handler(cdp.network.ResponseReceived, on_response)
        sb.add_handler(cdp.network.LoadingFinished, on_finished)
        send(cdp.network.enable())
        if single_sdk:
            async def paused(event):
                if event.request.url == SDK_URL:
                    await sb.page.send(cdp.fetch.fail_request(event.request_id, cdp.network.ErrorReason.BLOCKED_BY_CLIENT))
                else:
                    await sb.page.send(cdp.fetch.continue_request(event.request_id))
            sb.add_handler(cdp.fetch.RequestPaused, paused)
            send(cdp.fetch.enable(patterns=[cdp.fetch.RequestPattern(url_pattern=SDK_URL)]))

        sb.goto(receita.PAGE_URLS[kind])
        report["fingerprint"] = sb.evaluate("""(() => ({
            ua:navigator.userAgent,platform:navigator.platform,
            mobile:navigator.userAgentData?.mobile,touch:navigator.maxTouchPoints,
            viewport:[innerWidth,innerHeight],screen:[screen.width,screen.height],
            dpr:devicePixelRatio,coarse_pointer:matchMedia('(pointer:coarse)').matches,
            timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
            language:navigator.language,webdriver:navigator.webdriver}))()""")
        if android_port:
            assert "Android" in report["fingerprint"]["ua"], "Browser is not running Android"
            # userAgentData is occasionally absent right after load; the UA token is equivalent.
            assert report["fingerprint"].get("mobile", "Mobile" in report["fingerprint"]["ua"]), \
                "Android mobile browser not active"
        sb.wait_for_element_visible('input[name="niContribuinte"]', timeout=30)
        # Keep the page's own Emissao response in memory: CDP Network events can go silent
        # and a second Emissao call after the portal already issued may answer 5xx.
        sb.evaluate("""(() => {
            if (window.__certidoesEmissao !== undefined) return;
            window.__certidoesEmissao = null;
            const keep = (url, status, text) => {
                if (/\\/api\\/Emissao$/.test(String(url).split('?')[0])) window.__certidoesEmissao = {status, text};
            };
            const open = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function (method, url) {
                this.addEventListener('load', () => keep(this.responseURL || url, this.status, this.responseText));
                return open.apply(this, arguments);
            };
            const fetch0 = window.fetch;
            window.fetch = async function (...args) {
                const response = await fetch0.apply(this, args);
                response.clone().text().then(t => keep(response.url, response.status, t)).catch(() => {});
                return response;
            };
        })()""")
        sb.click_if_visible('button:contains("Aceitar")')
        formatted_birth = ""
        if kind == "CPF":
            year, month, day = request.data_nascimento.split("-")
            formatted_birth = f"{day}/{month}/{year}"

        def fill(selector: str, value: str):
            if android_port:
                sb.clear(selector)
                sb.press_keys(selector, value)
            else:
                sb.type(selector, value)

        def read_form() -> dict:
            return sb.evaluate("""(() => ({
                document_matches:document.querySelector('input[name="niContribuinte"]').value.replace(/\\D/g,'')===""" + json.dumps(document) + """,
                birth_matches:document.querySelector('input[name="dataNascimento"]')?.value===""" + json.dumps(formatted_birth) + """,
                birth_length:document.querySelector('input[name="dataNascimento"]')?.value.length,
                form_invalid:!!document.querySelector('form.ng-invalid'),
                focus:document.hasFocus(),visibility:document.visibilityState}))()""")

        # Android key events into the masked inputs occasionally drop a character; retype only the wrong field.
        fill('input[name="niContribuinte"]', document)
        if formatted_birth:
            fill('input[name="dataNascimento"]', formatted_birth)
        for attempt in range(4):
            report["form_state"] = read_form()
            document_ok = report["form_state"]["document_matches"]
            birth_ok = not formatted_birth or report["form_state"]["birth_matches"]
            if (document_ok and birth_ok) or attempt == 3:
                break
            report["form_retypes"] = attempt + 1
            if not document_ok:
                fill('input[name="niContribuinte"]', document)
            if not birth_ok:
                fill('input[name="dataNascimento"]', formatted_birth)
        assert report["form_state"]["document_matches"], "Form document mismatch"
        assert kind != "CPF" or report["form_state"]["birth_matches"], "Form birth mismatch"
        def dismiss_cookie_banner():
            # The LGPD banner can reappear after load and sit over the submit button.
            dismissed = sb.evaluate("""(() => {
                const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Aceitar'&&b.offsetParent);
                if (!b) return false; b.click(); return true;})()""")
            if dismissed:
                report["cookie_banner_dismissals"] = report.get("cookie_banner_dismissals", 0) + 1
                pump(0.6)

        def press(label: str, counter: str) -> dict:
            """Tap (Android) or click (desktop) a visible button, refusing to tap through an overlay."""
            sb.evaluate("""(() => {
                document.activeElement?.blur();
                const b=[...document.querySelectorAll('button')].filter(b=>b.textContent.trim()===""" + json.dumps(label) + """&&b.offsetParent).pop();
                window.__certidoesSubmit=window.__certidoesSubmit||{clicks:0,submits:0,new_issue_clicks:0};
                if (!b.__certidoesCounted) {
                    b.__certidoesCounted=true;
                    b.addEventListener('click',()=>window.__certidoesSubmit[""" + json.dumps(counter) + """]++,true);
                }
                const f=document.querySelector('form');
                if (f&&!f.__certidoesCounted) {f.__certidoesCounted=true;f.addEventListener('submit',()=>window.__certidoesSubmit.submits++,true);}
                b.scrollIntoView({block:'center',behavior:'instant'});
            })()""")
            pump(0.4)
            for attempt in range(3):
                dismiss_cookie_banner()
                point = sb.evaluate("""(() => {
                    const b=[...document.querySelectorAll('button')].filter(b=>b.textContent.trim()===""" + json.dumps(label) + """&&b.offsetParent).pop();
                    const r=b.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
                    return {x,y,covered:!b.contains(document.elementFromPoint(x,y))};
                })()""")
                if point.pop("covered"):
                    report.setdefault("covered_taps", []).append(label)
                    pump(0.6)
                    continue
                if android_port:
                    from mycdp import input_
                    send(input_.synthesize_tap_gesture(**point, gesture_source_type=input_.GestureSourceType.TOUCH))
                else:
                    sb.click(f'button:contains("{label}")', timeout=30)
                pump(0.4)
                events = sb.evaluate("window.__certidoesSubmit")
                if events[counter]:
                    return events
            raise receita.CertificateError("receita", "browser", "submit_tap_missed", f"Button {label!r} did not receive the tap")

        def existing_certificate_dialog() -> bool:
            return sb.evaluate("""(() => /Certidão Válida Encontrada/i.test(document.body?.innerText||'')
                && [...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Emitir Nova Certidão'&&b.offsetParent))()""")

        dismiss_cookie_banner()
        report["submit_events"] = press("Emitir Certidão", "clicks")
        deadline = time.monotonic() + request.token_timeout_ms / 1000
        issued_page = False
        while receita.VERIFICAR_URL not in responses and time.monotonic() < deadline:
            pump()
            # CDP network events sometimes go silent on Android Chrome; the result page
            # only renders after verificar and Emissao succeeded, so it is proof on its own.
            if sb.evaluate("/certid[aã]o foi emitida com sucesso/i.test(document.body?.innerText||'')"):
                issued_page = report["issued_page_without_network"] = True
                break
            if existing_certificate_dialog():
                # The captcha already passed; Receita asks whether to reuse a still-valid certificate.
                report["existing_certificate_dialog"] = True
                report["submit_events"] = press("Emitir Nova Certidão", "new_issue_clicks")
                deadline = time.monotonic() + request.token_timeout_ms / 1000
                continue
            state = sb.evaluate("""(() => ({
                rejected:/requested URL was rejected/i.test(document.body?.innerText||''),
                portal_106:/106\\s*-\\s*\\d{2}\\/\\d{2}\\/\\d{4}/.test(document.body?.innerText||''),
                visual:[...document.querySelectorAll('iframe')].some(f=>{const r=f.getBoundingClientRect();return f.src.includes('hcaptcha.com')&&r.width>200&&r.height>200&&r.y>=0})}))()""")
            report["page_state"] = state
            if state["rejected"] or state["portal_106"] or state["visual"]:
                code = "portal_rejected" if state["rejected"] else "portal_hcaptcha_106" if state["portal_106"] else "visual_challenge_required"
                raise receita.CertificateError("receita", "challenge", code, "Official form did not complete verification")
        if receita.VERIFICAR_URL in responses:
            status, body = responses[receita.VERIFICAR_URL]
            receita._require_ok(SimpleNamespace(status_code=status, json=lambda: body), "validation")
        elif not issued_page:
            raise receita.CertificateError("receita", "challenge", "verification_timeout", "Official form verification timed out")
        pdf = None
        for _ in range(25):
            captured = sb.evaluate("window.__certidoesEmissao")
            if captured:
                try:
                    pdf = receita._emissao_pdf(SimpleNamespace(
                        status_code=captured["status"], json=lambda: json.loads(captured["text"])))
                    report["pdf_source"] = "page_response"
                except receita.CertificateError as error:
                    report["page_response_error"] = error.code
                break
            if not issued_page:
                break
            pump(0.2)
        for delay in (() if pdf else (*receita.EMISSAO_RETRY_DELAYS, None)):
            emission = loop.run_until_complete(sb.page.evaluate("""(async () => {
                const response=await fetch(""" + json.dumps(receita.EMISSAO_URL) + """,{
                  method:'POST',headers:{'Content-Type':'application/json'},
                  body:JSON.stringify(""" + json.dumps({key: payload[key] for key in ("ni", "tipoContribuinte", "dataNascimento")}) + """),
                  signal:AbortSignal.timeout(30000)});
                return {status:response.status,data:await response.json()};})()""", await_promise=True))
            try:
                pdf = receita._emissao_pdf(SimpleNamespace(status_code=emission["status"], json=lambda: emission["data"]))
                report["pdf_source"] = "refetch"
                break
            except receita.CertificateError as error:
                if error.code != "emissao_em_processamento" or delay is None:
                    raise
                pump(delay)
        destination = request.output_dir / "certificate.pdf"
        receita._save(pdf, destination)
        return destination
    except Exception:
        if not report.get("verification"):
            try:
                report["failure_state"] = sb.evaluate("""(() => ({
                    rejected:/requested URL was rejected/i.test(document.body?.innerText||''),
                    portal_106:/106\\s*-\\s*\\d{2}\\/\\d{2}\\/\\d{4}/.test(document.body?.innerText||''),
                    form_invalid:!!document.querySelector('form.ng-invalid'),
                    submit_events:window.__certidoesSubmit||null}))()""")
                sb.evaluate("document.querySelectorAll('input,textarea').forEach(e=>e.style.visibility='hidden')")
                sb.save_screenshot(str(request.output_dir / "failure.png"))
            except Exception as error:
                report["capture_error"] = type(error).__name__
        raise
    finally:
        sb.quit()
        report["playwright_imported"] = any(name == "playwright" or name.startswith("playwright.") for name in sys.modules)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("desktop", "android"), required=True)
    parser.add_argument("--android-port", type=int)
    parser.add_argument("--single-sdk", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"engine": "SeleniumBase Pure CDP", "device": args.device,
        "single_sdk": args.single_sdk, "os": platform.system(),
        "seleniumbase": version("seleniumbase"), "sdk_loads": [], "status": "running"}
    started = time.monotonic()
    document, birth = "", ""
    try:
        payload = (decrypt(os.environ["RECEITA_PAYLOAD"], os.environ["RECEITA_PAYLOAD_PRIVATE_KEY"])
            if os.environ.get("RECEITA_PAYLOAD") else
            {"documento": os.environ["LIVE_DOCUMENT"].strip(), "data_nascimento": os.environ["LIVE_BIRTH"].strip()})
        document, birth = payload["documento"], payload["data_nascimento"]
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::add-mask::{document}\n::add-mask::{birth}", flush=True)
        assert (args.device == "android") == bool(args.android_port), "Android requires its ADB-forwarded CDP port"
        assert not (args.output / "profile").exists(), "Fresh profile required"
        path = run(receita.ReceitaRequest(document, birth, output_dir=args.output,
                   profile_dir=args.output / "profile", captcha_retries=1, token_timeout_ms=60000),
                   android_port=args.android_port, single_sdk=args.single_sdk, report=report)
        report.update(validate_pdf(path, document), status="success")
        assert report.get("playwright_imported") is False, "Unexpected Playwright import"
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__,
            code=getattr(error, "code", None), stage=getattr(error, "stage", None))
    finally:
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        serialized = json.dumps(report, indent=2)
        assert not document or document not in serialized, "Unredacted document"
        assert not birth or birth not in serialized, "Unredacted birth"
        (args.output / "result.json").write_text(serialized + "\n")
        print(serialized, flush=True)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
