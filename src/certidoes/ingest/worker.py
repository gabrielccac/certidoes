from __future__ import annotations

"""Ingest CLI: serve the API, process one record, or drift-check a mapping."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from certidoes.ingest.api import PROVIDERS, create_app
from certidoes.ingest.artifacts import LocalPublicDir
from certidoes.ingest.core import run_job
from certidoes.ingest.mapping import build_kwargs, load_mapping


def load_dotenv(path: str | Path) -> None:
    """Minimal KEY=VALUE loader (no interpolation). Missing file is fine."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _redacted(report) -> dict:
    return {
        "record": report.ref_label,
        "skipped": report.skipped,
        "status": report.status,
        "ok": report.ok,
        "failed": report.failed,
        "lines": list(report.lines),
    }


def _provider(name: str):
    try:
        factory = PROVIDERS[name]
    except KeyError:
        raise SystemExit(f"unknown provider {name!r}")
    return factory()


def cmd_check(mapping, provider_name: str, record_id: str | None) -> int:
    from certidoes.ingest.airtable_provider import AirtableProvider

    provider = _provider(provider_name)
    print(f"mapping: {len(mapping.sources)} source(s)")
    for source in mapping.sources:
        print(f"  - {source.label}: source={source.source}")
    if not isinstance(provider, AirtableProvider):
        print("column checks are currently implemented for the airtable provider")
        return 0
    table = mapping.table
    from certidoes.ingest.airtable import AirtableClient

    client = AirtableClient(
        os.environ.get("AIRTABLE_PAT", ""),
        os.environ.get("AIRTABLE_BASE_ID", "") or mapping.base_id,
    )
    try:
        schema = client.describe_table(table)
    except Exception as exc:
        print(f"schema check error: {type(exc).__name__}")
        return 1
    needed = (
        [(source.label, param, column) for source in mapping.sources
         for param, column in source.columns.items()]
        + [("outputs", "status", mapping.outputs.status),
           ("outputs", "summary", mapping.outputs.summary),
           ("outputs", "files", mapping.outputs.files)]
    )
    failed = False
    if schema is not None:
        for label, param, column in needed:
            if column in schema:
                name, kind = schema[column]
                print(f"  - {label}.{param}: column exists ({name} / {kind})")
            else:
                failed = True
                print(f"  - {label}.{param}: COLUMN NOT FOUND (renamed/deleted?)")
    else:
        print("  (schema scope missing on PAT; falling back to 100-record sample)")
        try:
            seen = client.present_columns(table)
        except Exception as exc:
            print(f"schema sample error: {type(exc).__name__}")
            return 1
        for label, param, column in needed:
            if column in seen:
                print(f"  - {label}.{param}: seen in sample")
            else:
                failed = True
                print(f"  - {label}.{param}: NOT SEEN in sample (check name/emptiness)")
    if record_id:
        try:
            fields = client.get_record(table, record_id)
        except Exception as exc:
            print(f"fetch error: {type(exc).__name__}")
            return 1
        for source in mapping.sources:
            _, missing = build_kwargs(source, fields)
            print(f"  - {source.label} record {record_id}: "
                  + ("all columns filled" if not missing else f"empty here: {', '.join(missing)}"))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Table-driven certificate ingest")
    parser.add_argument("--config", required=True, help="Mapping TOML file")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--provider", default=None,
                        help="Table provider (default: mapping's provider)")
    parser.add_argument("--public-base-url", default=os.environ.get("PUBLIC_BASE_URL", ""))
    parser.add_argument("--output-base", type=Path, default=Path("runs/ingest"))
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--record", help="Process one record synchronously and exit")
    parser.add_argument("--check", action="store_true", help="Validate mapping + columns, read-only")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    try:
        mapping = load_mapping(args.config)
    except (ValueError, OSError) as exc:
        print(f"mapping error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        return cmd_check(mapping, args.provider or mapping.provider, args.record)

    public_base_url = args.public_base_url or os.environ.get("PUBLIC_BASE_URL", "")
    if not public_base_url:
        print("PUBLIC_BASE_URL is required (table bases fetch attachments from it)", file=sys.stderr)
        return 2
    provider = _provider(args.provider or mapping.provider)
    store = LocalPublicDir(args.output_base / "_served", public_base_url)

    if args.record:
        ref = {"record_id": args.record, "table_id": mapping.table}
        try:
            report = run_job(provider, mapping, ref, store, args.output_base)
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__}))
            return 1
        print(json.dumps(_redacted(report), ensure_ascii=False))
        return 0

    if not os.environ.get("INGEST_SECRET"):
        print("INGEST_SECRET is required for --serve", file=sys.stderr)
        return 2
    import uvicorn

    app = create_app(args.config)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
