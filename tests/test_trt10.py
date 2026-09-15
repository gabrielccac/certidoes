from certidoes.errors import CertificateError
from certidoes.sources import trt10


def test_formats_cpf_like_trt10_frontend():
    request = trt10.Trt10Request(documento="12433377617")
    assert trt10._payload(request)["numeroDoDocumento"] == "124.333.776-17"


def test_retries_trt10_captcha(monkeypatch):
    calls = 0

    def emit(_request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise CertificateError(
                "trt10", "challenge", "captcha_rejeitado", "retry", retryable=True,
            )
        return 123

    monkeypatch.setattr(trt10, "_emit_api", emit)
    assert trt10._emit_with_retries(trt10.Trt10Request(documento="12433377617")) == 123
    assert calls == 3
