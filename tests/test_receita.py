from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from certidoes.errors import CertificateError
from certidoes.sources import receita
from certidoes.sources.receita import ReceitaRequest, _normalize, _require_ok
from certidoes.captcha import hcaptcha, solver
from certidoes.captcha.extractor import find_grid


class CaptchaPage:
    def __init__(self, token="", widgets=None):
        self.state = {"token": token, "error": False}
        self.widgets = widgets or ["widget"]
        self.started = []

    def wait_for_selector(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, arg=None):
        if "__certidoesCaptcha =" in script:
            self.started.append(arg)
        elif "__certidoesCaptcha" in script:
            return self.state
        elif "data-hcaptcha-widget-id" in script:
            return self.widgets
        elif "hcaptcha.execute" in script:
            return True
        elif script == hcaptcha.IFRAME_JS:
            return []
        else:
            raise AssertionError(script)


def test_token_without_visual_challenge_needs_no_model_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    page = CaptchaPage(token="fresh-token")
    assert receita._token(page, 1000) == "fresh-token"
    assert page.started == ["widget"]


def test_solves_visual_challenge_while_execute_is_pending(monkeypatch, tmp_path):
    page = CaptchaPage()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("RECEITA_CAPTCHA_MODEL", "test-model")
    monkeypatch.setattr(hcaptcha, "vis_modal", lambda page: [] if page.state["token"] else [{}])

    def solve_round(page, directory, rnd, model, **kwargs):
        assert Path(directory) == tmp_path and rnd == 0 and model == "test-model"
        page.state["token"] = "solved-token"
        return True, {"submitted": True}

    monkeypatch.setattr(hcaptcha, "solve_round", solve_round)
    assert receita._token(page, 1000, output_dir=tmp_path) == "solved-token"
    assert page.started == ["widget"]


def test_visual_challenge_reports_missing_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(hcaptcha, "vis_modal", lambda page: [{}])
    with pytest.raises(CertificateError) as raised:
        receita._token(CaptchaPage(), 1000)
    assert raised.value.code == "captcha_config_missing"
    assert not raised.value.retryable


def test_rejects_non_string_token():
    with pytest.raises(CertificateError) as raised:
        receita._token(CaptchaPage(token={"unexpected": "value"}), 1000)
    assert raised.value.code == "hcaptcha_falhou"


def test_restarts_primary_widget_after_gate_reloads_page():
    class ReloadPage(CaptchaPage):
        def evaluate(self, script, arg=None):
            if script == "() => window.__certidoesCaptcha" and len(self.started) == 1:
                return None
            return super().evaluate(script, arg)

    page = ReloadPage(token="receita-token", widgets=["receita-widget", "gate-widget"])
    assert receita._token(page, 1000) == "receita-token"
    assert page.started == ["receita-widget", "receita-widget"]


@pytest.mark.parametrize("info", [
    {"why": "interrupted-clicks"},
    {"solve_error": "provider error"},
])
def test_aborts_failed_round_without_reclicking(monkeypatch, info):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(hcaptcha, "vis_modal", lambda page: [{}])
    solve = Mock(return_value=(False, info))
    monkeypatch.setattr(hcaptcha, "solve_round", solve)
    with pytest.raises(CertificateError) as raised:
        receita._token(CaptchaPage(), 1000)
    assert raised.value.code == "hcaptcha_falhou"
    solve.assert_called_once()


