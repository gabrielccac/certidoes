"""Serialized, disposable Chrome sessions on the dedicated Redroid service."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import urlopen

from certidoes.errors import CertificateError
from certidoes.models import CertificateResult

CHROME_VERSION = "133.0.6943.137"


def _adb(serial: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(["adb", "-s", serial, *args], capture_output=True,
                            text=True, timeout=30)
    if check and result.returncode:
        raise CertificateError("receita", "browser", "android_adb_falhou", "Android ADB command failed")
    return result.stdout.strip()


def prepare(serial: str, port: int) -> None:
    subprocess.run(["adb", "connect", serial], capture_output=True, check=True, timeout=20)
    _adb(serial, "wait-for-device")
    if (_adb(serial, "shell", "getprop", "ro.hardware") != "redroid"
            or _adb(serial, "shell", "getprop", "ro.build.version.sdk") != "35"
            or _adb(serial, "shell", "getprop", "sys.boot_completed") != "1"):
        raise CertificateError("receita", "browser", "android_indisponivel", "A booted Redroid Android 15 device is required")
    if not _adb(serial, "shell", "pm", "path", "com.android.chrome", check=False).startswith("package:"):
        for name in ("TrichromeLibrary", "Chrome"):
            if _adb(serial, "shell", "pm", "install", "-r", f"/opt/receita-apks/{name}.apk") != "Success":
                raise CertificateError("receita", "browser", "android_chrome_ausente", "Could not install the pinned Chrome APKs")
    version = re.search(r"versionName=(\S+)", _adb(serial, "shell", "dumpsys", "package", "com.android.chrome"))
    if not version or version.group(1) != CHROME_VERSION:
        raise CertificateError("receita", "browser", "android_chrome_incompativel", "The validated Chrome version is required")
    if _adb(serial, "shell", "pm", "clear", "com.android.chrome") != "Success":
        raise CertificateError("receita", "browser", "android_perfil_falhou", "Could not clear the previous Chrome session")
    _adb(serial, "shell", "am", "set-debug-app", "--persistent", "com.android.chrome")
    _adb(serial, "shell", 'echo "chrome --disable-fre --no-default-browser-check --no-first-run" > /data/local/tmp/chrome-command-line')
    _adb(serial, "shell", "settings", "put", "global", "auto_time_zone", "0")
    _adb(serial, "shell", "setprop", "persist.sys.timezone", "America/Sao_Paulo")
    _adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    _adb(serial, "shell", "am", "start", "-W", "-n", "com.android.chrome/com.google.android.apps.chrome.Main",
         "-a", "android.intent.action.VIEW", "-d", "about:blank")
    _adb(serial, "forward", f"tcp:{port}", "localabstract:chrome_devtools_remote")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                browser = json.load(response)
            if "Android" in browser.get("User-Agent", "") and browser.get("Browser") == "Chrome/" + CHROME_VERSION:
                return
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise CertificateError("receita", "browser", "android_cdp_indisponivel", "Native Android Chrome CDP did not become ready")


@contextmanager
def device(request, report):
    import fcntl

    serial = os.environ.get("RECEITA_ANDROID_SERIAL", "receita-android:5555")
    try:
        port = int(os.environ.get("RECEITA_ANDROID_CDP_PORT", "9229"))
    except ValueError as error:
        raise CertificateError("receita", "input", "configuracao_invalida", "Invalid Android CDP port") from error
    if not 1 <= port <= 65535:
        raise CertificateError("receita", "input", "configuracao_invalida", "Invalid Android CDP port")
    lock_path = Path(os.environ.get("RECEITA_ANDROID_LOCK", "runs/.receita-android.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        lock_path.chmod(0o600)
        deadline = time.monotonic() + request.request_timeout_s
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CertificateError("receita", "browser", "android_ocupado", "Android is serving another request", retryable=True)
                time.sleep(.1)
        try:
            prepare(serial, port)
            yield port
        finally:
            report["cleanup_ok"] = True
            for args in (("forward", "--remove", f"tcp:{port}"),
                         ("shell", "am", "force-stop", "com.android.chrome"),
                         ("shell", "pm", "clear", "com.android.chrome"),
                         ("shell", "am", "clear-debug-app")):
                try:
                    _adb(serial, *args)
                except Exception:
                    report["cleanup_ok"] = False
            fcntl.flock(lock, fcntl.LOCK_UN)


def validate_pdf(path: Path, document: str) -> dict:
    from pypdf import PdfReader

    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or len(data) <= 1000:
        raise CertificateError("receita", "pdf", "pdf_invalido", "Invalid certificate PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as error:
        raise CertificateError("receita", "pdf", "pdf_invalido", "Could not parse certificate PDF") from error
    if document not in re.sub(r"\D", "", text):
        raise CertificateError("receita", "pdf", "documento_divergente", "Certificate document differs from the request")
    return {"bytes": len(data), "pages": len(reader.pages), "sha256": hashlib.sha256(data).hexdigest(),
            "document_matches": True}


def generate(request) -> CertificateResult:
    from certidoes.sources import receita
    from certidoes.receita_android_flow import run

    document, kind, _ = receita._normalize(request)
    request.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"executor": "redroid", "sdk_loads": [], "captcha_solver_used": False}
    started = time.monotonic()
    try:
        with device(request, report) as port:
            path = run(request, android_port=port, report=report)
            report.update(validate_pdf(path, document))
            filename = f"Certidão Receita Federal - {document}.pdf"
            destination = request.output_dir / filename
            path.replace(destination)
        report["status"] = "success"
        return CertificateResult("receita", kind, filename, destination,
                                 {"status": "Emitida", **report.get("verification", {}), **{k: report[k] for k in
                                  ("executor", "bytes", "pages", "sha256", "captcha_solver_used", "cleanup_ok")}})
    except CertificateError as error:
        report.update(status="error", code=error.code, stage=error.stage)
        raise
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__)
        raise CertificateError("receita", "browser", "android_falhou", "Android certificate issuance failed") from error
    finally:
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        serialized = json.dumps(report, indent=2)
        if document in serialized or (request.data_nascimento and request.data_nascimento in serialized):
            raise RuntimeError("Sensitive input in Android diagnostic report")
        output = request.output_dir / "receita-android.json"
        output.write_text(serialized + "\n")
        output.chmod(0o600)
