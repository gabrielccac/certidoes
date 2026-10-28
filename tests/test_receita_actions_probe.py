import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from experiments.receita_actions_probe import runner


def test_seleniumbase_probe_closes_launcher_after_issuance_failure(monkeypatch, tmp_path):
    import playwright.sync_api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["runner", "--browser", "seleniumbase"])
    monkeypatch.delenv("RECEITA_PAYLOAD", raising=False)
    monkeypatch.setenv("LIVE_DOCUMENT", "12433377617")
    monkeypatch.setenv("LIVE_BIRTH", "2002-05-23")
    monkeypatch.setattr(runner, "version", lambda package: "test-version")
    monkeypatch.setattr(runner.receita, "_chrome_path", lambda path: "chrome")
    launcher = Mock()
    chrome = Mock(return_value=launcher)
    monkeypatch.setitem(sys.modules, "seleniumbase", SimpleNamespace(sb_cdp=SimpleNamespace(Chrome=chrome)))
    page = Mock(context=SimpleNamespace(browser=SimpleNamespace(version="test-browser")))
    page.evaluate.return_value = {"platform": "Win32"}
    manager = MagicMock()
    manager.__enter__.return_value.chromium.connect_over_cdp.return_value.contexts = [
        SimpleNamespace(pages=[page])]
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: manager)
    original_browser = runner.receita._browser

    def fail_issuance(request):
        with runner.receita._browser(request) as actual_page:
            assert actual_page is page
            raise RuntimeError("issuance failed")

    monkeypatch.setattr(runner.receita, "generate", fail_issuance)
    assert runner.main() == 1
    launcher.quit.assert_called_once()
    manager.__exit__.assert_called_once()
    assert runner.receita._browser is original_browser
    report = json.loads((tmp_path / "runs/actions-browser-probe/seleniumbase/result.json").read_text())
    assert report["error_type"] == "RuntimeError"
    assert "12433377617" not in json.dumps(report)
