import json

import pytest

from experiments.receita_environment_probe.android_openatx import (
    bounds, chrome_dialog_texts, close_keyboard, field_state, inputs, keyboard_visible, ui_metadata,
)


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


def test_native_form_checks_full_date_instead_of_only_its_length():
    xml = '''<hierarchy>
      <node class="android.widget.EditText" package="com.android.chrome" resource-id="document-id" text="123.456.789-00" focused="false" bounds="[0,20][100,40]"/>
      <node class="android.widget.EditText" package="com.android.chrome" resource-id="birth-id" text="30/12/1990" focused="true" bounds="[0,50][100,70]"/>
    </hierarchy>'''
    ids = {"document": "document-id", "birth": "birth-id"}
    result = field_state(xml, "12345678900", "31121990", ids)
    assert result["document"]["matches"] is True
    assert result["birth"]["matches"] is False
    assert result["birth"]["focused"] is True
    assert field_state(xml, "12345678900", "30121990", ids)["birth"]["matches"] is True


def test_missing_field_is_unknown_and_does_not_invalidate_observed_field():
    xml = '''<hierarchy><node class="android.widget.EditText" package="com.android.chrome"
      resource-id="document-id" text="123.456.789-00" focused="true"/></hierarchy>'''
    result = field_state(xml, "12345678900", "31121990", {"document": "document-id", "birth": "birth-id"})
    assert result["document"]["matches"] is True
    assert result["document"]["focused"] is True
    assert result["birth"] == {"observed": False, "matches": None, "focused": None, "text_length": None}


def test_field_identity_survives_reordering_in_native_hierarchy():
    xml = '''<hierarchy>
      <node class="android.widget.EditText" package="com.android.chrome" resource-id="birth-id" text="31/12/1990"/>
      <node class="android.widget.EditText" package="com.android.chrome" resource-id="document-id" text="123.456.789-00"/>
    </hierarchy>'''
    result = field_state(xml, "12345678900", "31121990", {"document": "document-id", "birth": "birth-id"})
    assert all(value["matches"] is True for value in result.values())


def test_keyboard_close_uses_back_only_when_keyboard_is_visible():
    from unittest.mock import Mock
    shown = '<hierarchy><node package="com.google.android.inputmethod.latin" bounds="[0,100][100,200]"/></hierarchy>'
    hidden = '<hierarchy><node package="com.android.chrome" bounds="[0,0][100,200]"/></hierarchy>'
    assert keyboard_visible(shown) is True
    device = Mock()
    device.dump_hierarchy.side_effect = [shown, hidden]
    close_keyboard(device)
    device.press.assert_called_once_with("back")
    device.reset_mock()
    device.dump_hierarchy.side_effect = [hidden]
    close_keyboard(device)
    device.press.assert_not_called()


@pytest.mark.parametrize("value", ["[-1,0][100,20]", "[10,20][5,30]", "[0,0][0,0]", "invalid"])
def test_native_taps_reject_invalid_bounds(value):
    with pytest.raises(AssertionError):
        bounds({"bounds": value})
