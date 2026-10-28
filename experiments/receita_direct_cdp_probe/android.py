"""Run control and direct CDP in separate Python environments on one fresh AVD."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

from experiments.receita_environment_probe.android import adb


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--direct-only", action="store_true")
    args = parser.parse_args()
    out = Path("runs/direct-cdp-comparison")
    out.mkdir(parents=True, exist_ok=False)
    facts = {"android_api": adb("shell", "getprop", "ro.build.version.sdk"),
             "android_release": adb("shell", "getprop", "ro.build.version.release"),
             "emulator": adb("shell", "getprop", "ro.kernel.qemu") == "1"}
    facts["chrome"] = re.search(r"versionName=(\S+)", adb("shell", "dumpsys", "package", "com.android.chrome")).group(1)
    assert facts["emulator"] and facts["android_api"] == "36" and facts["chrome"] == "133.0.6943.137"
    (out / "android.json").write_text(json.dumps(facts, indent=2) + "\n")
    cases = {}
    try:
        adb("root")
        adb("wait-for-device")
        adb("shell", "settings", "put", "global", "auto_time_zone", "0")
        adb("shell", "setprop", "persist.sys.timezone", "America/Sao_Paulo")
        engines = [("preflight", sys.executable)]
        if not args.direct_only:
            engines.append(("seleniumbase", os.environ["CONTROL_PYTHON"]))
        engines.append(("direct", sys.executable))
        for engine, python in engines:
            assert adb("shell", "pm", "clear", "com.android.chrome") == "Success"
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
            print(json.dumps({"starting_engine": engine, "fresh_chrome_profile": True}), flush=True)
            if engine == "preflight":
                process = subprocess.run([python, "-m", "experiments.receita_direct_cdp_probe.smoke",
                    "--page-host", "10.0.2.2", "--require-android", "--output", str(out / "preflight.json")],
                    check=False, timeout=90)
                if process.returncode != 0:
                    return 1
                adb("shell", "am", "force-stop", "com.android.chrome")
                continue
            subprocess.run([python, "-m", "experiments.receita_direct_cdp_probe.case",
                "--engine", engine, "--output", str(out / engine)], check=False, timeout=300)
            report_path = out / engine / "result.json"
            cases[engine] = json.loads(report_path.read_text()) if report_path.exists() else {"status": "no_report"}
            adb("shell", "am", "force-stop", "com.android.chrome")
    finally:
        adb("forward", "--remove", "tcp:9229", check=False)
        adb("shell", "am", "force-stop", "com.android.chrome", check=False)
        adb("shell", "am", "clear-debug-app", check=False)
    summary = {name: {key: report.get(key) for key in ("status", "bytes", "pages", "document_matches",
        "elapsed_s", "site_packages_bytes", "python_peak_rss_kib", "seleniumbase_installed", "seleniumbase_imported")}
        for name, report in cases.items()}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    return int(not all(report["status"] == "success" for report in cases.values()))


if __name__ == "__main__":
    raise SystemExit(main())
