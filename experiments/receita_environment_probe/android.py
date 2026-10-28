"""Run the Pure CDP probe against Chrome inside a disposable Android AVD."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

from experiments.receita_environment_probe import pure_cdp


def adb(*arguments, check=True):
    result = subprocess.run(["adb", "-s", "emulator-5554", *arguments],
        check=check, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


def main():
    root = Path("runs/pure-cdp-android")
    root.mkdir(parents=True, exist_ok=True)
    facts = {"android_release": adb("shell", "getprop", "ro.build.version.release"),
             "android_api": adb("shell", "getprop", "ro.build.version.sdk"),
             "emulator": adb("shell", "getprop", "ro.kernel.qemu") == "1",
             "cpu_abi": adb("shell", "getprop", "ro.product.cpu.abi"),
             "ua_override": False, "touch_override": False, "metrics_override": False}
    facts["runtime"] = "docker" if os.environ.get("ANDROID_CONTAINER") == "true" else "native"
    if facts["runtime"] == "docker":
        running = subprocess.run(["docker", "inspect", "receita-android-avd", "--format", "{{.State.Running}}"],
            check=True, capture_output=True, text=True).stdout.strip()
        assert running == "true", "Android container is not running"
        facts["emulator_container_running"] = True
    assert facts["emulator"] and facts["android_api"].isdigit(), "Android AVD required"
    assert "package:" in adb("shell", "pm", "path", "com.android.chrome"), "Chrome package missing"
    info = adb("shell", "dumpsys", "package", "com.android.chrome")
    match = re.search(r"versionName=(\S+)", info)
    facts["chrome_package_version"] = match.group(1) if match else None
    (root / "android.json").write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts), flush=True)
    failed = False
    try:
        adb("root")
        adb("wait-for-device")
        adb("shell", "settings", "put", "global", "auto_time_zone", "0")
        adb("shell", "setprop", "persist.sys.timezone", "America/Sao_Paulo")
        for single in (False, True):
            case = "single-sdk" if single else "original-sdk"
            assert adb("shell", "pm", "clear", "com.android.chrome") == "Success", "Chrome reset failed"
            adb("shell", "am", "set-debug-app", "--persistent", "com.android.chrome")
            adb("shell", 'echo "chrome --disable-fre --no-default-browser-check --no-first-run" > /data/local/tmp/chrome-command-line')
            adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
            adb("shell", "am", "start", "-W", "-n", "com.android.chrome/com.google.android.apps.chrome.Main",
                "-a", "android.intent.action.VIEW", "-d", "about:blank")
            adb("forward", "tcp:9229", "localabstract:chrome_devtools_remote")
            for attempt in range(60):
                try:
                    with urlopen("http://127.0.0.1:9229/json/version", timeout=2) as response:
                        assert "Android" in json.load(response).get("User-Agent", "")
                    break
                except Exception:
                    if attempt == 59:
                        raise
                    time.sleep(0.5)
            sys.argv = ["pure-cdp", "--device", "android", "--android-port", "9229",
                        "--output", str(root / case)] + (["--single-sdk"] if single else [])
            failed |= pure_cdp.main() != 0
            adb("shell", "am", "force-stop", "com.android.chrome")
    finally:
        adb("forward", "--remove", "tcp:9229", check=False)
        adb("shell", "am", "force-stop", "com.android.chrome", check=False)
        adb("shell", "am", "clear-debug-app", check=False)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
