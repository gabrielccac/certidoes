import asyncio
import json
from unittest.mock import Mock

import mycdp
import pytest

from experiments.receita_direct_cdp_probe.client import CommandError, DirectChrome


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
