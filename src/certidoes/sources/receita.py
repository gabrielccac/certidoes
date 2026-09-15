from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from curl_cffi import requests

from certidoes.browser import CdpChrome
from certidoes.errors import CertificateError
from certidoes.models import CertificateResult

SOURCE = "receita"
PAGE_URLS = {
    "CPF": "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cpf",
    "CNPJ": "https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj",
}
VERIFICAR_URL = "https://servicos.receitafederal.gov.br/servico/certidoes/api/Emissao/verificar"
EMISSAO_URL = "https://servicos.receitafederal.gov.br/servico/certidoes/api/Emissao"
WIDGET_SELECTOR = "[data-hcaptcha-widget-id]"

BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "pt-BR,pt;q=0.6",
    "content-type": "application/json",
    "origin": "https://servicos.receitafederal.gov.br",
    "referer": "https://servicos.receitafederal.gov.br/servico/certidoes/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class ReceitaRequest:
    documento: str
    data_nascimento: str | None = None
    is_pessoa_fisica: bool | None = None
    output_dir: Path = Path("runs/receita")
    debug_port: int = 9336
    profile_dir: Path = Path("experiments/profile")
    chrome_path: str | None = None
    token_timeout_ms: int = 30_000
    request_timeout_s: int = 30
    captcha_retries: int = 3


def _digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _valid_cpf(value: str) -> bool:
    if len(value) != 11 or len(set(value)) == 1:
        return False
    for length in (9, 10):
        total = sum(int(value[index]) * (length + 1 - index) for index in range(length))
        digit = (total * 10 % 11) % 10
        if digit != int(value[length]):
            return False
    return True


def _valid_cnpj(value: str) -> bool:
    if len(value) != 14 or len(set(value)) == 1:
        return False
    weights = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    for length in (12, 13):
        current = weights if length == 13 else weights[1:]
        total = sum(int(value[index]) * current[index] for index in range(length))
        digit = 0 if total % 11 < 2 else 11 - total % 11
        if digit != int(value[length]):
            return False
    return True


def _normalize(request: ReceitaRequest) -> tuple[str, str, str]:
    documento = _digits(request.documento)
    if request.is_pessoa_fisica is None:
        is_pf = len(documento) == 11
    else:
        is_pf = request.is_pessoa_fisica
    tipo = "CPF" if is_pf else "CNPJ"
    valid = _valid_cpf(documento) if is_pf else _valid_cnpj(documento)
    if not valid:
        raise CertificateError(SOURCE, "input", "documento_invalido", f"Invalid {tipo}")
    birth = str(request.data_nascimento or "").strip()
    if is_pf and not birth:
        raise CertificateError(SOURCE, "input", "data_nascimento_obrigatoria", "CPF requires data_nascimento")
    if birth and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth):
        raise CertificateError(SOURCE, "input", "data_nascimento_invalida", "data_nascimento must use YYYY-MM-DD")
    if birth:
        try:
            date.fromisoformat(birth)
        except ValueError as exc:
            raise CertificateError(SOURCE, "input", "data_nascimento_invalida", "data_nascimento is not a real date") from exc
    if request.debug_port <= 0 or request.token_timeout_ms <= 0 or request.request_timeout_s <= 0 or request.captcha_retries <= 0:
        raise CertificateError(SOURCE, "input", "configuracao_invalida", "timeouts, retries, and debug_port must be positive")
    return documento, tipo, "PF" if is_pf else "PJ"


