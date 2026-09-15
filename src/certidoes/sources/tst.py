from __future__ import annotations

import argparse
import base64
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import requests

from certidoes.errors import CertificateError
from certidoes.models import CertificateResult

SOURCE = "tst"
BASE_URL = "https://cndt-certidao.tst.jus.br"
CAPTCHA_URL = f"{BASE_URL}/api/captcha"
CERTIFICATE_URL = f"{BASE_URL}/api/certidao"
REFERER = f"{BASE_URL}/gerarCertidao"


@dataclass(frozen=True)
class TstRequest:
    documento: str
    output_dir: Path = Path("runs/tst")
    request_timeout_seconds: int = 30
    captcha_retries: int = 3


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _validate(request: TstRequest) -> str:
    documento = _digits_only(request.documento)
    if len(documento) not in (11, 14):
        raise CertificateError(SOURCE, "input", "documento_invalido", "TST requires an 11-digit CPF or 14-digit CNPJ")
    if request.request_timeout_seconds <= 0 or request.captcha_retries <= 0:
        raise CertificateError(SOURCE, "input", "config_invalida", "timeout and captcha retries must be positive")
    return documento


def _headers(content_type: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/pdf, application/json, text/plain, */*",
        "Origin": BASE_URL,
        "Referer": REFERER,
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _image_bytes(value: str) -> bytes:
    encoded = value.split(",", 1)[-1]
    try:
        return base64.b64decode(encoded)
    except Exception as exc:
        raise CertificateError(SOURCE, "challenge", "captcha_invalido", "TST CAPTCHA image is not valid base64", retryable=True) from exc


def _captcha_answers(encoded_image: str) -> list[str]:
    try:
        import cv2
        import ddddocr
        import numpy as np
    except ImportError as exc:
        raise CertificateError(SOURCE, "challenge", "ocr_ausente", "Install ddddocr and OpenCV to solve the TST CAPTCHA") from exc
    image = cv2.imdecode(np.frombuffer(_image_bytes(encoded_image), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise CertificateError(SOURCE, "challenge", "captcha_invalido", "TST CAPTCHA image could not be decoded", retryable=True)
    height = image.shape[0]
    variants = (image, image[int(height * 0.35) : int(height * 0.9), :], cv2.medianBlur(image, 3))
    ocr = ddddocr.DdddOcr(show_ad=False)
    answers: list[str] = []
    for variant in variants:
        encoded = cv2.imencode(".png", variant)[1].tobytes()
        answer = ocr.classification(encoded).strip().lower()
        if re.fullmatch(r"[a-z0-9]{6}", answer) and answer not in answers:
            answers.append(answer)
    if not answers:
        raise CertificateError(SOURCE, "challenge", "ocr_invalido", "TST CAPTCHA OCR returned no valid six-character answer", retryable=True)
    return answers


def _save_pdf(destination: Path, body: bytes) -> None:
    if not body.startswith(b"%PDF-"):
        raise CertificateError(SOURCE, "artifact", "pdf_invalido", "TST did not return a PDF")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=destination.parent, suffix=".part", delete=False) as handle:
        handle.write(body)
        temporary = Path(handle.name)
    temporary.replace(destination)


def generate(request: TstRequest) -> CertificateResult:
    documento = _validate(request)
    session = requests.Session(impersonate="chrome")
    last_error: CertificateError | None = None
    for attempt in range(1, request.captcha_retries + 1):
        try:
            challenge_response = session.get(CAPTCHA_URL, headers=_headers(), timeout=request.request_timeout_seconds)
            challenge = challenge_response.json()
            if challenge_response.status_code >= 400 or not challenge.get("imagem") or not challenge.get("token"):
                raise CertificateError(SOURCE, "challenge", "captcha_indisponivel", "TST did not return a CAPTCHA challenge", retryable=True, upstream_status=challenge_response.status_code)
            answers = _captcha_answers(challenge["imagem"])
            for answer in answers:
                response = session.post(
                    CERTIFICATE_URL,
                    json={"cpfCnpj": documento, "email": "", "resposta": answer, "tokenDesafio": challenge["token"]},
                    headers=_headers(content_type=True),
                    timeout=request.request_timeout_seconds,
                )
                if response.status_code == 200 and response.content.startswith(b"%PDF-"):
                    destination = request.output_dir / f"Certidao TST - {documento}.pdf"
                    _save_pdf(destination, response.content)
                    return CertificateResult(SOURCE, "CPF" if len(documento) == 11 else "CNPJ", destination.name, destination, {"captcha_attempts": attempt})
                if response.status_code == 422:
                    try:
                        data = response.json()
                        message = data.get("mensagem") or data.get("erro") or response.text
                    except ValueError:
                        message = response.text
                    raise CertificateError(SOURCE, "validation", "documento_rejeitado", message, upstream_status=response.status_code)
                if response.status_code not in (400, 401, 403):
                    raise CertificateError(SOURCE, "emissao", "resposta_invalida", f"TST emission failed (HTTP {response.status_code})", retryable=True, upstream_status=response.status_code)
            last_error = CertificateError(SOURCE, "challenge", "captcha_rejeitado", "TST rejected the CAPTCHA answers", retryable=True)
        except CertificateError as exc:
            if not exc.retryable:
                raise
            last_error = exc
        except ValueError as exc:
            last_error = CertificateError(SOURCE, "challenge", "resposta_nao_json", "TST CAPTCHA endpoint returned non-JSON data", retryable=True)
        except Exception as exc:
            last_error = CertificateError(SOURCE, "network", "rede_indisponivel", "TST request failed", retryable=True)
    raise last_error or CertificateError(SOURCE, "challenge", "captcha_falhou", "TST CAPTCHA could not be solved", retryable=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one TST certificate using HTTP only")
    parser.add_argument("--documento", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("runs/tst"))
    parser.add_argument("--captcha-retries", type=int, default=3)
    args = parser.parse_args()
    try:
        result = generate(TstRequest(args.documento, args.output_dir, captcha_retries=args.captcha_retries))
    except CertificateError as exc:
        print(json.dumps({"status": "error", "source": exc.source, "stage": exc.stage, "code": exc.code, "message": exc.message}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "success", "source": result.source, "filename": result.filename, "pdf_path": str(result.pdf_path), "bytes": result.pdf_path.stat().st_size, "metadata": result.metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
