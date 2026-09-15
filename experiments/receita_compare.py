"""Capture a redacted Receita/hCaptcha browser trace for environment comparison."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from certidoes.sources import receita as r

SAFE_HEADERS = (
    "accept", "accept-language", "content-type", "origin", "referer",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "user-agent",
    "cache-control", "content-encoding", "content-length", "content-security-policy",
    "cross-origin-resource-policy", "location", "server", "vary", "x-frame-options",
)


def clean_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else parsed.path


def relevant(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return "hcaptcha.com" in host or host == "servicos.receitafederal.gov.br"


def selected(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): clean_url(value) if key.lower() == "location" else value
        for key, value in headers.items() if key.lower() in SAFE_HEADERS}


# JS installed via page.add_init_script (Playwright-native; verified to fire on
# CDP-connected pages, unlike a raw Page.addScriptToEvaluateOnNewDocument
# send which registers but never executes here). It runs before every
# document/frame load, including the hCaptcha iframes. It only overrides
# *string/numeric* signals; canvas pixels are deliberately untouched so the
# run records whether string-level spoofing is sufficient.
SPOOF_JS_TEMPLATE = """(() => {
  const VENDOR = __VENDOR__;
  const RENDERER = __RENDERER__;
  const maskGL = (proto) => {
    try {
      const original = proto.getParameter;
      const patched = function (param) {
        if (param === 37445) return VENDOR;
        if (param === 37446) return RENDERER;
        return original.call(this, param);
      };
      try { Object.defineProperty(patched, 'name', { value: 'getParameter' }); } catch (e) {}
      patched.toString = original.toString.bind(original);
      proto.getParameter = patched;
    } catch (e) {}
  };
  try { maskGL(WebGLRenderingContext.prototype); } catch (e) {}
  try { maskGL(WebGL2RenderingContext.prototype); } catch (e) {}
  const nav = Object.getPrototypeOf(navigator);
  try { Object.defineProperty(nav, 'deviceMemory', { configurable: true, get: () => __DEVICE_MEMORY__ }); } catch (e) {}
  try { Object.defineProperty(nav, 'hardwareConcurrency', { configurable: true, get: () => __HARDWARE_CONCURRENCY__ }); } catch (e) {}
  try { Object.defineProperty(window.screen, 'availHeight', { configurable: true, get: () => __AVAIL_HEIGHT__ }); } catch (e) {}
})();"""


def render_spoof_js(*, vendor: str, renderer: str, device_memory: int,
                     hardware_concurrency: int, avail_height: int) -> str:
    script = SPOOF_JS_TEMPLATE
    script = script.replace("__VENDOR__", json.dumps(vendor))
    script = script.replace("__RENDERER__", json.dumps(renderer))
    script = script.replace("__DEVICE_MEMORY__", str(int(device_memory)))
    script = script.replace("__HARDWARE_CONCURRENCY__", str(int(hardware_concurrency)))
    script = script.replace("__AVAIL_HEIGHT__", str(int(avail_height)))
    return script


def environment() -> dict:
    dns = {}
    for host in ("servicos.receitafederal.gov.br", "js.hcaptcha.com"):
        try:
            dns[host] = sorted({item[4][0] for item in socket.getaddrinfo(host, 443)})
        except OSError as exc:
            dns[host] = type(exc).__name__
    return {
        "os": platform.platform(), "python": platform.python_version(),
        "ci": {key: os.environ.get(key) for key in
            ("CI", "GITHUB_ACTIONS", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")
            if os.environ.get(key)},
        "packages": {name: importlib.metadata.version(name) for name in ("playwright", "curl_cffi")},
        "timezone": time.tzname, "proxyConfigured": any(os.environ.get(key) for key in
            ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")),
        "dns": dns,
    }


def fingerprint(page) -> dict:
    return page.evaluate("""async () => {
        const canvas=document.createElement('canvas'); canvas.width=300; canvas.height=60;
        const ctx=canvas.getContext('2d'); ctx.font='18px Arial'; ctx.fillText('Receita hCaptcha 123',10,30);
        const digest=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))))
            .map(x=>x.toString(16).padStart(2,'0')).join('');
        const gl=document.createElement('canvas').getContext('webgl');
        const ext=gl && gl.getExtension('WEBGL_debug_renderer_info');
        let uaData=null;
        if(navigator.userAgentData) uaData=await navigator.userAgentData.getHighEntropyValues(
            ['architecture','bitness','brands','fullVersionList','mobile','model','platform','platformVersion','uaFullVersion']);
        return {
            userAgent:navigator.userAgent, platform:navigator.platform, uaData,
            language:navigator.language, languages:navigator.languages,
            timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
            hardwareConcurrency:navigator.hardwareConcurrency, deviceMemory:navigator.deviceMemory,
            maxTouchPoints:navigator.maxTouchPoints, webdriver:navigator.webdriver,
            pdfViewerEnabled:navigator.pdfViewerEnabled,
            plugins:[...navigator.plugins].map(x=>x.name), mimeTypes:[...navigator.mimeTypes].map(x=>x.type),
            screen:{width:screen.width,height:screen.height,availWidth:screen.availWidth,availHeight:screen.availHeight,
                colorDepth:screen.colorDepth,pixelDepth:screen.pixelDepth,dpr:devicePixelRatio},
            window:{innerWidth,innerHeight,outerWidth,outerHeight},
            webgl:{available:!!gl,vendor:ext?gl.getParameter(ext.UNMASKED_VENDOR_WEBGL):null,
                renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null},
            canvasHash:await digest(canvas.toDataURL()),
            fonts:Object.fromEntries(['Arial','Times New Roman','Courier New','Noto Sans','Segoe UI','Liberation Sans']
                .map(name=>[name,document.fonts.check(`16px "${name}"`)])),
        };
    }""")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--incognito", action="store_true")
    parser.add_argument("--token", action="store_true", help="Execute hCaptcha without validating or issuing")
    parser.add_argument("--verify", action="store_true", help="Validate with LIVE_DOCUMENT and LIVE_BIRTH; never issue")
    parser.add_argument("--transport", choices=("browser", "curl"), default="browser")
    parser.add_argument("--chrome-arg", action="append", default=[], help="Add one Chrome flag for a controlled comparison")
    parser.add_argument("--remove-chrome-arg", action="append", default=[], help="Remove one default Chrome flag")
    parser.add_argument("--accept-language", help="Set Chrome profile language before startup")
    parser.add_argument("--windows-ua", action="store_true", help="Diagnostic: send the observed Windows Chrome 152 UA and Client Hints")
    parser.add_argument("--spoof-pack", action="store_true",
        help="Install string-level spoofs (WebGL vendor/renderer, deviceMemory, hardwareConcurrency, availHeight) as an init script before navigation")
    parser.add_argument("--spoof-webgl-vendor", default="Google Inc. (AMD)")
    parser.add_argument("--spoof-webgl-renderer",
        default="ANGLE (AMD, AMD Radeon(TM) Graphics (0x00001638) Direct3D11 vs_5_0 ps_5_0, D3D11)")
    parser.add_argument("--spoof-device-memory", type=int, default=32)
    parser.add_argument("--spoof-hardware-concurrency", type=int, default=16)
    parser.add_argument("--spoof-avail-height", type=int, default=1032)
    args = parser.parse_args()
    request = r.ReceitaRequest(os.environ["LIVE_DOCUMENT"], os.environ["LIVE_BIRTH"]) if args.verify else None
    document, kind, contributor = r._normalize(request) if request else (None, "CPF", None)
    started = time.monotonic()
    events: list[dict] = []
    frame_events: list[dict] = []
    trace: dict = {"result": {}, "events": events, "frameEvents": frame_events,
                   "environment": environment()}
    original_popen = subprocess.Popen
    if args.chrome_arg or args.remove_chrome_arg:
        def launch(command, **kwargs):
            if command[0] == r._chrome_path(None):
                command = [command[0], *args.chrome_arg,
                    *(flag for flag in command[1:] if flag not in args.remove_chrome_arg)]
            return original_popen(command, **kwargs)
        subprocess.Popen = launch
    chrome = r.CdpChrome(r._chrome_path(None), debug_port=9336, profile_dir=args.profile, incognito=args.incognito)
    if args.accept_language:
        preferences = chrome.profile_dir / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True, exist_ok=True)
        preferences.write_text(json.dumps({"intl": {"accept_languages": args.accept_language,
            "selected_languages": args.accept_language}}), encoding="utf-8")
    try:
        page = chrome.start()
        trace["arguments"] = chrome._process.args  # exact launch command, including Docker-only flags
        cdp = page.context.new_cdp_session(page)
        if args.windows_ua:
            brands = [{"brand": "Chromium", "version": "152"},
                      {"brand": "Not?A_Brand", "version": "24"},
                      {"brand": "Google Chrome", "version": "152"}]
            cdp.send("Network.setUserAgentOverride", {
                "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                "acceptLanguage": "pt-BR,pt,en-US,en", "platform": "Win32",
                "userAgentMetadata": {"brands": brands, "fullVersionList": [
                    {"brand": item["brand"], "version": "152.0.7977.84" if item["brand"] != "Not?A_Brand" else "24.0.0.0"}
                    for item in brands], "fullVersion": "152.0.7977.84", "platform": "Windows",
                    "platformVersion": "19.0.0", "architecture": "x86", "model": "", "mobile": False,
                    "bitness": "64", "wow64": False},
            })
        cdp.send("Network.enable")
        if args.spoof_pack:
            script = render_spoof_js(
                vendor=args.spoof_webgl_vendor,
                renderer=args.spoof_webgl_renderer,
                device_memory=args.spoof_device_memory,
                hardware_concurrency=args.spoof_hardware_concurrency,
                avail_height=args.spoof_avail_height,
            )
            page.add_init_script(script)
            trace["spoofPack"] = {
                "webglVendor": args.spoof_webgl_vendor,
                "webglRenderer": args.spoof_webgl_renderer,
                "deviceMemory": args.spoof_device_memory,
                "hardwareConcurrency": args.spoof_hardware_concurrency,
                "availHeight": args.spoof_avail_height,
            }
        request_ids: dict[str, int] = {}

        def on_cdp_request(item) -> None:
            if relevant(item["request"]["url"]):
                request_ids[item["requestId"]] = len(events)
                initiator = item.get("initiator", {})
                events.append({"atMs": round((time.monotonic()-started)*1000), "type": "request",
                    "method": item["request"]["method"], "url": clean_url(item["request"]["url"]),
                    "initiator": initiator.get("type"),
                    "initiatorUrl": clean_url(initiator["url"]) if initiator.get("url") else None,
                    "headers": selected(item["request"].get("headers", {})),
                    "postBytes": len(item["request"].get("postData", ""))})

        def on_cdp_request_extra(item) -> None:
            if item["requestId"] in request_ids:
                events[request_ids[item["requestId"]]]["wireHeaders"] = selected(item.get("headers", {}))

        def on_cdp_response(item) -> None:
            if item["requestId"] in request_ids:
                response = item["response"]
                events.append({"atMs": round((time.monotonic()-started)*1000), "type": "response",
                    "status": response["status"], "url": clean_url(response["url"]),
                    "headers": selected(response.get("headers", {})),
                    "protocol": response.get("protocol"), "remoteIp": response.get("remoteIPAddress"),
                    "fromCache": response.get("fromDiskCache") or response.get("fromServiceWorker")})

        cdp.on("Network.requestWillBeSent", on_cdp_request)
        cdp.on("Network.requestWillBeSentExtraInfo", on_cdp_request_extra)
        cdp.on("Network.responseReceived", on_cdp_response)

        def on_frame_request(item) -> None:
            if relevant(item.url):
                frame_events.append({"type": "request", "method": item.method,
                    "resourceType": item.resource_type, "url": clean_url(item.url),
                    "headers": selected(item.headers), "postBytes": len(item.post_data_buffer or b"")})

        def on_frame_response(item) -> None:
            if relevant(item.url):
                frame_events.append({"type": "response", "status": item.status,
                    "url": clean_url(item.url), "headers": selected(item.headers)})

        page.on("request", on_frame_request)
        page.on("response", on_frame_response)

        page.goto(r.PAGE_URLS[kind], wait_until="domcontentloaded", timeout=30000)
        trace["fingerprint"] = fingerprint(page)
        trace["browser"] = cdp.send("Browser.getVersion")
        page.wait_for_selector(r.WIDGET_SELECTOR, state="attached", timeout=30000)
        trace["page"] = page.evaluate("""() => ({readyState:document.readyState,
            widgetCount:document.querySelectorAll('[data-hcaptcha-widget-id]').length,
            hcaptchaReady:!!window.hcaptcha && typeof window.hcaptcha.execute === 'function',
            hcaptchaFrames:[...document.querySelectorAll('iframe')].filter(x=>x.src.includes('hcaptcha.com')).length})""")
        if args.verify or args.token:
            token = r._token(page, 30000)
            trace["result"]["tokenLength"] = len(token)
        if args.verify:
            payload = r._payload(request, document, kind, contributor)
            if args.transport == "browser":
                result = page.evaluate("""async ({url,payload,token}) => {
                    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','x-captcha-token':token},
                        body:JSON.stringify(payload),signal:AbortSignal.timeout(30000)});
                    return {status:response.status,data:await response.json()};
                }""", {"url": r.VERIFICAR_URL, "payload": payload, "token": token})
            else:
                with r.requests.Session(impersonate="chrome") as session:
                    response = r._post(session, r.VERIFICAR_URL, payload, token=token,
                        user_agent=trace["fingerprint"]["userAgent"], timeout=30)
                    result = {"status": response.status_code, "data": response.json()}
                    trace["curlResponseHeaders"] = selected(dict(response.headers))
            trace["result"] = {"tokenLength": len(token), "http": result["status"],
                "transport": args.transport, "status": result["data"].get("status"),
                "validation": result["data"].get("statusValidacao"),
                "responseKeys": sorted(result["data"])}
            return 0 if result["status"] == 200 else 1
        return 0
    except Exception as exc:
        trace["error"] = {"type": type(exc).__name__, "message": str(exc).splitlines()[0][:200]}
        return 1
    finally:
        trace["elapsedMs"] = round((time.monotonic()-started)*1000)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        chrome.close()
        subprocess.Popen = original_popen


if __name__ == "__main__":
    raise SystemExit(main())