def _chrome_path(configured: str | None) -> str:
    candidates = [configured, os.environ.get("CHROME_PATH"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise CertificateError(SOURCE, "browser", "chrome_nao_encontrado", "Google Chrome executable was not found")


def _token(page, timeout_ms: int) -> str:
    try:
        page.wait_for_selector(WIDGET_SELECTOR, state="attached", timeout=timeout_ms)
        widgets = page.evaluate("""() => [...document.querySelectorAll('[data-hcaptcha-widget-id]')]
            .map(element => element.getAttribute('data-hcaptcha-widget-id')).filter(Boolean)""")
        if not widgets:
            raise ValueError("hCaptcha widget id missing")
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            ready = page.evaluate("""() => !!window.hcaptcha && typeof window.hcaptcha.execute === 'function'""")
            if ready:
                break
            page.wait_for_timeout(250)
        else:
            raise ValueError("hCaptcha is not ready")
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        script = """
            async ({widgetId, timeoutMs}) => {
              if (!window.hcaptcha || typeof window.hcaptcha.execute !== 'function')
                throw new Error('hCaptcha is not ready');
              const value = await Promise.race([
                window.hcaptcha.execute(widgetId, {async: true}),
                new Promise((_, reject) => setTimeout(() => reject(new Error('hCaptcha timeout')), timeoutMs))
              ]);
              return value && typeof value === 'object' ? value.response : value;
            }
        """
        last_error = None
        for widget in widgets:
            try:
                result = page.evaluate(script, {"widgetId": widget, "timeoutMs": remaining})
                if isinstance(result, str) and result:
                    return result
                last_error = ValueError("hCaptcha returned an empty token")
            except Exception as exc:
                last_error = exc
        raise last_error or ValueError("hCaptcha token generation failed")
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError(SOURCE, "challenge", "hcaptcha_falhou", "hCaptcha token generation failed", retryable=True) from exc


def _fresh_token(request: ReceitaRequest, tipo: str) -> tuple[str, str]:
    last_error: CertificateError | None = None
    for attempt in range(request.captcha_retries):
        chrome = CdpChrome(
            _chrome_path(request.chrome_path),
            debug_port=request.debug_port,
            profile_dir=request.profile_dir,
            incognito=True,
        )
        try:
            page = chrome.start()
            page.goto(PAGE_URLS[tipo], wait_until="domcontentloaded", timeout=request.token_timeout_ms)
            token = _token(page, request.token_timeout_ms)
            user_agent = page.evaluate("navigator.userAgent")
            return token, user_agent
        except CertificateError as exc:
            last_error = exc
        except Exception:
            last_error = CertificateError(SOURCE, "browser", "pagina_falhou", "Receita page interaction failed", retryable=True)
        finally:
            chrome.close()
        if attempt + 1 < request.captcha_retries:
            time.sleep(0.5)
    raise last_error or CertificateError(SOURCE, "challenge", "hcaptcha_falhou", "hCaptcha token generation failed", retryable=True)


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def _payload(request: ReceitaRequest, documento: str, tipo: str, contributor: str) -> dict:
    return {
        "ni": documento,
        "tipoContribuinte": contributor,
        "dataNascimento": str(request.data_nascimento or "").strip(),
        "tipoContribuinteEnum": tipo,
    }


def _post(session, url: str, payload: dict, *, token: str | None = None, cookies: dict[str, str] | None = None, user_agent: str | None = None, timeout: int):
    headers = dict(BASE_HEADERS)
    if user_agent:
        headers["user-agent"] = user_agent
    if token:
        headers["x-captcha-token"] = token
    if cookies:
        headers["cookie"] = _cookie_header(cookies)
    try:
        return session.post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as exc:
        raise CertificateError(SOURCE, "request", "rede_indisponivel", "Receita request failed", retryable=True) from exc


def _require_ok(response, stage: str = "request") -> None:
    if response.status_code >= 400:
        try:
            validation = response.json().get("statusValidacao")
        except Exception:
            validation = None
        if validation == "CaptchaFalhaValidacao":
            raise CertificateError(
                SOURCE, "challenge", "captcha_rejeitado",
                "Receita rejected the hCaptcha token", retryable=True,
                upstream_status=response.status_code,
            )
        raise CertificateError(
            SOURCE, stage, "upstream_indisponivel" if response.status_code >= 500 else "validacao_remota",
            f"Receita request failed (HTTP {response.status_code})", retryable=response.status_code >= 500,
            upstream_status=response.status_code,
        )


def _emissao_pdf(response) -> bytes:
    _require_ok(response)
    try:
        payload = response.json()
    except Exception as exc:
        raise CertificateError(SOURCE, "artifact", "json_invalido", "Emissao response was not valid JSON", upstream_status=response.status_code) from exc
    if payload.get("statusEmissao") == "EmProcessamento":
        raise CertificateError(SOURCE, "request", "emissao_em_processamento", "Receita certificate is still processing", retryable=True)
    encoded = payload.get("pdf")
    if not encoded:
        raise CertificateError(SOURCE, "artifact", "pdf_ausente", "Emissao response has no PDF")
    try:
        pdf = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise CertificateError(SOURCE, "artifact", "pdf_invalido", "Emissao PDF was not valid base64") from exc
    if not pdf.startswith(b"%PDF-"):
        raise CertificateError(SOURCE, "artifact", "pdf_invalido", "Receita did not return a PDF")
    return pdf


def _save(pdf: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as handle:
        handle.write(pdf)
        partial = Path(handle.name)
    partial.replace(destination)


def _github_json(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = UrlRequest(url, data=body, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read()) if response.status != 204 else {}
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CertificateError(SOURCE, "github", "github_request_failed", "GitHub API request failed", retryable=True) from exc


def _github_generate(request: ReceitaRequest, documento: str, tipo: str) -> CertificateResult:
    repo = os.environ.get("RECEITA_GITHUB_REPO")
    token = os.environ.get("RECEITA_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    public_key = os.environ.get("RECEITA_GITHUB_PUBLIC_KEY")
    if not public_key:
        key_file = Path(__file__).resolve().parents[1] / "receita_github_public.pem"
        if key_file.exists():
            public_key = key_file.read_text(encoding="ascii")
    if not repo or not token or not public_key:
        raise CertificateError(SOURCE, "github", "github_config_missing", "GitHub executor is not configured")
    if tipo != "CPF":
        raise CertificateError(SOURCE, "github", "github_only_cpf", "GitHub Receita executor currently supports CPF only")

    from certidoes.github_payload import encrypt
    request_id = uuid.uuid4().hex
    encrypted = encrypt({"documento": documento, "data_nascimento": request.data_nascimento}, public_key)
    workflow = os.environ.get("RECEITA_GITHUB_WORKFLOW", "receita-diagnostic.yml")
    ref = os.environ.get("RECEITA_GITHUB_REF", "main")
    api = f"https://api.github.com/repos/{repo}"
    _github_json("POST", f"{api}/actions/workflows/{workflow}/dispatches", token,
                 {"ref": ref, "inputs": {"mode": "issue", "request_id": request_id, "payload": encrypted}})

    deadline = time.monotonic() + int(os.environ.get("RECEITA_GITHUB_TIMEOUT_S", "900"))
    run = None
    while time.monotonic() < deadline:
        runs = _github_json("GET", f"{api}/actions/runs?event=workflow_dispatch&branch={ref}&per_page=20", token)
        for candidate in runs.get("workflow_runs", []):
            if request_id in str(candidate.get("display_title", "")):
                run = candidate
                break
        if run and run.get("status") == "completed":
            break
        time.sleep(float(os.environ.get("RECEITA_GITHUB_POLL_S", "5")))
    if not run:
        raise CertificateError(SOURCE, "github", "github_run_not_found", "GitHub workflow run was not found", retryable=True)
    if run.get("conclusion") != "success":
        raise CertificateError(SOURCE, "github", "github_run_failed", "GitHub workflow failed", retryable=True)

    artifacts = _github_json("GET", f"{api}/actions/runs/{run['id']}/artifacts", token).get("artifacts", [])
    artifact = next((item for item in artifacts if request_id in item.get("name", "") and not item.get("expired")), None)
    if not artifact:
        raise CertificateError(SOURCE, "github", "github_artifact_missing", "GitHub PDF artifact was not found", retryable=True)
    download = UrlRequest(artifact["archive_download_url"], headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(download, timeout=60) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
    except (HTTPError, URLError, TimeoutError, zipfile.BadZipFile) as exc:
        raise CertificateError(SOURCE, "github", "github_artifact_download_failed", "GitHub artifact download failed", retryable=True) from exc
    pdf_names = [name for name in archive.namelist() if name.lower().endswith(".pdf") and ".." not in Path(name).parts]
    if len(pdf_names) != 1:
        raise CertificateError(SOURCE, "artifact", "pdf_ausente", "GitHub artifact did not contain exactly one PDF")
    pdf = archive.read(pdf_names[0])
    filename = f"Certidão Receita Federal - {documento}.pdf"
    destination = request.output_dir / filename
    _save(pdf, destination)
    return CertificateResult(SOURCE, tipo, filename, destination,
                             {"status": "Emitida", "bytes": len(pdf), "executor": "github", "run_id": run["id"]})


def generate(request: ReceitaRequest) -> CertificateResult:
    documento, tipo, contributor = _normalize(request)
    if os.environ.get("RECEITA_EXECUTOR") == "github":
        return _github_generate(request, documento, tipo)
    token, user_agent = _fresh_token(request, tipo)

    session = requests.Session(impersonate="chrome")
    base = _payload(request, documento, tipo, contributor)
    verificar = None
    cookies: dict[str, str] = {}
    for attempt in range(request.captcha_retries):
        verificar = _post(session, VERIFICAR_URL, base, token=token, user_agent=user_agent, timeout=request.request_timeout_s)
        cookies = dict(verificar.cookies.items())
        if verificar.status_code != 400:
            break
        # hCaptcha tokens are single-use; only retry with a fresh browser token.
        if attempt + 1 < request.captcha_retries:
            token, user_agent = _fresh_token(request, tipo)
    _require_ok(verificar, "validation")
    try:
        status = verificar.json().get("status")
    except Exception as exc:
        raise CertificateError(SOURCE, "artifact", "json_invalido", "Verificar response was not valid JSON") from exc
    emissao = _post(
        session,
        EMISSAO_URL,
        {key: base[key] for key in ("ni", "tipoContribuinte", "dataNascimento")},
        cookies=cookies,
        user_agent=user_agent,
        timeout=request.request_timeout_s,
    )
    pdf = _emissao_pdf(emissao)
    filename = f"Certidão Receita Federal - {documento}.pdf"
    destination = request.output_dir / filename
    _save(pdf, destination)
    return CertificateResult(SOURCE, tipo, filename, destination, {"status": status or "Emissao", "bytes": len(pdf)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Receita Federal certificate")
    parser.add_argument("--documento", required=True)
    parser.add_argument("--data-nascimento")
    parser.add_argument("--cnpj", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("runs/receita"))
    parser.add_argument("--profile", type=Path, default=Path("experiments/profile"))
    parser.add_argument("--debug-port", type=int, default=9336)
    parser.add_argument("--captcha-retries", type=int, default=3)
    args = parser.parse_args()
    request = ReceitaRequest(args.documento, args.data_nascimento, not args.cnpj, args.output_dir, args.debug_port, args.profile, captcha_retries=args.captcha_retries)
    try:
        result = generate(request)
    except CertificateError as exc:
        print(json.dumps({"status": "error", "source": exc.source, "stage": exc.stage, "code": exc.code, "message": exc.message}))
        return 1
    print(json.dumps({"status": "success", "source": result.source, "filename": result.filename, "pdf_path": str(result.pdf_path), "bytes": result.pdf_path.stat().st_size, "metadata": result.metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
