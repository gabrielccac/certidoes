from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests

from certidoes.browser import CdpChrome
from certidoes.errors import CertificateError
from certidoes.models import CertificateResult

SOURCE = "trf1"
SOLICITACAO_URL = "https://sistemas.trf1.jus.br/certidao/#/solicitacao"
CERTIDAO_API_URL = "https://sistemas.trf1.jus.br/pgp-api/api/certidao"
RECAPTCHA_CO = "aHR0cHM6Ly9zaXN0ZW1hcy50cmYxLmp1cy5icjo0NDM."
TIPOS_CERTIDAO = {
    1: {"id": 1, "descricao": "Civel", "stWeb": "S"},
    2: {"id": 2, "descricao": "Criminal", "stWeb": "S"},
    3: {"id": 3, "descricao": "Eleitoral", "stWeb": "S"},
}
ORGAOS = {
    "AC": {"nome": "SECAO JUDICIARIA DO ESTADO DO ACRE", "sigla": "AC", "grau": 1, "codigoSecaoSubsecao": 3000, "uf": "AC", "situacao": "ATIVO"},
    "AM": {"nome": "SECAO JUDICIARIA DO ESTADO DO AMAZONAS", "sigla": "AM", "grau": 1, "codigoSecaoSubsecao": 3200, "uf": "AM", "situacao": "ATIVO"},
    "AP": {"nome": "SECAO JUDICIARIA DO ESTADO DO AMAPA", "sigla": "AP", "grau": 1, "codigoSecaoSubsecao": 3100, "uf": "AP", "situacao": "ATIVO"},
    "BA": {"nome": "SECAO JUDICIARIA DO ESTADO DA BAHIA", "sigla": "BA", "grau": 1, "codigoSecaoSubsecao": 3300, "uf": "BA", "situacao": "ATIVO"},
    "DF": {"nome": "SECAO JUDICIARIA DO DISTRITO FEDERAL", "sigla": "DF", "grau": 1, "codigoSecaoSubsecao": 3400, "uf": "DF", "situacao": "ATIVO"},
    "GO": {"nome": "SECAO JUDICIARIA DO ESTADO DE GOIAS", "sigla": "GO", "grau": 1, "codigoSecaoSubsecao": 3500, "uf": "GO", "situacao": "ATIVO"},
    "MA": {"nome": "SECAO JUDICIARIA DO ESTADO DO MARANHAO", "sigla": "MA", "grau": 1, "codigoSecaoSubsecao": 3700, "uf": "MA", "situacao": "ATIVO"},
    "MT": {"nome": "SECAO JUDICIARIA DO ESTADO DE MATO GROSSO", "sigla": "MT", "grau": 1, "codigoSecaoSubsecao": 3600, "uf": "MT", "situacao": "ATIVO"},
    "PA": {"nome": "SECAO JUDICIARIA DO ESTADO DO PARA", "sigla": "PA", "grau": 1, "codigoSecaoSubsecao": 3900, "uf": "PA", "situacao": "ATIVO"},
    "PI": {"nome": "SECAO JUDICIARIA DO ESTADO DO PIAUI", "sigla": "PI", "grau": 1, "codigoSecaoSubsecao": 4000, "uf": "PI", "situacao": "ATIVO"},
    "RO": {"nome": "SECAO JUDICIARIA DO ESTADO DE RONDONIA", "sigla": "RO", "grau": 1, "codigoSecaoSubsecao": 4100, "uf": "RO", "situacao": "ATIVO"},
    "RR": {"nome": "SECAO JUDICIARIA DO ESTADO DE RORAIMA", "sigla": "RR", "grau": 1, "codigoSecaoSubsecao": 4200, "uf": "RR", "situacao": "ATIVO"},
    "TO": {"nome": "SECAO JUDICIARIA DO ESTADO DO TOCANTINS", "sigla": "TO", "grau": 1, "codigoSecaoSubsecao": 4300, "uf": "TO", "situacao": "ATIVO"},
    "TRF1": {"nome": "TRIBUNAL REGIONAL FEDERAL DA 1A REGIAO", "sigla": "TRF1", "grau": 2, "codigoSecaoSubsecao": 100, "uf": "TRF", "situacao": "ATIVO"},
}


