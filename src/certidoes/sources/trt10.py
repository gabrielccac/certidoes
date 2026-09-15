from __future__ import annotations

import argparse
import base64
from collections import Counter
from functools import lru_cache
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import requests

from certidoes.errors import CertificateError
from certidoes.models import CertificateResult

SOURCE = "trt10"
EMISSION_URL = "https://pje.trt10.jus.br/certidoes/trabalhista/emissao"
CERTIFICATE_PAGE_URL = "https://pje.trt10.jus.br/certidoes/trabalhista/certidao"
EMISSAO_API_URL = "https://pje.trt10.jus.br/pje-certidoes-api/api/certidoes/trabalhistas/emissao"
CAPTCHA_API_URL = "https://pje.trt10.jus.br/pje-certidoes-api/api/captcha"
CERTIFICATE_API_URL = "https://pje.trt10.jus.br/pje-certidoes-api/api/certidoes/trabalhistas"
_ORIGIN = "https://pje.trt10.jus.br"
_CRITERIA = {"RAIZ_DE_CNPJ": "Raiz do CNPJ", "CPF": "CPF", "NOME": "Nome completo"}


@dataclass(frozen=True)
class Trt10Request:
    documento: str = ""
    criterio: str = "CPF"
    nome: str = ""
    output_dir: Path = Path("runs/trt10")
    navigation_timeout_ms: int = 30_000
    render_timeout_ms: int = 15_000
    request_timeout_seconds: int = 30
    captcha_retries: int = 3
    headless: bool = True


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _validate(request: Trt10Request) -> str:
    if request.criterio not in _CRITERIA:
        raise CertificateError(SOURCE, "input", "criterio_invalido", f"Unsupported TRT10 criterion: {request.criterio}")
    if request.criterio == "NOME":
        if not request.nome.strip():
            raise CertificateError(SOURCE, "input", "nome_obrigatorio", "nome is required for NOME criterion")
        documento = ""
    else:
        documento = _digits_only(request.documento)
        expected = 11 if request.criterio == "CPF" else 8
        if len(documento) != expected:
            raise CertificateError(SOURCE, "input", "documento_invalido", f"{request.criterio} must contain {expected} digits")
    if min(request.navigation_timeout_ms, request.render_timeout_ms,
           request.request_timeout_seconds, request.captcha_retries) <= 0:
        raise CertificateError(SOURCE, "input", "timeout_invalido", "timeouts must be greater than zero")
    return documento


def _payload(request: Trt10Request) -> dict[str, str]:
    documento = _digits_only(request.documento)
    if request.criterio == "CPF" and len(documento) == 11:
        documento = f"{documento[:3]}.{documento[3:6]}.{documento[6:9]}-{documento[9:]}"
    return {
        "criterioDeEmissao": request.criterio,
        "nome": request.nome.strip(),
        "numeroDoDocumento": documento if request.criterio != "NOME" else "",
        "respostaDoReCaptcha": "",
    }


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Origin": _ORIGIN, "Referer": EMISSION_URL, "Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    return headers


@lru_cache(maxsize=1)
def _ocr_models():
    import ddddocr

    return (
        ddddocr.DdddOcr(show_ad=False),
        ddddocr.DdddOcr(show_ad=False, beta=True),
    )


