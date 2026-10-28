import io
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from certidoes import github_executor as gx
from certidoes.errors import CertificateError
from certidoes.github_payload import decrypt, encrypt
from certidoes.sources import fazenda
from certidoes.sources.fazenda import FazendaRequest

FAZENDA_FIELDS = ("documento", "tipo_pessoa", "finalidade", "tipo_certidao")
PAYLOAD = {"documento": "11122233344", "tipo_pessoa": "Pessoa Física",
           "finalidade": "Junto ao GDF", "tipo_certidao": "Certidão de Débitos"}


def _keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return public_pem, private_pem


def test_decrypt_accepts_fazenda_fields():
    public, private = _keys()
    assert decrypt(encrypt(PAYLOAD, public), private, FAZENDA_FIELDS) == PAYLOAD


def test_decrypt_default_still_rejects_other_shapes():
    public, private = _keys()
    with pytest.raises(ValueError):
        decrypt(encrypt(PAYLOAD, public), private)


def _clear_executor_env(monkeypatch):
    monkeypatch.delenv("FAZENDA_EXECUTOR", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)


def test_executor_defaults_to_local_outside_cloud_run(monkeypatch):
    _clear_executor_env(monkeypatch)
    assert fazenda._executor() == "local"


def test_executor_defaults_to_github_on_cloud_run(monkeypatch):
    _clear_executor_env(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "certidoes-ingest")
    assert fazenda._executor() == "github"


def test_explicit_executor_overrides_the_cloud_run_default(monkeypatch):
    _clear_executor_env(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "certidoes-ingest")
    monkeypatch.setenv("FAZENDA_EXECUTOR", "local")
    assert fazenda._executor() == "local"
    monkeypatch.delenv("K_SERVICE")
    monkeypatch.setenv("FAZENDA_EXECUTOR", "github")
    assert fazenda._executor() == "github"


def test_unknown_executor_value_falls_back_to_detection(monkeypatch):
    _clear_executor_env(monkeypatch)
    monkeypatch.setenv("FAZENDA_EXECUTOR", "nonsense")
    assert fazenda._executor() == "local"


def test_generate_routes_to_github_on_cloud_run(monkeypatch, tmp_path):
    _clear_executor_env(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "certidoes-ingest")
    seen = {}
    monkeypatch.setattr(fazenda, "_github_generate", lambda request, documento: seen.update(doc=documento) or "via-github")
    assert fazenda.generate(FazendaRequest(documento="111.222.333-44", output_dir=tmp_path)) == "via-github"
    assert seen["doc"] == "11122233344"


def test_generate_keeps_the_local_browser_path_outside_cloud_run(monkeypatch, tmp_path):
    _clear_executor_env(monkeypatch)
    monkeypatch.setattr(fazenda, "_chrome_path", lambda: "chrome")

    def local_path_reached(*args, **kwargs):
        raise RuntimeError("local path reached")

    monkeypatch.setattr(fazenda, "CdpChrome", local_path_reached)
    with pytest.raises(RuntimeError, match="local path reached"):
        fazenda.generate(FazendaRequest(documento="11122233344", output_dir=tmp_path))


def test_github_generate_requires_configuration(monkeypatch, tmp_path):
    for name in ("FAZENDA_GITHUB_REPO", "RECEITA_GITHUB_REPO", "FAZENDA_GITHUB_TOKEN",
                 "RECEITA_GITHUB_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CertificateError) as exc:
        fazenda._github_generate(FazendaRequest(documento="11122233344", output_dir=tmp_path), "11122233344")
    assert exc.value.code == "github_config_missing"


def _fake_api(conclusion, calls):
    def fake(source, method, url, token, payload=None):
        calls.append(method)
        if method == "POST":
            return {}
        if "/actions/runs?" in url:
            return {"workflow_runs": [{"id": 7, "display_title": "Fazenda abc123",
                                       "status": "completed", "conclusion": conclusion}]}
        if url.endswith("/artifacts"):
            return {"artifacts": [{"name": "fazenda-abc123", "expired": False,
                                   "archive_download_url": "https://example.test/zip", "url": "https://api.test/a/1"}]}
        return {}
    return fake


def _fake_opener(pdf_bytes):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("certificate.pdf", pdf_bytes)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return archive.getvalue()

    class Opener:
        def open(self, request, timeout=None):
            return Response()

    return lambda *handlers: Opener()


def _run(monkeypatch, conclusion, pdf_bytes=b"%PDF-1.4 test"):
    calls = []
    monkeypatch.setattr(gx, "_api_json", _fake_api(conclusion, calls))
    monkeypatch.setattr(gx, "build_opener", _fake_opener(pdf_bytes))
    result = gx.run_and_fetch_pdf(source="fazenda", repo="o/r", token="t", workflow="w.yml", ref="main",
                                  inputs={"payload": "p"}, request_id="abc123", timeout_s=5, poll_s=0)
    return result, calls


def test_executor_returns_pdf_and_deletes_the_artifact(monkeypatch):
    (pdf, run_id), calls = _run(monkeypatch, "success")
    assert pdf.startswith(b"%PDF-") and run_id == 7
    assert calls[0] == "POST" and calls[-1] == "DELETE"


def test_executor_reports_a_failed_run(monkeypatch):
    with pytest.raises(CertificateError) as exc:
        _run(monkeypatch, "failure")
    assert exc.value.code == "github_run_failed" and exc.value.source == "fazenda"


def test_executor_rejects_a_non_pdf_artifact(monkeypatch):
    with pytest.raises(CertificateError) as exc:
        _run(monkeypatch, "success", pdf_bytes=b"not a pdf")
    assert exc.value.code == "pdf_invalido"
