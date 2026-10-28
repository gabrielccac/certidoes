"""Receita E2E through Android accessibility/touch and its native PDF download."""
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from certidoes.github_payload import decrypt
from certidoes.sources import receita
from experiments.receita_actions_probe.runner import validate_pdf
from experiments.receita_environment_probe.android import adb

SERIAL = "emulator-5554"
CHROME = "com.android.chrome"
SAFE_TEXT = {"CPF", "CPF *", "Data de nascimento", "Data de nascimento *",
             "Aceitar", "Emitir Certidão", "Emitir Nova Certidão", "Baixar", "Download", "Fazer download"}


def bounds(node):
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    assert match, "Invalid native bounds"
    left, top, right, bottom = map(int, match.groups())
    assert right > left and bottom > top, "Empty native bounds"
    return left, top, right, bottom


def inputs(xml):
    result = []
    for node in ET.fromstring(xml).iter("node"):
        if (node.get("class") == "android.widget.EditText"
                and node.get("package") == CHROME
                and not node.get("resource-id", "").startswith(CHROME + ":id/")):
            try:
                bounds(node)
            except AssertionError:
                continue
            result.append(node)
    return sorted(result, key=lambda node: bounds(node)[1])


def ui_metadata(xml):
    # Only known public labels may leave the worker; other UI text can contain personal data.
    return [{"class": node.get("class"),
             "resource_id": re.sub(r"\d", "*", node.get("resource-id", "")),
             "bounds": node.get("bounds"), "enabled": node.get("enabled"),
             "focused": node.get("focused"),
             "package": node.get("package"),
             "text_length": len(node.get("text", "")),
             "text": node.get("text") if node.get("text") in SAFE_TEXT else "<redacted>"}
            for node in ET.fromstring(xml).iter("node")]


def chrome_dialog_texts(xml):
    for node in ET.fromstring(xml).iter("node"):
        if node.get("resource-id") == CHROME + ":id/modal_dialog_view":
            return [child.get("text")[:300] for child in node.iter("node")
                    if child.get("text") and child.get("class") in
                    ("android.widget.TextView", "android.widget.Button")][:12]
    return []


def field_state(xml, document, birth_digits, field_ids):
    nodes = {node.get("resource-id"): node for node in ET.fromstring(xml).iter("node")
             if node.get("class") == "android.widget.EditText" and node.get("package") == CHROME}
    result = {}
    for name, expected in (("document", document), ("birth", birth_digits)):
        node = nodes.get(field_ids[name])
        result[name] = {"observed": node is not None,
                        "matches": receita._digits(node.get("text")) == expected if node is not None else None,
                        "focused": node.get("focused") == "true" if node is not None else None,
                        "text_length": len(node.get("text", "")) if node is not None else None}
    return result


def keyboard_visible(xml):
    for node in ET.fromstring(xml).iter("node"):
        if ".inputmethod." in node.get("package", "") or ".inputmethod." in node.get("resource-id", ""):
            try:
                bounds(node)
                return True
            except AssertionError:
                pass
    return False


