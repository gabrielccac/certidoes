from __future__ import annotations

import argparse
import base64
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from certidoes.browser import CdpChrome
from certidoes.errors import CertificateError
from certidoes.models import CertificateResult

SOURCE = "fazenda"
FAZENDA_URL = "https://ww1.receita.fazenda.df.gov.br/cidadao/certidoes/Certidao"
SOLVED_ASSET = Path(__file__).resolve().parents[3] / "solved.png"

TIPOS_PESSOA = ("Pessoa Física", "Pessoa Jurídica", "Imóvel", "Veículo")
FINALIDADES = (
    "Junto ao GDF", "Quitação de Saldo Devedor", "Lavrar Escritura Pública",
    "Hipoteca de Imóvel", "Inscrição no Cadastro Fiscal", "Baixa de Inscrição",
    "Registro Imobiliário", "Prova Junto a Cartório", "Prova Junto a IDHAB",
    "Prova Junto ao Terracap", "Isenção IPVA - Veículo Zero Km",
    "Prova Junto à Justiça", "Financiamento", "Cadastro Bancário", "Inventário",
    "Formal de Partilha", "Licitação", "Alteração Cadastral", "Venda",
    "Cessão de Direitos", "Alienação de Imóveis", "Verificação de Débitos",
    "Junto a Órgãos Públicos", "Junto a Novacap", "Transferência",
    "Transferência de Veículo", "Junto à Polícia Federal", "Baixa Cadastro Detran/DF",
    "Alteração Cadastral - CNAE Inativo", "Certidão Declarada Nula Pela SUREC",
    "Teste de Emissão de Certidão",
)
TIPOS_CERTIDAO = ("Certidão de Débitos", "Certidão da Dívida Ativa")


@dataclass(frozen=True)
class FazendaRequest:
    documento: str
    tipo_pessoa: str = "Pessoa Física"
    finalidade: str = "Junto ao GDF"
    tipo_certidao: str = "Certidão de Débitos"
    output_dir: Path = Path("runs/fazenda")
    profile_dir: Path = Path("experiments/profile")
    debug_port: int = 9335
    timeout_ms: int = 30_000


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _validate(request: FazendaRequest) -> str:
    if request.tipo_pessoa not in TIPOS_PESSOA:
        raise CertificateError(SOURCE, "input", "tipo_pessoa_invalido", "Unsupported Fazenda person type")
    if request.finalidade not in FINALIDADES:
        raise CertificateError(SOURCE, "input", "finalidade_invalida", "Unsupported Fazenda purpose")
    if request.tipo_certidao not in TIPOS_CERTIDAO:
        raise CertificateError(SOURCE, "input", "tipo_certidao_invalido", "Unsupported Fazenda certificate type")
    documento = _digits_only(request.documento)
    if not documento:
        raise CertificateError(SOURCE, "input", "documento_obrigatorio", "documento is required")
    if request.tipo_pessoa == "Pessoa Física" and len(documento) != 11:
        raise CertificateError(SOURCE, "input", "documento_invalido", "Pessoa Física requires 11 digits")
    if request.tipo_pessoa == "Pessoa Jurídica" and len(documento) != 14:
        raise CertificateError(SOURCE, "input", "documento_invalido", "Pessoa Jurídica requires 14 digits")
    if request.debug_port <= 0 or request.timeout_ms <= 0:
        raise CertificateError(SOURCE, "input", "config_invalida", "debug port and timeout must be positive")
    return documento