@dataclass(frozen=True)
class Trf1Request:
    documento: str
    is_pessoa_fisica: bool = True
    tipo_certidao_id: int = 1
    orgao_siglas: tuple[str, ...] = ("DF",)
    output_dir: Path = Path("runs/trf1")
    page_timeout_ms: int = 20000
    debug_port: int = 9334
    profile_dir: Path = Path("experiments/profile")
    chrome_path: str | None = None


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _validate(request: Trf1Request) -> str:
    documento = _digits_only(request.documento)
    expected = 11 if request.is_pessoa_fisica else 14
    if len(documento) != expected:
        kind = "CPF" if request.is_pessoa_fisica else "CNPJ"
        raise CertificateError(SOURCE, "input", "documento_invalido", f"{kind} must contain {expected} digits")
    if request.tipo_certidao_id not in TIPOS_CERTIDAO:
        raise CertificateError(SOURCE, "input", "tipo_certidao_invalido", "Unsupported TRF1 certificate type")
    if not request.orgao_siglas or any(sigla.upper() not in ORGAOS for sigla in request.orgao_siglas):
        raise CertificateError(SOURCE, "input", "orgao_invalido", "At least one valid TRF1 section is required")
    if request.page_timeout_ms <= 0 or request.debug_port <= 0:
        raise CertificateError(SOURCE, "input", "config_invalida", "timeout and debug port must be greater than zero")
    return documento