def wait_ui(device, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        xml = device.dump_hierarchy()
        if predicate(xml):
            return xml
        if time.monotonic() >= deadline:
            raise TimeoutError("Native UI invariant not reached")
        time.sleep(0.2)


def close_keyboard(device):
    if keyboard_visible(device.dump_hierarchy()):
        device.press("back")
        wait_ui(device, lambda xml: not keyboard_visible(xml))


def pdf_files():
    return set(adb("shell", "find /sdcard/Download -maxdepth 1 -type f -iname '*.pdf' 2>/dev/null",
                   check=False).splitlines())


def main():
    os.umask(0o077)
    out = Path("runs/android-openatx")
    out.mkdir(parents=True, exist_ok=True)
    report = {"engine": "OpenATX/uiautomator2", "uiautomator2": version("uiautomator2"),
              "device": "android", "cdp_used": False, "sdk_policy": "official unmodified",
              "typing_backend": "adb shell input text",
              "status": "running", "stage": "input"}
    started = time.monotonic()
    device = None
    document = birth = ""
    try:
        payload = decrypt(os.environ["RECEITA_PAYLOAD"], os.environ["RECEITA_PAYLOAD_PRIVATE_KEY"])
        request = receita.ReceitaRequest(payload["documento"], payload["data_nascimento"])
        document, kind, _ = receita._normalize(request)
        assert kind == "CPF", "This diagnostic requires the reference CPF request"
        birth = request.data_nascimento
        year, month, day = birth.split("-")
        native_birth = f"{day}/{month}/{year}"
        print(f"::add-mask::{document}\n::add-mask::{birth}\n::add-mask::{native_birth}", flush=True)
        report["stage"] = "android_setup"
        report["android_release"] = adb("shell", "getprop", "ro.build.version.release")
        report["android_api"] = adb("shell", "getprop", "ro.build.version.sdk")
        assert adb("shell", "getprop", "ro.kernel.qemu") == "1", "Android emulator required"
        assert report["android_api"] == "36" and report["android_release"] == "16"
        package = adb("shell", "dumpsys", "package", CHROME)
        report["chrome_package_version"] = re.search(r"versionName=(\S+)", package).group(1)
        assert report["chrome_package_version"] == "133.0.6943.137", "Reference Chrome changed"
        adb("root", check=False)
        adb("wait-for-device")
        assert adb("shell", "id", "-u") == "0", "Root AVD required for reference Chrome flags"
        adb("shell", "settings", "put", "global", "auto_time_zone", "0")
        adb("shell", "setprop", "persist.sys.timezone", "America/Sao_Paulo")
        assert adb("shell", "pm", "clear", CHROME) == "Success"
        adb("shell", "am", "set-debug-app", "--persistent", CHROME)
        adb("shell", 'echo "chrome --disable-fre --no-default-browser-check --no-first-run" > /data/local/tmp/chrome-command-line')
        before = pdf_files()

        import uiautomator2 as u2
        device = u2.connect(SERIAL)
        device.implicitly_wait(5)
        device.screen_on()
        device.press("menu")
        report["native_display"] = list(device.window_size())
        adb("shell", "am", "start", "-W", "-n", CHROME + "/com.google.android.apps.chrome.Main",
            "-a", "android.intent.action.VIEW", "-d", receita.PAGE_URLS[kind])
        report["stage"] = "native_form"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            xml = device.dump_hierarchy()
            dialog = chrome_dialog_texts(xml)
            if dialog:
                # Chrome's own startup dialog is recorded before any personal data is entered.
                report.setdefault("chrome_startup_dialogs", []).append(dialog)
                dismissed = False
                for button_id, allowed in (
                    ("negative_button", {"not now", "no thanks", "agora não", "não, obrigado", "cancel", "cancelar"}),
                    ("positive_button", {"got it", "ok", "entendi"}),
                ):
                    button = device(resourceId=CHROME + ":id/" + button_id)
                    if button.exists and button.get_text().strip().casefold() in allowed:
                        button.click()
                        report["native_chrome_dialog_dismissed"] = True
                        dismissed = True
                        break
                assert dismissed, "Unrecognized Chrome startup dialog; inspect recorded labels"
                continue
            accept = device(textMatches=r"(?i)\s*aceitar\s*")
            if accept.exists:
                report["stage"] = "native_cookie_consent"
                accept.click()
                wait_ui(device, lambda value: not any(
                    n.get("text", "").strip().casefold() == "aceitar"
                    for n in ET.fromstring(value).iter("node")))
                report["native_cookie_consent_dismissed"] = True
                report["stage"] = "native_form"
                continue
            if len(inputs(xml)) == 2:
                break
            time.sleep(1)
        else:
            raise TimeoutError("Native form did not expose its two inputs")

        nodes = inputs(xml)
        field_ids = dict(zip(("document", "birth"), (node.get("resource-id") for node in nodes)))
        assert all(field_ids.values()) and len(set(field_ids.values())) == 2, "Unique native input identities required"

        def checkpoint(phase):
            xml = device.dump_hierarchy()
            entry = {"phase": phase, "fields": field_state(xml, document, day + month + year, field_ids),
                     "keyboard_visible": keyboard_visible(xml)}
            report.setdefault("checkpoints", []).append(entry)
            print(json.dumps({"native_checkpoint": entry}), flush=True)
            return entry

        for name, value in (("document", document), ("birth", day + month + year)):
            report["stage"] = "native_" + name + "_focus"
            close_keyboard(device)
            target = device(resourceId=field_ids[name], className="android.widget.EditText")
            assert target.wait(timeout=5), "Bound native input is not visible"
            assert not receita._digits(target.get_text()), "Fresh field must be empty before native typing"
            target.click()
            wait_ui(device, lambda xml: field_state(xml, document, day + month + year, field_ids)[name]["focused"] is True)
            checkpoint(name + "_focused_before_typing")
            report["stage"] = "native_" + name + "_input"
            adb("shell", "input", "text", value)
            wait_ui(device, lambda xml: field_state(xml, document, day + month + year, field_ids)[name]["matches"] is True)
            checkpoint(name + "_typed_and_verified")
            report["stage"] = "native_" + name + "_keyboard_close"
            close_keyboard(device)
            checkpoint(name + "_keyboard_closed")
        report["stage"] = "native_form_check"
        report["form_state"] = checkpoint("before_emission")["fields"]
        assert all(item["matches"] is True for item in report["form_state"].values()), "Native input values do not match request"
        button = device(textMatches=r"\s*Emitir Certidão\s*")
        assert button.wait(timeout=15), "Native emit button missing"
        report["button_enabled_before"] = button.info["enabled"]
        assert report["button_enabled_before"], "Native emit button disabled"
        report["stage"] = "native_emission"
        button.click()
        report["native_tap_sent"] = True
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            new_files = pdf_files() - before
            if new_files:
                assert len(new_files) == 1, "Ambiguous certificate download"
                destination = out / "certificate.pdf"
                subprocess.run(["adb", "-s", SERIAL, "pull", new_files.pop(), str(destination)],
                               check=True, capture_output=True, timeout=30)
                report.update(validate_pdf(destination, document), status="success", stage="complete")
                break
            xml = device.dump_hierarchy()
            text = " ".join(node.get("text", "") for node in ET.fromstring(xml).iter("node"))
            report["page_state"] = {
                "rejected": bool(re.search(r"requested URL was rejected", text, re.I)),
                "portal_106": bool(re.search(r"106\s*-\s*\d{2}/\d{2}/\d{4}", text)),
                "visual_instruction": bool(re.search(r"select all|selecione todas|selecione todos", text, re.I))}
            if any(report["page_state"].values()):
                raise RuntimeError("Portal requires intervention")
            if device(textMatches=r"\s*Emitir Nova Certidão\s*").click_exists(timeout=0.1):
                report["existing_certificate_dialog_confirmed"] = True
            for label in ("Baixar", "Download", "Fazer download"):
                device(text=label).click_exists(timeout=0.1)
            time.sleep(2)
        else:
            raise TimeoutError("No native PDF download after emit tap")
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__)
    finally:
        if device is not None and report["status"] != "success":
            try:
                (out / "native-ui.json").write_text(json.dumps(ui_metadata(device.dump_hierarchy()), indent=2) + "\n")
            except Exception as error:
                report["ui_dump_error_type"] = type(error).__name__
        adb("shell", "am", "force-stop", CHROME, check=False)
        adb("shell", "am", "clear-debug-app", check=False)
        report["seleniumbase_imported"] = any(n == "seleniumbase" or n.startswith("seleniumbase.") for n in sys.modules)
        report["playwright_imported"] = any(n == "playwright" or n.startswith("playwright.") for n in sys.modules)
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        serialized = json.dumps(report, indent=2)
        assert not document or document not in serialized, "Unredacted document"
        assert not birth or birth not in serialized, "Unredacted birth"
        (out / "result.json").write_text(serialized + "\n")
        print(serialized, flush=True)
    return int(report["status"] != "success")


if __name__ == "__main__":
    raise SystemExit(main())
