"""Run one controlled Android case and retain only redacted proof and metrics."""
import argparse
from importlib import metadata, util
import json
import os
from pathlib import Path
import resource
import site
import sys
import time
import traceback

from certidoes.github_payload import decrypt
from certidoes.sources import receita
from experiments.receita_actions_probe.runner import validate_pdf
from experiments.receita_direct_cdp_probe import reference


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("seleniumbase", "direct"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"engine": args.engine, "device": "android", "single_sdk": False,
              "reference_commit": "af6af04828651a461120cfce379c39685edb1b27",
              "sdk_loads": [], "status": "running", "stage": "input"}
    document = birth = ""
    client = None
    started = time.monotonic()
    try:
        payload = decrypt(os.environ["RECEITA_PAYLOAD"], os.environ["RECEITA_PAYLOAD_PRIVATE_KEY"])
        document, birth = payload["documento"], payload["data_nascimento"]
        print(f"::add-mask::{document}\n::add-mask::{birth}", flush=True)
        report["seleniumbase_installed"] = util.find_spec("seleniumbase") is not None
        if args.engine == "direct":
            assert not report["seleniumbase_installed"]
            assert util.find_spec("playwright") is None and util.find_spec("selenium") is None
            from experiments.receita_direct_cdp_probe.client import DirectChrome

            def factory(**kwargs):
                nonlocal client
                client = DirectChrome(**kwargs)
                return client
        else:
            factory = None
            report["seleniumbase_version"] = metadata.version("seleniumbase")
        report["stage"] = "official_form_and_emission"
        destination = reference.run(receita.ReceitaRequest(document, birth,
            output_dir=args.output, profile_dir=args.output / "profile", captcha_retries=1,
            token_timeout_ms=60000), android_port=9229, single_sdk=False, report=report,
            chrome_factory=factory)
        report.update(validate_pdf(destination, document), status="success", stage="complete")
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__,
                      code=getattr(error, "code", None), failed_stage=getattr(error, "stage", None))
        report["cdp_error_method"] = getattr(error, "method", None)
        report["failure_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno,
            "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
    finally:
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        report["python_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["site_packages_bytes"] = sum(p.stat().st_size for folder in site.getsitepackages()
            for p in Path(folder).rglob("*") if p.is_file())
        report["seleniumbase_imported"] = any(n.split(".")[0] == "seleniumbase" for n in sys.modules)
        report["playwright_imported"] = any(n.split(".")[0] == "playwright" for n in sys.modules)
        if client is not None:
            report["cdp_commands"] = dict(client.commands)
            report["ignored_unsubscribed_events"] = dict(client.ignored_events)
        if args.engine == "direct" and report["seleniumbase_imported"]:
            report.update(status="error", code="unexpected_seleniumbase_import")
        text = json.dumps(report, indent=2)
        assert not document or document not in text
        assert not birth or birth not in text
        (args.output / "result.json").write_text(text + "\n")
        print(text, flush=True)
    return int(report["status"] != "success")


if __name__ == "__main__":
    raise SystemExit(main())
