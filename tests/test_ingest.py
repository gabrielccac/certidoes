import dataclasses
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from certidoes import registry
from certidoes.errors import CertificateError
from certidoes.ingest import mapping
from certidoes.ingest import worker as worker_mod
from certidoes.ingest.airtable import attachment_objects
from certidoes.ingest.artifacts import LocalPublicDir
from certidoes.ingest.core import run_job
from certidoes.ingest.providers import JobInputs, JobReport, TableProvider
from certidoes.models import CertificateResult

PESSOAS_TOML = Path(__file__).resolve().parents[1] / "config" / "ingest" / "pessoas.toml"


def test_registry_resolves_all_sources():
    for name in ("receita", "trf1", "tjdft", "fazenda", "trt10", "tst"):
        spec = registry.get_source(name)
        assert callable(spec.generate) and spec.request_cls is not None
    with pytest.raises(ValueError):
        registry.get_source("nope")


def test_transforms():
    assert mapping.apply_transform("strip", "  DF ") == "DF"
    assert mapping.apply_transform("digits", "124.333.776-17\n") == "12433377617"
    assert mapping.apply_transform("first_word", "Maria Da Silva") == "Maria"
    assert mapping.apply_transform("first_word", "  ") == ""
    with pytest.raises(ValueError):
        mapping.apply_transform("rot13", "x")


def test_load_pessoas_mapping(monkeypatch):
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTEST")
    loaded = mapping.load_mapping(PESSOAS_TOML)
    assert loaded.table == "tbllHy5KvlJrFmXsh"
    assert loaded.provider == "airtable"
    assert [source.key for source in loaded.sources] == ["receita-pf", "tjdft-civel"]
    assert [source.source for source in loaded.sources] == ["receita", "tjdft"]
    assert loaded.sources[0].transforms == {"documento": "digits"}
    assert loaded.status.claim_from == ("PENDENTE", "TENTAR NOVAMENTE")


