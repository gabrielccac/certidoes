import json

import pytest

from experiments.receita_environment_probe.android_openatx import bounds, chrome_dialog_texts, inputs, ui_metadata


def test_native_inputs_exclude_chrome_address_bar_and_empty_bounds():
    xml = '''<hierarchy>
      <node class="android.widget.EditText" package="com.android.chrome" resource-id="com.android.chrome:id/url_bar" bounds="[0,0][100,30]"/>
      <node class="android.widget.EditText" package="com.android.chrome" text="birth" bounds="[10,200][100,240]"/>
      <node class="android.widget.EditText" package="com.android.chrome" text="document" bounds="[10,100][100,140]"/>
      <node class="android.widget.EditText" package="com.android.chrome" bounds="[0,0][0,0]"/>
      <node class="android.widget.EditText" package="other.app" bounds="[10,40][100,80]"/>
    </hierarchy>'''
    assert [node.get("text") for node in inputs(xml)] == ["document", "birth"]


def test_ui_metadata_does_not_publish_input_values_or_unknown_page_text():
    xml = '''<hierarchy>
      <node text="123.456.789-00" resource-id="Certidao-12345678900" content-desc="secret token"/>
      <node text="31/12/1990"/><node text="person name"/>
      <node text="Emitir Certidão"/>
    </hierarchy>'''
    serialized = json.dumps(ui_metadata(xml), ensure_ascii=False)
    for secret in ("123", "789", "1990", "person name", "secret token"):
        assert secret not in serialized
    assert "Emitir Certidão" in serialized


def test_chrome_startup_dialog_excludes_page_inputs_and_unrelated_nodes():
    xml = '''<hierarchy>
      <node text="private page content" class="android.widget.TextView"/>
      <node resource-id="com.android.chrome:id/modal_dialog_view">
        <node class="android.widget.TextView" text="Chrome setup"/>
        <node class="android.widget.Button" text="Not now"/>
        <node class="android.widget.EditText" text="private input"/>
      </node>
    </hierarchy>'''
    assert chrome_dialog_texts(xml) == ["Chrome setup", "Not now"]


@pytest.mark.parametrize("value", ["[-1,0][100,20]", "[10,20][5,30]", "[0,0][0,0]", "invalid"])
def test_native_taps_reject_invalid_bounds(value):
    with pytest.raises(AssertionError):
        bounds({"bounds": value})