def _chrome_path(configured: str | None) -> str:
    candidates = [configured, os.environ.get("CHROME_PATH"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise CertificateError(SOURCE, "browser", "chrome_nao_encontrado", "Google Chrome executable was not found")


def _recaptcha_metadata(page, timeout_ms: int) -> tuple[str, str]:
    try:
        page.wait_for_selector("iframe[src*='recaptcha/api2/anchor']", timeout=timeout_ms)
    except Exception as exc:
        raise CertificateError(SOURCE, "challenge", "recaptcha_ausente", "TRF1 reCAPTCHA frame was not found", retryable=True) from exc
    def read_url(url: str) -> tuple[str, str] | None:
        if "recaptcha/api2/anchor" not in url:
            return None
        query = parse_qs(urlparse(url).query)
        key = (query.get("k") or [""])[0]
        version = (query.get("v") or [""])[0]
        return (key, version) if key and version else None

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for index in range(page.locator("iframe").count()):
            match = read_url(page.locator("iframe").nth(index).get_attribute("src") or "")
            if match:
                return match
        for frame in page.frames:
            match = read_url(frame.url)
            if match:
                return match
        time.sleep(0.2)
    raise CertificateError(SOURCE, "challenge", "recaptcha_invalido", "TRF1 reCAPTCHA metadata was incomplete", retryable=True)


def _fill_and_submit(page, documento: str, is_pessoa_fisica: bool) -> None:
    try:
        field = page.locator("#mat-input-0")
        field.fill(documento)
        page.get_by_role("button", name=re.compile("Emitir", re.IGNORECASE)).first.click()
        page.wait_for_timeout(1200)
        errors = page.locator("mat-error").all_inner_texts()
    except Exception as exc:
        raise CertificateError(SOURCE, "browser", "formulario_falhou", "TRF1 certificate form could not be submitted", retryable=True) from exc
    normalized = [unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower() for value in errors]
    expected = "cpf invalido" if is_pessoa_fisica else "cnpj invalido"
    if any(expected in value for value in normalized):
        raise CertificateError(SOURCE, "validation", "documento_rejeitado", f"TRF1 rejected the document: {expected}")


def _get_recaptcha_token(key: str, version: str) -> str:
    session = requests.Session(impersonate="chrome120")
    try:
        anchor = session.get(
            "https://www.google.com/recaptcha/api2/anchor",
            params={"ar": "1", "k": key, "co": RECAPTCHA_CO, "hl": "pt-BR", "v": version, "size": "invisible"},
            timeout=30,
        )
        match = re.search(r'id="recaptcha-token"\s+value="([^"]+)"', anchor.text)
        if not match:
            raise ValueError("recaptcha anchor token missing")
        reload_response = session.post(
            f"https://www.google.com/recaptcha/api2/reload?k={key}",
            data=f"v={version}&reason=q&c={match.group(1)}&k={key}&co={RECAPTCHA_CO}&hl=pt-BR&size=invisible",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        token_match = re.search(r'"rresp","([^"]+)"', reload_response.text)
        if not token_match:
            raise ValueError("recaptcha response token missing")
        return token_match.group(1)
    except Exception as exc:
        raise CertificateError(SOURCE, "challenge", "recaptcha_falhou", "TRF1 reCAPTCHA token retrieval failed", retryable=True) from exc


def _payload(request: Trf1Request, documento: str) -> dict:
    siglas = [sigla.upper() for sigla in request.orgao_siglas]
    return {"spec": {"@type": "PFS" if request.is_pessoa_fisica else "PJS", "tipo": TIPOS_CERTIDAO[request.tipo_certidao_id], "secoes": [ORGAOS[sigla] for sigla in siglas], "requerente": {"documento": documento, "isPessoaFisica": request.is_pessoa_fisica}}}


def _save_pdf(response, destination: Path) -> None:
    if response.status_code >= 400:
        raise CertificateError(SOURCE, "request", "upstream_indisponivel" if response.status_code >= 500 else "validacao_remota", f"TRF1 certificate request failed (HTTP {response.status_code})", retryable=response.status_code >= 500, upstream_status=response.status_code)
    if not response.content.startswith(b"%PDF-"):
        raise CertificateError(SOURCE, "artifact", "pdf_invalido", "TRF1 did not return a PDF", upstream_status=response.status_code)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as handle:
        handle.write(response.content)
        partial = Path(handle.name)
    partial.replace(destination)


def generate(request: Trf1Request) -> CertificateResult:
    documento = _validate(request)
    chrome = CdpChrome(_chrome_path(request.chrome_path), debug_port=request.debug_port, profile_dir=request.profile_dir)
    try:
        page = chrome.start()
        try:
            page.goto(SOLICITACAO_URL, wait_until="domcontentloaded", timeout=request.page_timeout_ms)
            key, version = _recaptcha_metadata(page, request.page_timeout_ms)
            _fill_and_submit(page, documento, request.is_pessoa_fisica)
        except CertificateError:
            raise
        except Exception as exc:
            raise CertificateError(SOURCE, "browser", "pagina_falhou", "TRF1 page interaction failed", retryable=True) from exc
        token = _get_recaptcha_token(key, version)
        try:
            response = requests.post(CERTIDAO_API_URL, json=_payload(request, documento), headers={"X-Token": token}, timeout=60, impersonate="chrome120")
        except Exception as exc:
            raise CertificateError(SOURCE, "request", "rede_indisponivel", "TRF1 certificate request failed", retryable=True) from exc
    finally:
        chrome.close()
    tipo = TIPOS_CERTIDAO[request.tipo_certidao_id]["descricao"]
    filename = f"Certidao TRF1 {tipo} - {documento}.pdf"
    destination = request.output_dir / filename
    _save_pdf(response, destination)
    return CertificateResult(SOURCE, "CPF" if request.is_pessoa_fisica else "CNPJ", filename, destination, {"certificate_type": tipo, "status_code": response.status_code})


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one TRF1 certificate")
    parser.add_argument("--documento", required=True)
    parser.add_argument("--cnpj", action="store_true")
    parser.add_argument("--tipo-certidao-id", type=int, default=1)
    parser.add_argument("--orgao", nargs="+", default=["DF"])
    parser.add_argument("--output-dir", type=Path, default=Path("runs/trf1"))
    parser.add_argument("--profile", type=Path, default=Path("experiments/profile"))
    parser.add_argument("--debug-port", type=int, default=9334)
    args = parser.parse_args()
    request = Trf1Request(documento=args.documento, is_pessoa_fisica=not args.cnpj, tipo_certidao_id=args.tipo_certidao_id, orgao_siglas=tuple(args.orgao), output_dir=args.output_dir, profile_dir=args.profile, debug_port=args.debug_port)
    try:
        result = generate(request)
    except CertificateError as exc:
        print(json.dumps({"status": "error", "source": exc.source, "stage": exc.stage, "code": exc.code, "message": exc.message}))
        return 1
    print(json.dumps({"status": "success", "source": result.source, "filename": result.filename, "pdf_path": str(result.pdf_path), "bytes": result.pdf_path.stat().st_size, "metadata": result.metadata}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