def _solve_internal_captcha(session, payload: dict[str, str], timeout: int) -> str:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise CertificateError(SOURCE, "challenge", "ocr_ausente", "Install ddddocr to solve the TRT10 internal CAPTCHA") from exc
    try:
        response = session.post(CAPTCHA_API_URL, json=payload, headers=_headers(), timeout=timeout)
        challenge = response.json()
    except ValueError as exc:
        raise CertificateError(SOURCE, "challenge", "resposta_nao_json", "TRT10 CAPTCHA endpoint returned non-JSON data", retryable=True) from exc
    except Exception as exc:
        raise CertificateError(SOURCE, "challenge", "rede_indisponivel", "TRT10 CAPTCHA request failed", retryable=True) from exc
    if response.status_code >= 400 or not challenge.get("token") or not challenge.get("imagem"):
        raise CertificateError(SOURCE, "challenge", "captcha_indisponivel", "TRT10 did not return a CAPTCHA challenge", retryable=True, upstream_status=response.status_code)
    try:
        image = cv2.imdecode(np.frombuffer(base64.b64decode(challenge["imagem"]), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("invalid image")
        height = image.shape[0]
        variants = (
            image[int(height * 0.20) : int(height * 0.85), :],
            image[int(height * 0.25) : int(height * 0.85), :],
            image[int(height * 0.30) : int(height * 0.85), :],
            image[int(height * 0.35) : int(height * 0.90), :],
        )
        recognized = []
        for model in _ocr_models():
            for variant in variants:
                encoded = cv2.imencode(".png", variant)[1].tobytes()
                answer = model.classification(encoded).strip().lower()
                if re.fullmatch(r"[a-z0-9]{6}", answer):
                    recognized.append(answer)
    except Exception as exc:
        raise CertificateError(SOURCE, "challenge", "ocr_falhou", "TRT10 CAPTCHA image could not be recognized", retryable=True) from exc
    answers = [answer for answer, _ in Counter(recognized).most_common()]
    if not answers:
        raise CertificateError(SOURCE, "challenge", "ocr_invalido", "TRT10 CAPTCHA OCR returned an invalid answer", retryable=True)
    for answer in answers:
        try:
            solution = session.post(
                f"{CAPTCHA_API_URL}/solucao",
                json=payload,
                headers=_headers({"PJe-Captcha-Token-Desafio": challenge["token"], "PJe-Captcha-Resposta-Desafio": answer}),
                timeout=timeout,
            )
        except Exception as exc:
            raise CertificateError(SOURCE, "challenge", "rede_indisponivel", "TRT10 CAPTCHA solution request failed", retryable=True) from exc
        access = solution.headers.get("PJe-Captcha-Token-Acesso")
        if solution.status_code < 400 and access:
            return access
    raise CertificateError(
        SOURCE, "challenge", "captcha_rejeitado", "TRT10 rejected the CAPTCHA answers",
        retryable=True, upstream_status=solution.status_code,
    )


def _emit_api(request: Trt10Request) -> int:
    payload = _payload(request)
    session = requests.Session(impersonate="chrome120")
    try:
        access = _solve_internal_captcha(session, payload, request.request_timeout_seconds)
        response = session.post(
            EMISSAO_API_URL,
            json=payload,
            headers=_headers({"PJe-Captcha-Token-Acesso": access}),
            timeout=request.request_timeout_seconds,
        )
        data = response.json()
    except ValueError as exc:
        raise CertificateError(SOURCE, "emissao", "resposta_nao_json", "TRT10 emission returned non-JSON data", retryable=True) from exc
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError(SOURCE, "emissao", "rede_indisponivel", "TRT10 emission request failed", retryable=True) from exc
    if response.status_code >= 400 or data.get("codigoErro"):
        code = data.get("codigoErro", f"HTTP_{response.status_code}")
        raise CertificateError(SOURCE, "validation", "documento_rejeitado", f"TRT10 emission failed ({code}): {data.get('mensagem', '')}", upstream_status=response.status_code)
    try:
        return int(data["codigo"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CertificateError(SOURCE, "emissao", "codigo_ausente", "TRT10 emission returned no request code") from exc


def _render_pdf(page, destination: Path, timeout_ms: int) -> None:
    try:
        page.wait_for_selector("img.brasao-certidao", state="visible", timeout=timeout_ms)
        cdp = page.context.new_cdp_session(page)
        result = cdp.send("Page.printToPDF", {"landscape": False, "printBackground": True, "preferCSSPageSize": True})
        content = base64.b64decode(result["data"])
    except Exception as exc:
        raise CertificateError(SOURCE, "artifact", "pdf_geracao_falhou", "TRT10 certificate page could not be rendered", retryable=True) from exc
    if not content.startswith(b"%PDF-"):
        raise CertificateError(SOURCE, "artifact", "pdf_invalido", "TRT10 did not produce a PDF")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as handle:
        handle.write(content)
        partial = Path(handle.name)
    partial.replace(destination)


def _emit_with_retries(request: Trt10Request) -> int:
    last_error: CertificateError | None = None
    for _ in range(request.captcha_retries):
        try:
            return _emit_api(request)
        except CertificateError as exc:
            if not exc.retryable or exc.stage != "challenge":
                raise
            last_error = exc
    raise last_error or CertificateError(
        SOURCE, "challenge", "captcha_falhou", "TRT10 CAPTCHA could not be solved",
        retryable=True,
    )


def generate(request: Trt10Request) -> CertificateResult:
    documento = _validate(request)
    codigo = _emit_with_retries(request)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CertificateError(SOURCE, "browser", "playwright_ausente", "Playwright is required for TRT10") from exc

    destination = request.output_dir / f"Certidao TRT10 - {documento or 'nome'}.pdf"
    try:
        with sync_playwright() as playwright:
            launch_options = {"headless": request.headless}
            if os.environ.get("CHROME_PATH"):
                launch_options["executable_path"] = os.environ["CHROME_PATH"]
            browser = playwright.chromium.launch(**launch_options)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.goto(f"{CERTIFICATE_PAGE_URL}/{codigo}", wait_until="networkidle", timeout=request.navigation_timeout_ms)
                page.wait_for_load_state("networkidle", timeout=request.navigation_timeout_ms)
                _render_pdf(page, destination, request.render_timeout_ms)
                url = page.url
            finally:
                browser.close()
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError(SOURCE, "browser", "workflow_falhou", "TRT10 browser workflow failed", retryable=True) from exc
    return CertificateResult(SOURCE, "CPF" if request.criterio == "CPF" else request.criterio, destination.name, destination, {"codigo": codigo, "url": url})


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one TRT10 labor certificate")
    parser.add_argument("--documento", default="")
    parser.add_argument("--criterio", choices=sorted(_CRITERIA), default="CPF")
    parser.add_argument("--nome", default="")
    parser.add_argument("--output-dir", type=Path, default=Path("runs/trt10"))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    try:
        result = generate(
            Trt10Request(
                documento=args.documento,
                criterio=args.criterio,
                nome=args.nome,
                output_dir=args.output_dir,
                headless=not args.headed,
            )
        )
    except CertificateError as exc:
        print(json.dumps({"status": "error", "source": exc.source, "stage": exc.stage, "code": exc.code, "message": exc.message}))
        return 1
    print(json.dumps({"status": "success", "source": result.source, "filename": result.filename, "pdf_path": str(result.pdf_path), "bytes": result.pdf_path.stat().st_size, "metadata": result.metadata}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
