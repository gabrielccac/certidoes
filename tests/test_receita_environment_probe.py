import json
import sys
from unittest.mock import Mock

from experiments.receita_environment_probe import probe


def test_payload_metadata_contains_comparisons_without_values():
    document, birth = "12433377617", "2002-05-23"
    result = probe.payload_metadata({"ni": document, "dataNascimento": birth,
                                     "tipoContribuinte": "PF"}, document, birth)
    assert result["document_matches"] and result["birth_matches"]
    assert document not in json.dumps(result) and birth not in json.dumps(result)
    assert probe.payload_metadata(None, document, birth) == {"object": False}


def test_network_metadata_excludes_ip(monkeypatch):
    response = Mock(text="ip=203.0.113.9\nloc=BR\ncolo=GIG\nhttp=h2\ntls=TLSv1.3\n")
    monkeypatch.setattr(probe.requests, "get", Mock(return_value=response))
    result = probe.network_metadata()
    assert result == {"loc": "BR", "colo": "GIG", "http": "h2", "tls": "TLSv1.3"}
    assert "203.0.113.9" not in json.dumps(result)


def test_headless_startup_failure_closes_browser_and_restores_hooks(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["probe", "--mode", "headless", "--output", str(tmp_path)])
    monkeypatch.delenv("RECEITA_PAYLOAD", raising=False)
    monkeypatch.setenv("LIVE_DOCUMENT", "12433377617")
    monkeypatch.setenv("LIVE_BIRTH", "2002-05-23")
    monkeypatch.setenv("RECEITA_EXECUTOR", "local")
    monkeypatch.setattr(probe, "network_metadata", lambda: {"loc": "BR"})
    monkeypatch.setattr(probe.receita, "_chrome_path", lambda path: "chrome")
    chrome = Mock()
    chrome.start.side_effect = RuntimeError("startup failed")
    monkeypatch.setattr(probe.receita, "CdpChrome", Mock(return_value=chrome))
    original_browser, original_log = probe.receita._browser, probe.receita._log
    original_popen = probe.subprocess.Popen
    assert probe.main() == 1
    chrome.close.assert_called_once()
    assert probe.receita._browser is original_browser and probe.receita._log is original_log
    assert probe.subprocess.Popen is original_popen
    report = json.loads((tmp_path / "result.json").read_text())
    assert report["stage"] == "browser"
    assert "12433377617" not in json.dumps(report) and "2002-05-23" not in json.dumps(report)