def _chrome_path() -> str:
    for candidate in (
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    raise CertificateError(SOURCE, "browser", "chrome_nao_encontrado", "Google Chrome executable was not found")


def _find_solved_marker(page, asset: Path = SOLVED_ASSET) -> tuple[bool, float]:
    try:
        import cv2
        import numpy as np

        screenshot = cv2.imdecode(np.frombuffer(page.screenshot(type="png"), dtype=np.uint8), cv2.IMREAD_COLOR)
        template = cv2.imread(str(asset), cv2.IMREAD_COLOR)
        if screenshot is None or template is None:
            return False, 0.0
        if template.shape[0] > screenshot.shape[0] or template.shape[1] > screenshot.shape[1]:
            return False, 0.0
        scores = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, _ = cv2.minMaxLoc(scores)
        return confidence >= 0.80, float(confidence)
    except (ImportError, OSError):
        raise CertificateError(SOURCE, "challenge", "opencv_indisponivel", "OpenCV is required to inspect the solved marker")


def _log_challenge(**fields) -> None:
    # Operational trace only: no document, name or URL query ever goes in here.
    print(json.dumps({"fazenda_challenge": fields}, ensure_ascii=False), flush=True)


def _frame_hosts(page) -> list[str]:
    from urllib.parse import urlparse

    try:
        return sorted({urlparse(frame.url).netloc for frame in page.frames if frame.url})
    except Exception:
        return []


def _widget_box(page) -> dict | None:
    try:
        return page.locator('iframe[src*="challenges.cloudflare.com"]').first.bounding_box(timeout=2000)
    except Exception:
        return None


def _widget_snapshot(page, label: str) -> None:
    """Log a small PNG crop (base64) of the challenge area so the real rendering
    can be inspected from the Cloud Run logs. The crop starts below the
    "Gerar PDF" button, so the form fields (document number) never appear in it."""
    try:
        box = _widget_box(page)
        info = {"event": "widget_snapshot", "label": label, "box": box,
                "dpr": page.evaluate("window.devicePixelRatio")}
        if box:
            pad = 30
            clip = {"x": max(box["x"] - pad, 0), "y": max(box["y"] - pad, 0),
                    "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad}
        else:
            button = page.get_by_role("button", name="Gerar PDF", exact=True).bounding_box(timeout=2000)
            viewport = page.evaluate("({w: window.innerWidth, h: window.innerHeight})")
            if not button:
                _log_challenge(event="widget_snapshot_skipped", label=label, reason="no_anchor")
                return
            top = button["y"] + button["height"] + 4
            clip = {"x": 0, "y": top, "width": viewport["w"], "height": min(140, viewport["h"] - top)}
            info["anchor"] = "below_gerar_pdf"
        png = page.screenshot(type="png", clip=clip)
        info["image_bytes"] = len(png)
        info["image_b64"] = base64.b64encode(png).decode()
        _log_challenge(**info)
    except Exception as exc:
        _log_challenge(event="widget_snapshot_failed", label=label, error=type(exc).__name__)


def _wait_solved(page, timeout_ms: int) -> float:
    """Wait passively for the widget to clear itself; nothing in it is clicked."""
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    confidence = 0.0
    while time.monotonic() < deadline:
        solved, confidence = _find_solved_marker(page)
        if solved:
            _log_challenge(outcome="solved", solved_confidence=round(confidence, 3),
                           elapsed_s=round(time.monotonic() - started, 1))
            return confidence
        page.wait_for_timeout(500)
    _widget_snapshot(page, "final")
    _log_challenge(outcome="not_solved", last_solved_confidence=round(confidence, 3),
                   frame_hosts=_frame_hosts(page))
    raise CertificateError(SOURCE, "challenge", "captcha_nao_resolvido", "Fazenda challenge was not solved", retryable=True)


def _select(page, selector: str, value: str) -> None:
    page.locator(selector).click()
    page.get_by_role("option", name=value, exact=True).click()


def _fill_form(page, request: FazendaRequest, documento: str) -> None:
    try:
        page.locator("mat-expansion-panel-header").first.wait_for(state="visible", timeout=request.timeout_ms)
        page.locator("mat-expansion-panel-header").first.click()
        page.locator("mat-radio-button").filter(has_text=request.tipo_pessoa).first.click()
        page.locator("#documento").wait_for(state="visible", timeout=request.timeout_ms)
        page.locator("#documento").fill(documento)
        _select(page, "#finalidade", request.finalidade)
        _select(page, 'mat-select[formcontrolname="TipoCertidao"]', request.tipo_certidao)
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError(SOURCE, "browser", "formulario_falhou", "Fazenda form could not be filled", retryable=True) from exc


def _popup_error(page) -> str | None:
    dialog = page.locator("mat-dialog-container")
    if not dialog.count() or not dialog.first.is_visible():
        return None
    try:
        return dialog.first.inner_text().strip()
    except Exception:
        return "Fazenda rejected the certificate request"


def _save_pdf(destination: Path, body: bytes) -> None:
    if not body.startswith(b"%PDF-"):
        raise CertificateError(SOURCE, "artifact", "pdf_invalido", "Fazenda did not return a PDF")
    with tempfile.NamedTemporaryFile("wb", dir=destination.parent, delete=False) as handle:
        handle.write(body)
        temporary = Path(handle.name)
    temporary.replace(destination)


def _fetch_pdf(page, url: str) -> bytes:
    encoded = page.evaluate("""async (url) => {
        const response = await fetch(url);
        const bytes = new Uint8Array(await response.arrayBuffer());
        let binary = "";
        for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        return btoa(binary);
    }""", url)
    return base64.b64decode(encoded)


def _github_generate(request: FazendaRequest, documento: str) -> CertificateResult:
    """Emit on a GitHub-hosted runner (opt-in via FAZENDA_EXECUTOR=github)."""
    import uuid

    from certidoes.github_executor import run_and_fetch_pdf
    from certidoes.github_payload import encrypt

    repo = os.environ.get("FAZENDA_GITHUB_REPO") or os.environ.get("RECEITA_GITHUB_REPO")
    token = (os.environ.get("FAZENDA_GITHUB_TOKEN") or os.environ.get("RECEITA_GITHUB_TOKEN")
             or os.environ.get("GITHUB_TOKEN"))
    public_key = os.environ.get("RECEITA_GITHUB_PUBLIC_KEY")
    if not public_key:
        key_file = Path(__file__).resolve().parents[1] / "receita_github_public.pem"
        if key_file.exists():
            public_key = key_file.read_text(encoding="ascii")
    if not repo or not token or not public_key:
        raise CertificateError(SOURCE, "github", "github_config_missing", "GitHub executor is not configured")
    request_id = uuid.uuid4().hex
    encrypted = encrypt({"documento": documento, "tipo_pessoa": request.tipo_pessoa,
                         "finalidade": request.finalidade, "tipo_certidao": request.tipo_certidao}, public_key)
    pdf, run_id = run_and_fetch_pdf(
        source=SOURCE, repo=repo, token=token,
        workflow=os.environ.get("FAZENDA_GITHUB_WORKFLOW", "fazenda-emit.yml"),
        ref=os.environ.get("FAZENDA_GITHUB_REF", "main"),
        inputs={"payload": encrypted}, request_id=request_id,
        timeout_s=int(os.environ.get("FAZENDA_GITHUB_TIMEOUT_S", "900")),
        poll_s=float(os.environ.get("FAZENDA_GITHUB_POLL_S", "5")),
    )
    request.output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{request.tipo_pessoa} - {request.finalidade} ({documento}).pdf"
    destination = request.output_dir / filename
    _save_pdf(destination, pdf)
    return CertificateResult(SOURCE, "CPF" if request.tipo_pessoa == "Pessoa Física" else "CNPJ", filename, destination,
                             {"executor": "github", "run_id": run_id, "purpose": request.finalidade,
                              "certificate_type": request.tipo_certidao})


def generate(request: FazendaRequest) -> CertificateResult:
    documento = _validate(request)
    if os.environ.get("FAZENDA_EXECUTOR") == "github":
        return _github_generate(request, documento)
    request.output_dir.mkdir(parents=True, exist_ok=True)
    chrome = CdpChrome(_chrome_path(), debug_port=request.debug_port, profile_dir=request.profile_dir)
    try:
        page = chrome.start()
        page.goto(FAZENDA_URL, wait_until="domcontentloaded", timeout=request.timeout_ms)
        _fill_form(page, request, documento)
        confidence = _wait_solved(page, request.timeout_ms)
        filename = f"{request.tipo_pessoa} - {request.finalidade} ({documento}).pdf"
        destination = request.output_dir / filename
        with page.expect_response(
            lambda response: "/WPI/api/Report/token/" in response.url
            and response.request.method == "GET"
            and "chrome-extension://" not in response.url
            and "application/pdf" in (response.headers.get("content-type") or "").lower(),
            timeout=request.timeout_ms,
        ) as report_info:
            page.get_by_role("button", name="Gerar PDF", exact=True).click()
        report = report_info.value
        if "application/pdf" not in (report.headers.get("content-type") or "").lower():
            raise CertificateError(SOURCE, "artifact", "resposta_invalida", "Fazenda report endpoint did not return a PDF")
        _save_pdf(destination, _fetch_pdf(page, report.url))
        page.wait_for_timeout(1000)
        error = _popup_error(page)
        if error:
            raise CertificateError(SOURCE, "validation", "emissao_rejeitada", error)
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError(SOURCE, "browser", "pagina_falhou", "Fazenda page interaction failed", retryable=True) from exc
    finally:
        chrome.close()
    return CertificateResult(SOURCE, "CPF" if request.tipo_pessoa == "Pessoa Física" else "CNPJ", filename, destination, {"captcha_confidence": round(confidence, 4), "purpose": request.finalidade, "certificate_type": request.tipo_certidao})


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one Fazenda DF certificate")
    parser.add_argument("--documento", required=True)
    parser.add_argument("--tipo-pessoa", default="Pessoa Física", choices=TIPOS_PESSOA)
    parser.add_argument("--finalidade", default="Junto ao GDF", choices=FINALIDADES)
    parser.add_argument("--tipo-certidao", default="Certidão de Débitos", choices=TIPOS_CERTIDAO)
    parser.add_argument("--output-dir", type=Path, default=Path("runs/fazenda"))
    parser.add_argument("--profile", type=Path, default=Path("experiments/profile"))
    parser.add_argument("--debug-port", type=int, default=9335)
    args = parser.parse_args()
    try:
        result = generate(FazendaRequest(
            documento=args.documento,
            tipo_pessoa=args.tipo_pessoa,
            finalidade=args.finalidade,
            tipo_certidao=args.tipo_certidao,
            output_dir=args.output_dir,
            profile_dir=args.profile,
            debug_port=args.debug_port,
        ))
    except CertificateError as exc:
        print(json.dumps({"status": "error", "source": exc.source, "stage": exc.stage, "code": exc.code, "message": exc.message}))
        return 1
    print(json.dumps({"status": "success", "source": result.source, "filename": result.filename, "pdf_path": str(result.pdf_path), "bytes": result.pdf_path.stat().st_size, "metadata": result.metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