def test_load_mapping_rejects_bad_schema(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[base]\nid = \"appX\"\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mapping.load_mapping(bad)

    bad_source = tmp_path / "bad2.toml"
    bad_source.write_text(
        "[base]\nid = \"appX\"\n[table]\nid = \"tblX\"\n"
        "[outputs]\nstatus = \"f1\"\nsummary = \"f2\"\nfiles = \"f3\"\n"
        "[status]\nclaim_from = [\"A\"]\nprocessing = \"P\"\ndone = \"D\"\nerror = \"E\"\n"
        "[[sources]]\nkey = \"s\"\nlabel = \"S\"\nsource = \"nope\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        mapping.load_mapping(bad_source)


def _source(**overrides):
    base = {
        "key": "s", "label": "S", "source": "receita",
        "columns": {"documento": "fld1"}, "transforms": {"documento": "digits"},
        "params": {"fixed": 1},
    }
    base.update(overrides)
    return mapping.SourceMap(**base)


def test_build_kwargs_and_missing():
    source = _source()
    kwargs, missing = mapping.build_kwargs(source, {"fld1": " 123.456 "})
    assert (kwargs, missing) == ({"fixed": 1, "documento": "123456"}, [])
    _, missing = mapping.build_kwargs(source, {})
    assert missing == ["documento"]
    _, missing = mapping.build_kwargs(source, {"fld1": "   "})
    assert missing == ["documento"]


def test_summary_and_status():
    ok = mapping.CertOutcome("A", True)
    fail = mapping.CertOutcome("B", False, "documento_invalido", "Invalid CPF")
    lines = mapping.summary_lines([ok, fail])
    assert lines == ["\u2022 [A] pronta", "\u2022 [B] FALHA (documento_invalido): Invalid CPF"]
    assert "124" not in "\n".join(lines)
    status = mapping.StatusSet(("P",), "W", "D", "E")
    assert mapping.derive_status([ok], status) == "D"
    assert mapping.derive_status([fail], status) == "E"
    assert mapping.derive_status([ok, fail], status) == "D"
    assert mapping.derive_status([], status) == "E"


def test_attachment_objects():
    assert attachment_objects(["https://x/f/a.pdf"]) == [{"url": "https://x/f/a.pdf"}]


def test_artifact_store_roundtrip(tmp_path):
    store = LocalPublicDir(tmp_path / "served", "https://files.example.test")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    url = store.publish("c.pdf", str(pdf))
    assert url.startswith("https://files.example.test/files/")
    assert url.endswith("/certificate.pdf")
    assert "c.pdf" not in url
    token = url.split("/files/")[1].split("/")[0]
    path, filename = store.resolve(token)
    assert filename == "c.pdf" and path.read_bytes() == b"%PDF-1.4 fake"
    assert pdf.exists()  # original preserved; only the served copy is dropped
    store.discard(url)
    assert store.resolve(token) is None
    assert not path.exists()


@dataclasses.dataclass(frozen=True)
class _FakeRequest:
    documento: str
    output_dir: object = None


class _FakeProvider(TableProvider):
    name = "fake"

    def __init__(self, fields, writes=None):
        self._fields = fields
        self.writes = writes if writes is not None else []

    def fetch(self, ref):
        return JobInputs(ref_label="recX", fields=dict(self._fields),
                         status=self._fields.get("fldS"))

    def claim(self, ref, processing_value, claim_from):
        inputs = self.fetch(ref)
        if inputs.status not in claim_from:
            return None
        self._fields["fldS"] = processing_value
        return inputs

    def publish(self, ref, *, status, summary, attachment_urls):
        self.writes.append({"status": status, "summary": summary, "files": attachment_urls})
        return {}

    def column_report(self, table, columns):
        return {column: "seen" for column in columns}


def _fake_mapping():
    return mapping.Mapping(
        base_id="appX",
        table="tblX",
        provider="fake",
        sources=(
            mapping.SourceMap(
                key="one", label="One", source="stub",
                columns={"documento": "fld1"}, transforms={"documento": "digits"},
            ),
            mapping.SourceMap(
                key="two", label="Two", source="stub",
                columns={"documento": "fldMissing"},
            ),
        ),
        outputs=mapping.Outputs(status="fldS", summary="fldE", files="fldF"),
        status=mapping.StatusSet(("PENDENTE",), "PROCESSANDO", "FINALIZADO", "FALHA"),
    )


def _run_with(monkeypatch, tmp_path, fields, generate):
    monkeypatch.setitem(
        registry._CACHE, "stub",
        registry.SourceSpec("stub", _FakeRequest, generate),
    )
    store = LocalPublicDir(tmp_path / "served", "https://files.example.test")
    provider = _FakeProvider(fields)
    report = run_job(provider, _fake_mapping(), {"record_id": "recX"},
                     store, tmp_path / "out")
    return provider, report


def test_run_job_mixed_results(monkeypatch, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    def generate(request):
        if request.documento == "111":
            return CertificateResult("s", "CPF", "c.pdf", pdf)
        raise CertificateError("s", "input", "documento_invalido", "Invalid CPF")

    provider, report = _run_with(
        monkeypatch, tmp_path, {"fldS": "PENDENTE", "fld1": "111"}, generate
    )
    assert report.status == "FINALIZADO"
    assert (report.ok, report.failed) == (1, 1)
    assert report.lines[1].startswith("\u2022 [Two] FALHA (coluna_ausente)")
    final = provider.writes[-1]
    assert final["status"] == "FINALIZADO"
    assert len(final["files"]) == 1 and final["files"][0].startswith("https://files.example.test/files/")
    assert len(list((tmp_path / "served").iterdir())) == 1  # Airtable fetches after PATCH returns
    assert "111" not in json.dumps({"lines": list(report.lines)})


def test_run_job_skips_non_claimable(monkeypatch, tmp_path):
    def generate(request):  # pragma: no cover - must not run
        raise AssertionError("should not generate")

    provider, report = _run_with(
        monkeypatch, tmp_path, {"fldS": "FINALIZADO", "fld1": "111"}, generate
    )
    assert report.skipped is True
    assert provider.writes == []


def test_api_endpoints(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from certidoes.ingest import api as api_mod

    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTEST")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://files.example.test")
    monkeypatch.setenv("INGEST_SECRET", "s3cret")
    app = api_mod.create_app(PESSOAS_TOML)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/healthz").json() == {"ok": True}

    response = client.post("/jobs", json={"provider": "airtable", "ref": {"record_id": "recX"}})
    assert response.status_code == 401

    def fake_run_job(provider, mapping_arg, ref, store, output_base):
        assert ref["record_id"] == "recX"
        return JobReport(ref_label="recX", skipped=False, status="FINALIZADO",
                         ok=1, failed=0, lines=("x",))

    monkeypatch.setattr(api_mod, "run_job", fake_run_job)
    response = client.post(
        "/jobs",
        json={"provider": "airtable", "ref": {"record_id": "recX"}},
        headers={"X-Ingest-Secret": "s3cret"},
    )
    assert response.status_code == 202
    assert response.json() == {"accepted": True}

    response = client.post(
        "/jobs", json={"provider": "airtable", "ref": {}},
        headers={"X-Ingest-Secret": "s3cret"},
    )
    assert response.status_code == 400


def test_api_serializes_jobs(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from certidoes.ingest import api as api_mod

    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTEST")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://files.example.test")
    monkeypatch.setenv("INGEST_SECRET", "s3cret")
    active = completed = peak = 0
    lock = threading.Lock()
    finished = threading.Event()

    def fake_run_job(provider, mapping_arg, ref, store, output_base):
        nonlocal active, completed, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
            completed += 1
            if completed == 2:
                finished.set()
        return JobReport(ref_label=ref["record_id"], skipped=False,
                         status="FINALIZADO", ok=1, failed=0)

    monkeypatch.setattr(api_mod, "run_job", fake_run_job)
    with TestClient(api_mod.create_app(PESSOAS_TOML)) as client:
        def submit(record_id):
            return client.post(
                "/jobs",
                json={"provider": "airtable", "ref": {"record_id": record_id}},
                headers={"X-Ingest-Secret": "s3cret"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as callers:
            assert list(callers.map(submit, ("rec1", "rec2"))) == [202, 202]
        assert finished.wait(2)
        assert peak == 1


def test_local_provider_claim_publish_skip(tmp_path):
    from certidoes.ingest.local_provider import LocalProvider

    provider = LocalProvider(tmp_path / "reports")
    ref = {"label": "job1", "status": "PENDENTE", "fields": {"documento": "123"}}
    inputs = provider.claim(ref, "PROCESSANDO", ("PENDENTE",))
    assert inputs is not None and inputs.fields == {"documento": "123"}
    assert provider.claim(ref, "PROCESSANDO", ("PENDENTE",)) is None
    report = provider.publish(ref, status="FINALIZADO", summary="ok", attachment_urls=["https://x/f.pdf"])
    assert report["status"] == "FINALIZADO"
    saved = json.loads((tmp_path / "reports" / "job1.json").read_text(encoding="utf-8"))
    assert saved["attachments"] == ["https://x/f.pdf"]
    assert provider.column_report("local", ["documento"]) == {
        "documento": "inline (provided at request time)"}


def test_load_dotenv(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("INGEST_DOTENV_TEST", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nINGEST_DOTENV_TEST = hello \n", encoding="utf-8")
    worker_mod.load_dotenv(env_file)
    assert os.environ["INGEST_DOTENV_TEST"] == "hello"
    del os.environ["INGEST_DOTENV_TEST"]
