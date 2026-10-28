"""SeleniumBase UC probe: same Receita verify steps without CDP/Playwright.

Fingerprint -> hCaptcha token -> POST /verificar. Never issues.
Requires LIVE_DOCUMENT and LIVE_BIRTH env (GitHub secrets). Writes a redacted
JSON trace compatible with receita_compare output shape.
On Linux without a display it starts its own Xvfb (GitHub runners have none).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

FP_JS = """const done = arguments[arguments.length - 1];
(async () => {
  const canvas = document.createElement('canvas'); canvas.width = 300; canvas.height = 60;
  const ctx = canvas.getContext('2d'); ctx.font = '18px Arial'; ctx.fillText('Receita hCaptcha 123', 10, 30);
  const digest = async v => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(v)))).map(x => x.toString(16).padStart(2, '0')).join('');
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  let uaData = null;
  if (navigator.userAgentData) uaData = await navigator.userAgentData.getHighEntropyValues(
      ['architecture','bitness','brands','fullVersionList','mobile','model','platform','platformVersion','uaFullVersion']);
  done({
    userAgent: navigator.userAgent, platform: navigator.platform, uaData,
    language: navigator.language, languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    hardwareConcurrency: navigator.hardwareConcurrency, deviceMemory: navigator.deviceMemory,
    maxTouchPoints: navigator.maxTouchPoints, webdriver: navigator.webdriver,
    pdfViewerEnabled: navigator.pdfViewerEnabled,
    plugins: [...navigator.plugins].map(x => x.name), mimeTypes: [...navigator.mimeTypes].map(x => x.type),
    screen: {width: screen.width, height: screen.height, availWidth: screen.availWidth, availHeight: screen.availHeight,
        colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth, dpr: devicePixelRatio},
    window: {innerWidth, innerHeight, outerWidth, outerHeight},
    webgl: {available: !!gl, vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
        renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null},
    canvasHash: await digest(canvas.toDataURL()),
    fonts: Object.fromEntries(['Arial','Times New Roman','Courier New','Noto Sans','Segoe UI','Liberation Sans']
        .map(name => [name, document.fonts.check(`16px "${name}"`)])),
  });
})();"""


def ensure_display() -> subprocess.Popen | None:
    if sys.platform != "linux" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    display = 97
    while os.path.exists(f"/tmp/.X{display}-lock"):
        display += 1
    proc = subprocess.Popen(["Xvfb", f":{display}", "-screen", "0", "1280x900x24"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = f":{display}"
    os.environ.pop("WAYLAND_DISPLAY", None)
    time.sleep(2)
    return proc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from certidoes.sources import receita as r

    request = r.ReceitaRequest(os.environ["LIVE_DOCUMENT"], os.environ["LIVE_BIRTH"])
    document, kind, contributor = r._normalize(request)
    payload = r._payload(request, document, kind, contributor)

    trace: dict = {"result": {}, "environment": {
        "os": sys.platform,
        "ci": {key: os.environ.get(key) for key in
               ("CI", "GITHUB_ACTIONS", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")
               if os.environ.get(key)},
        "stack": "seleniumbase-uc-webdriver",
    }}
    xvfb = ensure_display()
    started = time.monotonic()
    http_status = 1
    try:
        from seleniumbase import Driver

        kwargs = {}
        if sys.platform == "linux":
            kwargs["chromium_arg"] = "--ozone-platform=x11,--no-sandbox,--disable-dev-shm-usage"
        driver = Driver(uc=True, headless=False, browser="chrome",
                        user_data_dir="/tmp/receita-selenium-profile", **kwargs)
        try:
            driver.set_script_timeout(120)
            trace["browser"] = {"version": driver.capabilities.get("browserVersion")}
            driver.get(r.PAGE_URLS[kind])
            deadline = time.time() + 30
            while time.time() < deadline:
                if driver.execute_script(
                        "return !!window.hcaptcha && typeof window.hcaptcha.execute === 'function'"
                        " && document.querySelectorAll('[data-hcaptcha-widget-id]').length > 0"):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("hCaptcha widget not ready")
            trace["fingerprint"] = driver.execute_async_script(FP_JS)
            token = driver.execute_async_script("""
              const done = arguments[arguments.length - 1];
              const id = document.querySelector('[data-hcaptcha-widget-id]').getAttribute('data-hcaptcha-widget-id');
              Promise.resolve(window.hcaptcha.execute(id, {async: true}))
                .then(v => done((v && typeof v === 'object' ? v.response : v) || ''))
                .catch(() => done(''));""")
            if not token:
                raise RuntimeError("empty token from hcaptcha.execute")
            res = driver.execute_async_script("""
              const done = arguments[arguments.length - 1];
              const [url, payload, token] = arguments;
              fetch(url, {method:'POST', headers:{'Content-Type':'application/json','x-captcha-token':token},
                  body: JSON.stringify(payload)})
                .then(async resp => done({status: resp.status, data: await resp.json()}))
                .catch(e => done({status: -1, data: {error: String(e)}}));""",
                                              r.VERIFICAR_URL, payload, token)
            trace["result"] = {"tokenLength": len(token), "http": res["status"],
                                "transport": "seleniumbase-uc",
                                "status": (res.get("data") or {}).get("status"),
                                "validation": (res.get("data") or {}).get("statusValidacao")}
            http_status = 0 if res["status"] == 200 else 1
        finally:
            driver.quit()
    except Exception as exc:
        trace["error"] = {"type": type(exc).__name__, "message": str(exc).splitlines()[0][:200]}
    finally:
        trace["elapsedMs"] = round((time.monotonic() - started) * 1000)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        if xvfb is not None:
            xvfb.terminate()
    return http_status


if __name__ == "__main__":
    raise SystemExit(main())
