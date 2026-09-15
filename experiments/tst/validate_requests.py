"""Validate the TST certificate flow using HTTP only."""

from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path

import cv2
import ddddocr
import numpy as np
from curl_cffi import requests

BASE_URL = "https://cndt-certidao.tst.jus.br"


def _captcha_answer(encoded_image: str) -> str:
    image = cv2.imdecode(np.frombuffer(base64.b64decode(encoded_image), np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError("TST CAPTCHA image could not be decoded")
    height = image.shape[0]
    crop = image[int(height * 0.35) : int(height * 0.9), :]
    encoded = cv2.imencode(".png", crop)[1].tobytes()
    answer = ddddocr.DdddOcr(show_ad=False).classification(encoded).strip().lower()
    if not re.fullmatch(r"[a-z0-9]{6}", answer):
        raise RuntimeError(f"OCR returned invalid answer: {answer!r}")
    return answer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documento", default="12433377617")
    parser.add_argument("--output", type=Path, default=Path("runs/tst/requests_certidao_tst_12433377617.pdf"))
    args = parser.parse_args()
    headers = {"Accept": "application/json, text/plain, */*", "Origin": BASE_URL, "Referer": f"{BASE_URL}/gerarCertidao"}
    session = requests.Session(impersonate="chrome")
    challenge = session.get(f"{BASE_URL}/api/captcha", headers=headers, timeout=30).json()
    payload = {"cpfCnpj": args.documento, "email": "", "resposta": _captcha_answer(challenge["imagem"]), "tokenDesafio": challenge["token"]}
    response = session.post(f"{BASE_URL}/api/certidao", json=payload, headers={**headers, "Content-Type": "application/json"}, timeout=30)
    if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
        raise RuntimeError(f"TST did not return a PDF: HTTP {response.status_code}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(response.content)
    print(f"success pdf={args.output} bytes={len(response.content)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
