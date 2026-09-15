"""Validate the legacy SeleniumBase UC token against Receita end-to-end."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from curl_cffi import requests
from seleniumbase import cdp_driver

PAGE_URL = "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cpf"
VERIFY_URL = "https://servicos.receitafederal.gov.br/servico/certidoes/api/Emissao/verificar"
EMIT_URL = "https://servicos.receitafederal.gov.br/servico/certidoes/api/Emissao"
BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "pt-BR,pt;q=0.6",
    "content-type": "application/json",
    "origin": "https://servicos.receitafederal.gov.br",
    "referer": "https://servicos.receitafederal.gov.br/servico/certidoes/",
    "sec-ch-ua": '"Not:A-Brand";v="99", "Brave";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "sec-gpc": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


async def main() -> int:
    document = os.environ["LIVE_DOCUMENT"]
    birth = os.environ["LIVE_BIRTH"]
    driver = await cdp_driver.start_async(uc=True, headless=False)
    try:
        page = await driver.get(PAGE_URL, lang="pt-BR")
        await page.select("[data-hcaptcha-widget-id]", timeout=30)
        await page.evaluate("""(() => {
            window.__token = null;
            window.__error = null;
            (async () => {
                try {
                    const element = document.querySelector('[data-hcaptcha-widget-id]');
                    const result = await hcaptcha.execute(element.dataset.hcaptchaWidgetId, {async: true});
                    window.__token = result && typeof result === 'object' ? result.response : result;
                } catch (error) { window.__error = String(error); }
            })();
        })()""")
        token = None
        for _ in range(60):
            token = await page.evaluate("window.__token")
            error = await page.evaluate("window.__error")
            if token or error:
                break
            await asyncio.sleep(0.5)
        print(json.dumps({"stage": "token", "present": bool(token), "length": len(token or "")}), flush=True)
        if not token:
            return 1
        payload = {"ni": document, "tipoContribuinte": "PF", "dataNascimento": birth, "tipoContribuinteEnum": "CPF"}
        session = requests.Session(impersonate="chrome120")
        verify_response = session.post(VERIFY_URL, json=payload, headers={**BASE_HEADERS, "x-captcha-token": token}, timeout=30)
        verify = verify_response.json()
        print(json.dumps({"stage": "verify", "http": verify_response.status_code,
            "status": verify.get("status"), "validation": verify.get("statusValidacao")}), flush=True)
        if verify_response.status_code != 200:
            return 1
        emit_payload = {key: payload[key] for key in ("ni", "tipoContribuinte", "dataNascimento")}
        cookies = "; ".join(f"{key}={value}" for key, value in verify_response.cookies.items())
        emit_response = session.post(EMIT_URL, json=emit_payload, headers={**BASE_HEADERS, "cookie": cookies}, timeout=30)
        emission = emit_response.json()
        encoded = emission.get("pdf")
        print(json.dumps({"stage": "emission", "http": emit_response.status_code,
            "status": emission.get("statusEmissao"), "pdf": bool(encoded)}), flush=True)
        if not encoded:
            return 1
        pdf = base64.b64decode(encoded, validate=True)
        if not pdf.startswith(b"%PDF-"):
            return 1
        output = Path("/app/runs/docker_receita_seleniumbase/certificate.pdf")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(pdf)
        return 0
    finally:
        driver.stop(deconstruct=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
