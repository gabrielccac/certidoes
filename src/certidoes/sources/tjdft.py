from __future__ import annotations

import argparse
import json
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

from certidoes.errors import CertificateError
from certidoes.models import CertificateResult

API_BASE_URL = "https://cnc-api.tjdft.jus.br/api/ext"
SOURCE = "tjdft"

_TIPO_ALIASES = {
    "CRIMINAL": "CRIMINAL",
    "CIVEL": "CIVEL",
    "CIVIL": "CIVEL",
    "FALENCIA CONCORDATA": "FALENCIA_CONCORDATA",
    "FALENCIA/CONCORDATA": "FALENCIA_CONCORDATA",
    "FALENCIA": "FALENCIA_CONCORDATA",
    "ESPECIAL": "ESPECIAL",
}


@dataclass(frozen=True)
class TjdftRequest:
    documento: str
    primeiro_nome: str
    nome_mae: str
    tipo_certidao: str
    output_dir: Path = Path("runs/tjdft")
    timeout_seconds: int = 30
    download_timeout_seconds: int = 60


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_tipo(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    normalized = " ".join(text.strip().upper().split())
    if normalized not in _TIPO_ALIASES:
        raise CertificateError(SOURCE, "input", "tipo_certidao_invalido", "Unsupported TJDFT certificate type")
    return _TIPO_ALIASES[normalized]


def _validate(request: TjdftRequest) -> tuple[str, str]:
    documento = _digits_only(request.documento)
    if len(documento) != 11:
        raise CertificateError(SOURCE, "input", "documento_invalido", "TJDFT currently supports an 11-digit CPF")
    if not request.primeiro_nome.strip():
        raise CertificateError(SOURCE, "input", "primeiro_nome_obrigatorio", "primeiro_nome is required")
    if not request.nome_mae.strip():
        raise CertificateError(SOURCE, "input", "nome_mae_obrigatorio", "nome_mae is required")
    if request.timeout_seconds <= 0 or request.download_timeout_seconds <= 0:
        raise CertificateError(SOURCE, "input", "timeout_invalido", "timeouts must be greater than zero")
    return documento, _normalize_tipo(request.tipo_certidao)


def _json_response(response: requests.Response, stage: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CertificateError(
            SOURCE,
            stage,
            "resposta_nao_json",
            f"TJDFT returned non-JSON data (HTTP {response.status_code})",
            retryable=response.status_code >= 500,
            upstream_status=response.status_code,
        ) from exc
    if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("erro"):
        raise CertificateError(
            SOURCE,
            stage,
            "validacao_remota" if response.status_code < 500 else "upstream_indisponivel",
            f"TJDFT request failed (HTTP {response.status_code})",
            retryable=response.status_code >= 500,
            upstream_status=response.status_code,
        )
    return payload


def _get_json(session: requests.Session, url: str, *, timeout: int, stage: str) -> dict:
    try:
        response = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise CertificateError(SOURCE, stage, "rede_indisponivel", "TJDFT request failed", retryable=True) from exc
    return _json_response(response, stage)


def _post_json(session: requests.Session, url: str, *, payload: dict, timeout: int, stage: str) -> dict:
    try:
        response = session.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise CertificateError(SOURCE, stage, "rede_indisponivel", "TJDFT request failed", retryable=True) from exc
    return _json_response(response, stage)


def _download_pdf(session: requests.Session, url: str, destination: Path, timeout: int) -> None:
    try:
        with session.get(url, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
                partial = Path(handle.name)
        with partial.open("rb") as handle:
            signature = handle.read(5)
        if partial.stat().st_size < 5 or signature != b"%PDF-":
            partial.unlink(missing_ok=True)
            raise CertificateError(SOURCE, "artifact", "pdf_invalido", "TJDFT did not return a PDF")
        partial.replace(destination)
    except CertificateError:
        raise
    except (OSError, requests.RequestException) as exc:
        raise CertificateError(SOURCE, "artifact", "download_falhou", "TJDFT PDF download failed", retryable=True) from exc


def generate(request: TjdftRequest) -> CertificateResult:
    documento, tipo_certidao = _validate(request)
    request.output_dir.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        pessoa = _get_json(
            session,
            f"{API_BASE_URL}/pessoas/{documento}?nome={quote(request.primeiro_nome.strip())}",
            timeout=request.timeout_seconds,
            stage="pessoa",
        )
        nome_receita = str(pessoa.get("nome_receita") or "").strip()
        cpf_cnpj = str(pessoa.get("cpf_cnpj") or documento).strip()
        if not nome_receita or not cpf_cnpj:
            raise CertificateError(SOURCE, "validation", "pessoa_incompleta", "TJDFT person response is incomplete")

        solicitacao = _post_json(
            session,
            f"{API_BASE_URL}/solicitacoes",
            payload={
                "nome": nome_receita,
                "cpf_cnpj": cpf_cnpj,
                "nome_mae": request.nome_mae.strip(),
                "tipo_certidao": tipo_certidao,
            },
            timeout=request.timeout_seconds,
            stage="solicitacao",
        )
        url_certidao = str((solicitacao.get("certidao") or {}).get("url_certidao") or "").strip()
        if not url_certidao:
            raise CertificateError(SOURCE, "request", "url_certidao_ausente", "TJDFT response has no certificate URL")

        filename = f"Certidao TJDFT {tipo_certidao} - {documento}.pdf"
        destination = request.output_dir / filename
        _download_pdf(session, url_certidao, destination, request.download_timeout_seconds)
        return CertificateResult(
            source=SOURCE,
            document_type="CPF",
            filename=filename,
            pdf_path=destination,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one TJDFT certificate")
    parser.add_argument("--documento", required=True)
    parser.add_argument("--primeiro-nome", required=True)
    parser.add_argument("--nome-mae", required=True)
    parser.add_argument("--tipo-certidao", default="CRIMINAL")
    parser.add_argument("--output-dir", type=Path, default=Path("runs/tjdft"))
    args = parser.parse_args()
    try:
        result = generate(TjdftRequest(**vars(args)))
    except CertificateError as exc:
        print(json.dumps({"status": "error", "source": exc.source, "stage": exc.stage, "code": exc.code, "message": exc.message}))
        return 1
    print(json.dumps({"status": "success", "source": result.source, "filename": result.filename, "pdf_path": str(result.pdf_path), "bytes": result.pdf_path.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
