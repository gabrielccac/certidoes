import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from experiments.receita_environment_probe import pure_cdp


def test_verification_metadata_redacts_other_fields():
    data = pure_cdp.verification_metadata(400, {
        "status": "Emitida", "statusValidacao": "2002-05-23",
        "documento": "12433377617", "token": "secret-token"})
    assert data == {"http": 400, "status": "Emitida"}
    assert "secret" not in json.dumps(data)


def test_pure_cdp_quits_after_navigation_failure(monkeypatch, tmp_path):
    sb = Mock()
    sb.page.send.return_value = ("1.3", "Chrome/155.0.0.0", "", "", "")
    sb.get_event_loop.return_value.run_until_complete.side_effect = lambda result: result
    sb.goto.side_effect = RuntimeError("navigation failed")
    chrome = Mock(return_value=sb)
    monkeypatch.setitem(sys.modules, "seleniumbase", SimpleNamespace(sb_cdp=SimpleNamespace(Chrome=chrome)))
    monkeypatch.setitem(sys.modules, "mycdp", SimpleNamespace(
        browser=Mock(), network=Mock()))
    monkeypatch.setattr(pure_cdp.receita, "_chrome_path", lambda path: "chrome")
    report = {"sdk_loads": []}
    with pytest.raises(RuntimeError, match="navigation failed"):
        pure_cdp.run(pure_cdp.receita.ReceitaRequest("12433377617", "2002-05-23",
            output_dir=tmp_path, profile_dir=tmp_path / "profile"),
            android_port=None, single_sdk=False, report=report)
    sb.quit.assert_called_once()
    assert "playwright_imported" in report