def test_limits_visual_rounds(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(hcaptcha, "vis_modal", lambda page: [{}])
    solve = Mock(return_value=(False, {"next_round_ready": True}))
    monkeypatch.setattr(hcaptcha, "solve_round", solve)
    with pytest.raises(CertificateError):
        receita._token(CaptchaPage(), 1000)
    assert solve.call_count == 6


def test_browser_closes_on_missing_solver_configuration(monkeypatch):
    monkeypatch.setattr(receita.sys, "platform", "win32")
    browser = Mock()
    monkeypatch.setattr(receita, "CdpChrome", Mock(return_value=browser))
    monkeypatch.setattr(receita, "_chrome_path", lambda path: "chrome")
    error = CertificateError("receita", "challenge", "captcha_config_missing", "missing")
    token = Mock(side_effect=error)
    monkeypatch.setattr(receita, "_token", token)
    with pytest.raises(CertificateError) as raised:
        receita._fresh_token(ReceitaRequest("12433377617", "2002-05-23"), "CPF")
    assert raised.value is error
    token.assert_called_once()
    browser.close.assert_called_once()


def test_native_browser_cleans_up_after_startup_failure(monkeypatch):
    from certidoes.captcha import browser

    monkeypatch.setattr(receita.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(receita, "_chrome_path", lambda path: "chrome")
    manager = Mock()
    manager.start.side_effect = RuntimeError("Chrome startup failed")
    monkeypatch.setattr(browser, "ManagedBrowser", Mock(return_value=manager))
    with pytest.raises(RuntimeError, match="Chrome startup failed"):
        with receita._browser(ReceitaRequest("12433377617", "2002-05-23")):
            pytest.fail("Failed browser must not provide a page")
    manager.stop.assert_called_once()


def test_linux_uses_existing_desktop_session(monkeypatch):
    monkeypatch.setattr(receita.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setattr(receita, "_chrome_path", lambda path: "chrome")
    chrome = Mock()
    factory = Mock(return_value=chrome)
    monkeypatch.setattr(receita, "CdpChrome", factory)
    with receita._browser(ReceitaRequest("12433377617", "2002-05-23")) as page:
        assert page is chrome.start.return_value
    factory.assert_called_once()
    chrome.close.assert_called_once()


def test_solver_sends_only_cropped_images_and_validates_indices(monkeypatch):
    from PIL import Image

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": "[9,1,1,0,10]"}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(solver.requests, "post", post)
    crop = Image.new("RGB", (4, 4), "black")
    assert solver.solve(crop, [crop] * 9, "test-model") == [1, 9]
    content = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert sum(item["type"] == "image_url" for item in content) == 10
    response.raise_for_status.assert_called_once()


def test_recognizes_wide_modal_and_ignores_hidden_frames():
    page = Mock()
    compact = {"x": 373, "y": 122, "w": 520, "h": 570, "visible": True, "challenge": True}
    lower = {"x": 50, "y": 350, "w": 620, "h": 750, "visible": True, "challenge": True}
    page.evaluate.return_value = [
        compact, lower,
        {"x": 1, "y": -9999, "w": 300, "h": 150, "visible": False, "challenge": True},
        {"x": 10, "y": 10, "w": 300, "h": 74, "visible": True, "challenge": False},
    ]
    assert hcaptcha.vis_modal(page) == [compact, lower]


def test_captcha_diagnostic_compares_tokens_without_logging_values(tmp_path, capsys):
    import json

    page = Mock()
    token = "private-token-not-for-logs"
    other = "other-private-response"
    page.evaluate.side_effect = [[], {"ready": True, "responses": [token, other]}]
    receita._trace_captcha(page, tmp_path, "verification_request", token)
    contents = (tmp_path / "state-verification_request.json").read_text()
    state = json.loads(contents)
    assert state["submitted_token_length"] == len(token)
    assert state["submitted_matches_widgets"] == [True, False]
    logged = capsys.readouterr().err + contents
    assert token not in logged and other not in logged


def test_finds_centered_grid_in_wide_canvas():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (520, 570), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 509, 117), fill=(0, 128, 140))
    draw.rectangle((10, 130, 509, 479), fill=(235, 235, 235))
    draw.rectangle((20, 145, 499, 464), fill=(216, 216, 216))
    for row in range(3):
        for col in range(3):
            draw.rectangle((89 + col * 344 // 3, 145 + row * 320 // 3,
                            88 + (col + 1) * 344 // 3, 144 + (row + 1) * 320 // 3),
                           fill=(30 + row * 30, 40 + col * 30, 70))
    assert find_grid(image) == (89, 148, 344, 314)


@pytest.mark.parametrize("reply", [
    '{"start":[500,320],"end":[640,710]}',
    '{"start":[-1,320],"end":[640,710]}',
    '{"start":[500,320],"end":[640,1001]}',
    '{"start":[true,320],"end":[640,710]}',
    '{"start":[500,320],"end":[500,320]}',
    '{"start":[500,945],"end":[500,500]}',
])
def test_validates_drag_coordinates(monkeypatch, reply):
    from PIL import Image

    monkeypatch.setattr(solver, "_completion", lambda *args: reply)
    image = Image.new("RGB", (520, 570), "white")
    if reply == '{"start":[500,320],"end":[640,710]}':
        assert solver.solve_drag(image, "test") == {"start": [500, 320], "end": [640, 710]}
    else:
        with pytest.raises(ValueError):
            solver.solve_drag(image, "test")


def test_detects_drag_from_visible_frame_prompt():
    frame = Mock(url="https://newassets.hcaptcha.com/challenge")
    frame.frame_element.return_value.bounding_box.return_value = {"x": 373, "y": 122}
    frame.evaluate.return_value = "Please drag the shape to the loop that is closed"
    page = Mock(frames=[frame])
    assert hcaptcha.is_drag_challenge(page, {"x": 373, "y": 122})
    assert not hcaptcha.is_drag_challenge(page, {"x": 0, "y": -9999})


def test_drag_waits_for_scene_to_load():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (520, 570), "white")
    assert not hcaptcha.drag_ready(image)
    ImageDraw.Draw(image).ellipse((200, 200, 300, 300), fill="purple")
    assert hcaptcha.drag_ready(image)


@pytest.mark.parametrize("document,birth", [("12433377617", "2002-05-23"), ("11222333000181", None)])
@pytest.mark.parametrize("verification_status", [200, 400])
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_native_issuance_keeps_browser_alive_and_retries_processing(monkeypatch, tmp_path, document, birth, verification_status, platform):
    import base64

    monkeypatch.setenv("RECEITA_EXECUTOR", "local")
    monkeypatch.setattr(receita.sys, "platform", platform)
    monkeypatch.setattr(hcaptcha, "vis_modal", lambda page: [])
    monkeypatch.setattr(receita, "_trace_captcha", lambda *args: None)
    page = Mock()
    alive = []
    events = {}
    page.on.side_effect = lambda name, callback: events.update({name: callback})
    verification = SimpleNamespace(url=receita.VERIFICAR_URL, request=SimpleNamespace(method="POST"),
                                   status=verification_status,
                                   json=lambda: {"status": "Emitida", "statusValidacao": "CaptchaFalhaValidacao"})
    page.get_by_role.return_value.click.side_effect = lambda **kwargs: events["response"](verification)
    pdf = b"%PDF-1.4 test certificate"
    emissions = iter([{"statusEmissao": "EmProcessamento"}, {"pdf": base64.b64encode(pdf).decode()}])

    def evaluate(script, parameters):
        assert alive == [True]
        assert parameters["payload"]["ni"] == document
        return {"status": 200, "data": next(emissions)}

    page.evaluate.side_effect = evaluate

    @contextmanager
    def browser(request):
        alive.append(True)
        try:
            yield page
        finally:
            alive.clear()

    monkeypatch.setattr(receita, "_browser", browser)
    request = ReceitaRequest(document, birth, output_dir=tmp_path)
    if verification_status == 400:
        with pytest.raises(CertificateError) as error:
            receita.generate(request)
        assert error.value.code == "captcha_rejeitado"
        page.evaluate.assert_not_called()
        assert not list(tmp_path.glob("*.pdf"))
    else:
        result = receita.generate(request)
        assert result.pdf_path.read_bytes() == pdf
        assert result.metadata["executor"] == "browser"
        page.wait_for_timeout.assert_called_once_with(1000)
    assert alive == []


def test_normalizes_formatted_cpf():
    assert _normalize(ReceitaRequest("124.333.776-17", "2002-05-23")) == ("12433377617", "CPF", "PF")


def test_late_cookie_banner_is_dismissed_before_form_action(monkeypatch, tmp_path):
    import base64

    monkeypatch.setenv("RECEITA_EXECUTOR", "local")
    monkeypatch.setattr(receita, "_trace_captcha", lambda *args: None)
    monkeypatch.setattr(hcaptcha, "vis_modal", lambda page: [])
    page, consent, submit = Mock(), Mock(), Mock()
    page.get_by_role.side_effect = lambda role, name, exact: consent if name == "Aceitar" else submit
    handlers, events = {}, {}
    page.add_locator_handler.side_effect = lambda locator, handler, **kwargs: handlers.update({locator: handler})
    page.on.side_effect = lambda name, callback: events.update({name: callback})

    def fill(value):
        # The banner appears after navigation, during the first form action.
        if not consent.click.called:
            handlers[consent](consent)
        assert consent.click.called

    page.locator.return_value.fill.side_effect = fill
    submit.click.side_effect = lambda **kwargs: events["response"](SimpleNamespace(
        url=receita.VERIFICAR_URL, request=SimpleNamespace(method="POST"),
        status=200, json=lambda: {"status": "Emitida"}))
    pdf = b"%PDF-1.4 cookie banner handled"
    page.evaluate.return_value = {"status": 200, "data": {"pdf": base64.b64encode(pdf).decode()}}

    @contextmanager
    def browser(request):
        yield page

    monkeypatch.setattr(receita, "_browser", browser)
    result = receita.generate(ReceitaRequest("12433377617", "2002-05-23", output_dir=tmp_path))
    assert result.pdf_path.read_bytes() == pdf
    consent.click.assert_called_once()
    submit.click.assert_called_once()


def test_windows_form_waits_for_visual_solver_before_verification(monkeypatch, tmp_path, capsys):
    import base64

    monkeypatch.setenv("RECEITA_EXECUTOR", "local")
    monkeypatch.setattr(receita.sys, "platform", "win32")
    monkeypatch.setattr(receita, "_trace_captcha", lambda *args: None)
    page = Mock()
    events = {}
    state = {"waiting": False, "alive": False}
    page.on.side_effect = lambda name, callback: events.update({name: callback})
    page.get_by_role.return_value.click.side_effect = lambda **kwargs: state.update(waiting=True)
    monkeypatch.setattr(hcaptcha, "vis_modal", lambda page: [{}] if state["waiting"] else [])

    def solve_visual(page, directory, round_number):
        assert state == {"waiting": True, "alive": True}
        assert round_number == 0
        events["response"](SimpleNamespace(url=receita.VERIFICAR_URL,
            request=SimpleNamespace(method="POST"), status=200, json=lambda: {"status": "Emitida"}))
        state["waiting"] = False

    solve = Mock(side_effect=solve_visual)
    monkeypatch.setattr(receita, "_solve_visual", solve)
    pdf = b"%PDF-1.4 solved"
    page.evaluate.return_value = {"status": 200, "data": {"pdf": base64.b64encode(pdf).decode()}}

    @contextmanager
    def browser(request):
        state["alive"] = True
        try:
            yield page
        finally:
            state["alive"] = False

    monkeypatch.setattr(receita, "_browser", browser)
    result = receita.generate(ReceitaRequest("12433377617", "2002-05-23", output_dir=tmp_path))
    assert result.pdf_path.read_bytes() == pdf
    assert result.metadata["captcha_rounds"] == 1
    solve.assert_called_once()
    logs = capsys.readouterr().err
    assert '"event": "verification_received", "http": 200, "captcha_rounds": 1' in logs
    assert "12433377617" not in logs and "2002-05-23" not in logs


@pytest.mark.parametrize("retryable,expected_calls", [(True, 3), (False, 1)])
def test_browser_retry_limit_preserves_error(monkeypatch, retryable, expected_calls):
    monkeypatch.setenv("RECEITA_EXECUTOR", "local")
    error = CertificateError("receita", "challenge", "captcha_rejeitado", "rejected", retryable=retryable)
    browser_generate = Mock(side_effect=error)
    monkeypatch.setattr(receita, "_browser_generate", browser_generate)
    with pytest.raises(CertificateError) as raised:
        receita.generate(ReceitaRequest("12433377617", "2002-05-23", captcha_retries=3))
    assert raised.value is error
    assert browser_generate.call_count == expected_calls


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


def test_retries_receita_while_emission_is_processing(monkeypatch):
    pdf = b"%PDF-1.4 ready"
    responses = iter([
        type("Response", (), {"status_code": 200, "json": lambda self: {
            "statusEmissao": "EmProcessamento"}})(),
        type("Response", (), {"status_code": 200, "json": lambda self: {
            "pdf": __import__("base64").b64encode(pdf).decode()}})(),
    ])
    calls = []
    monkeypatch.setattr(receita, "_post", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(receita.time, "sleep", calls.append)

    result = receita._request_emissao_pdf(
        object(), {"ni": "x"}, cookies={}, user_agent="ua", timeout=30,
    )

    assert result == pdf
    assert calls == [1]
