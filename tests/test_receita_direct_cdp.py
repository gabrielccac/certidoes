import asyncio
import json
from unittest.mock import Mock

import pytest

mycdp = pytest.importorskip("mycdp", reason="optional direct-CDP experiment dependencies")
pytest.importorskip("websocket", reason="optional direct-CDP experiment dependencies")

from experiments.receita_direct_cdp_probe.client import CommandError, DirectChrome


def test_unused_chrome133_extra_info_does_not_break_command_response(monkeypatch):
    event = {"method": "Network.requestWillBeSentExtraInfo", "params": {
        "requestId": "sample", "associatedCookies": [], "headers": {},
        "connectTiming": {"requestTime": 1}, "clientSecurityState": {
            "initiatorIsSecureContext": True, "initiatorIPAddressSpace": "Local",
            "privateNetworkRequestPolicy": "Allow"}}}
    with pytest.raises(ValueError, match="LocalNetworkAccessRequestPolicy"):
        mycdp.util.parse_json_event(event)
    c = client_with_messages(monkeypatch, [event, {"id": 1, "result": {"ok": True}}])
    c.add_handler(mycdp.network.ResponseReceived, lambda event: None)
    try:
        assert c.call("Runtime.evaluate") == {"ok": True}
    finally:
        c.quit()


def test_invalid_required_event_is_reported_instead_of_silently_ignored(monkeypatch):
    c = client_with_messages(monkeypatch, [{"method": "Network.loadingFinished", "params": {"requestId": "sample"}}])
    c.add_handler(mycdp.network.LoadingFinished, lambda event: None)
    try:
        with pytest.raises(CommandError) as error:
            c.call("Runtime.evaluate")
        assert error.value.method == "Network.loadingFinished"
        assert error.value.code.startswith("event_decode_")
    finally:
        c.quit()


def test_android_preflight_failure_prevents_both_receita_cases(monkeypatch, tmp_path):
    from experiments.receita_direct_cdp_probe import android
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTROL_PYTHON", "/unused/control/python")
    monkeypatch.setattr("sys.argv", ["android"])
    values = {"ro.build.version.sdk": "36", "ro.build.version.release": "16", "ro.kernel.qemu": "1"}

    def adb(*args, **kwargs):
        if args[:2] == ("shell", "getprop"):
            return values[args[2]]
        if args[:2] == ("shell", "dumpsys"):
            return "versionName=133.0.6943.137"
        if args[:3] == ("shell", "pm", "clear"):
            return "Success"
        return ""

    monkeypatch.setattr(android, "adb", adb)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"User-Agent":"Android"}'
    monkeypatch.setattr(android, "urlopen", lambda *a, **k: response)
    process = Mock(returncode=1)
    run = Mock(return_value=process)
    monkeypatch.setattr(android.subprocess, "run", run)
    assert android.main() == 1
    assert run.call_count == 1
    assert "experiments.receita_direct_cdp_probe.smoke" in run.call_args.args[0]


def test_local_case_does_not_print_identity(monkeypatch, tmp_path, capsys):
    from experiments.receita_direct_cdp_probe import case
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("RECEITA_PAYLOAD", raising=False)
    monkeypatch.setenv("LIVE_DOCUMENT", "12345678900")
    monkeypatch.setenv("LIVE_BIRTH", "1990-12-31")
    monkeypatch.setattr("sys.argv", ["case", "--engine", "direct", "--output", str(tmp_path / "case")])
    monkeypatch.setattr(case.util, "find_spec", lambda name: None)
    monkeypatch.setattr(case.reference, "run", Mock(side_effect=RuntimeError("test stop")))
    assert case.main() == 1
    text = capsys.readouterr().out
    assert "12345678900" not in text and "1990-12-31" not in text


def client_with_messages(monkeypatch, messages):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps([{"type": "page", "webSocketDebuggerUrl": "ws://localhost:9229/test"}]).encode()
    monkeypatch.setattr("experiments.receita_direct_cdp_probe.client.urlopen", lambda *a, **k: response)
    socket = Mock()
    socket.recv.side_effect = [json.dumps(value) for value in messages]
    monkeypatch.setattr("experiments.receita_direct_cdp_probe.client.websocket.create_connection", lambda *a, **k: socket)
    return DirectChrome(host="127.0.0.1", port=9229)


def test_transport_preserves_network_events_interleaved_with_command_response(monkeypatch):
    c = client_with_messages(monkeypatch, [
        {"method": "Network.loadingFinished", "params": {"requestId": "sample", "timestamp": 1, "encodedDataLength": 12}},
        {"id": 1, "result": {}},
    ])
    events = []
    c.add_handler(mycdp.network.LoadingFinished, events.append)
    try:
        assert c.loop.run_until_complete(c.send(mycdp.network.enable())) is None
        assert str(events[0].request_id) == "sample"
        assert json.loads(c.socket.send.call_args.args[0])["method"] == "Network.enable"
    finally:
        c.quit()


def test_protocol_errors_are_not_mistaken_for_success(monkeypatch):
    c = client_with_messages(monkeypatch, [{"id": 1, "error": {"code": -32000, "message": "private content"}}])
    try:
        with pytest.raises(CommandError) as error:
            c.call("Runtime.evaluate")
        assert "private content" not in str(error.value)
    finally:
        c.quit()


def test_async_runtime_result_keeps_page_emission_body_intact(monkeypatch):
    c = client_with_messages(monkeypatch, [{"id": 1, "result": {"result": {"type": "object", "value": {"pdf": "sample"}}}}])
    try:
        assert c.loop.run_until_complete(c.page.evaluate("expression", await_promise=True)) == {"pdf": "sample"}
    finally:
        c.quit()


def test_javascript_exception_is_not_returned_as_empty_pdf(monkeypatch):
    c = client_with_messages(monkeypatch, [{"id": 1, "result": {"exceptionDetails": {"text": "private content"}, "result": {}}}])
    try:
        with pytest.raises(CommandError):
            c.evaluate("expression")
    finally:
        c.quit()


def test_navigation_does_not_confuse_old_about_blank_with_the_loaded_portal(monkeypatch):
    c = client_with_messages(monkeypatch, [])
    c.call = Mock(return_value={})
    c.evaluate = Mock(side_effect=[{"url": "about:blank", "ready": "complete"},
                                  {"url": "https://example.test/", "ready": "complete"}])
    c.send = Mock(side_effect=lambda command: asyncio.sleep(0))
    try:
        c.goto("https://example.test/")
        assert c.evaluate.call_count == 2
    finally:
        c.quit()
