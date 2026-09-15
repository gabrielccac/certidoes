"""Validate TST CAPTCHA OCR without issuing a certificate."""

from __future__ import annotations

import base64
import re
import unicodedata

import cv2
import ddddocr
import numpy as np
from playwright.sync_api import sync_playwright

URL = "https://cndt-certidao.tst.jus.br/gerarCertidao"
INVALID_DOCUMENT = "00000000000"


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _answers(image: np.ndarray) -> list[str]:
    ocr = ddddocr.DdddOcr(show_ad=False)
    height = image.shape[0]
    variants = (
        image,
        image[int(height * 0.35) : int(height * 0.9), :],
        cv2.medianBlur(image, 3),
    )
    answers: list[str] = []
    for variant in variants:
        encoded = cv2.imencode(".png", variant)[1].tobytes()
        answer = ocr.classification(encoded).strip().lower()
        if re.fullmatch(r"[a-z0-9]{6}", answer) and answer not in answers:
            answers.append(answer)
    return answers


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for attempt in range(1, 4):
                page = browser.new_page()
                try:
                    page.goto(URL, wait_until="networkidle", timeout=30_000)
                    page.locator("#cpfCnpj").fill(INVALID_DOCUMENT)
                    source = page.locator("#captcha-imagem").get_attribute("src") or ""
                    image = cv2.imdecode(np.frombuffer(base64.b64decode(source.split(",", 1)[1]), np.uint8), cv2.IMREAD_GRAYSCALE)
                    if image is None:
                        continue
                    answers = _answers(image)
                    if not answers:
                        continue
                    page.locator("#captcha-resposta").fill(answers[0])
                    with page.expect_response(lambda response: "/api/certidao" in response.url and response.request.method == "POST", timeout=30_000) as response_info:
                        page.locator("#botao-emitir").click()
                    response = response_info.value
                    body = _ascii(response.text())
                    if response.status == 422 and "cnpj / cpf informado e invalido" in body:
                        print(f"captcha=accepted attempt={attempt} answer_length=6 validation_status={response.status}")
                        return 0
                finally:
                    page.close()
            raise RuntimeError("OCR did not produce an accepted CAPTCHA answer in 3 attempts")
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
