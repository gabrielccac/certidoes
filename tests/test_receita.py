from pathlib import Path

import pytest

from certidoes.errors import CertificateError
from certidoes.sources.receita import ReceitaRequest, _normalize, _require_ok


def test_normalizes_formatted_cpf():
    assert _normalize(ReceitaRequest("124.333.776-17", "2002-05-23")) == ("12433377617", "CPF", "PF")


def test_normalizes_cnpj_without_birth_date():
    assert _normalize(ReceitaRequest("11.222.333/0001-81", is_pessoa_fisica=False)) == ("11222333000181", "CNPJ", "PJ")


@pytest.mark.parametrize("config", [
    ReceitaRequest("123"),
    ReceitaRequest("111.111.111-11", "2002-05-23"),
    ReceitaRequest("124.333.776-17"),
])
def test_rejects_invalid_local_input(config):
    with pytest.raises(CertificateError) as raised:
        _normalize(config)
    assert raised.value.stage == "input"


def test_reports_receita_captcha_rejection():
    response = type("Response", (), {
        "status_code": 400,
        "json": lambda self: {"statusValidacao": "CaptchaFalhaValidacao"},
    })()
    with pytest.raises(CertificateError) as raised:
        _require_ok(response, "validation")
    assert (raised.value.stage, raised.value.code, raised.value.retryable) == (
        "challenge", "captcha_rejeitado", True,
    )
