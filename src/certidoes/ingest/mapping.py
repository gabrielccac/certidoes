from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SourceMap:
    key: str
    label: str
    source: str
    columns: dict[str, str] = field(default_factory=dict)
    transforms: dict[str, str] = field(default_factory=dict)
    wraps: dict[str, str] = field(default_factory=dict)
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outputs:
    status: str
    summary: str
    files: str


@dataclass(frozen=True)
class StatusSet:
    claim_from: tuple[str, ...]
    processing: str
    done: str
    error: str


@dataclass(frozen=True)
class Mapping:
    base_id: str
    table: str
    provider: str
    sources: tuple[SourceMap, ...]
    outputs: Outputs
    status: StatusSet


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _first_word(value: str) -> str:
    parts = str(value or "").split()
    return parts[0] if parts else ""


TRANSFORMS = {
    "strip": lambda value: str(value or "").strip(),
    "digits": lambda value: _digits(str(value or "").strip()),
    "first_word": lambda value: _first_word(str(value or "").strip()),
}


def apply_transform(name: str, value: object) -> str:
    try:
        transform = TRANSFORMS[name]
    except KeyError:
        raise ValueError(f"unknown transform {name!r}") from None
    return transform(value)


def load_mapping(path: str | Path) -> Mapping:
    """Load and validate a mapping file. Raises ValueError on any schema drift
    in the *mapping itself* (missing keys, unknown transforms). Drift in the
    *base* (renamed/deleted columns) surfaces per-record at runtime instead,
    so one bad column never breaks the worker process."""
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    try:
        base_id = raw["base"]["id"]
        if isinstance(base_id, str) and base_id.startswith("env:"):
            resolved = os.environ.get(base_id[4:], "")
            if not resolved:
                raise ValueError(f"environment variable {base_id[4:]} is not set")
            base_id = resolved
        table = raw["table"]["id"]
        outputs = Outputs(**raw["outputs"])
        status_raw = raw["status"]
        status = StatusSet(
            claim_from=tuple(status_raw["claim_from"]),
            processing=status_raw["processing"],
            done=status_raw["done"],
            error=status_raw["error"],
        )
        sources = tuple(
            SourceMap(
                key=item["key"],
                label=item.get("label", item["key"]),
                source=item["source"],
                columns=dict(item.get("columns", {})),
                transforms=dict(item.get("transforms", {})),
                wraps=dict(item.get("wraps", {})),
                params=dict(item.get("params", {})),
            )
            for item in raw["sources"]
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid mapping file {path}: {exc}") from exc
    for source in sources:
        for param, name in source.transforms.items():
            if param not in source.columns:
                raise ValueError(f"transform on unmapped param {param!r} in {source.key}")
            if name not in TRANSFORMS:
                raise ValueError(f"unknown transform {name!r} in {source.key}")
        for param, kind in source.wraps.items():
            if kind != "tuple":
                raise ValueError(f"unknown wrap {kind!r} in {source.key}")
            if param not in source.columns and param not in source.params:
                raise ValueError(f"wrap on unknown param {param!r} in {source.key}")
    if not sources:
        raise ValueError(f"mapping {path} declares no sources")
    from certidoes.registry import get_source

    for source in sources:
        try:
            get_source(source.source)
        except ValueError as exc:
            raise ValueError(f"mapping {path}: {exc}") from exc
    provider = raw.get("provider", {}).get("name", "airtable")
    return Mapping(base_id=base_id, table=table, provider=provider,
                   sources=sources, outputs=outputs, status=status)


def build_kwargs(source: SourceMap, fields: dict) -> tuple[dict, list[str]]:
    """Build request kwargs from a row's fields (keyed by field ID).

    Returns (kwargs, missing_params). A param is missing when its column is
    absent or empty after its transform. Constants from ``params`` are always
    present.
    """
    kwargs = dict(source.params)
    missing: list[str] = []
    for param, column in source.columns.items():
        raw = fields.get(column)
        if raw is None or (isinstance(raw, str) and not raw.strip()) or (isinstance(raw, list) and not raw):
            missing.append(param)
            continue
        value = apply_transform(source.transforms.get(param, "strip"), raw)
        if not value:
            missing.append(param)
            continue
        kwargs[param] = value
    return kwargs, missing


@dataclass(frozen=True)
class CertOutcome:
    label: str
    ok: bool
    code: str = ""
    message: str = ""
    filename: str = ""
    pdf_path: str = ""


def summary_lines(outcomes: list[CertOutcome]) -> list[str]:
    """Human-readable run summary without PII (no filenames, no documents)."""
    lines = []
    for outcome in outcomes:
        if outcome.ok:
            lines.append(f"\u2022 [{outcome.label}] pronta")
        else:
            lines.append(f"\u2022 [{outcome.label}] FALHA ({outcome.code}): {outcome.message}")
    return lines


def derive_status(outcomes: list[CertOutcome], status: StatusSet) -> str:
    if not outcomes:
        return status.error
    if all(outcome.ok for outcome in outcomes):
        return status.done
    if all(not outcome.ok for outcome in outcomes):
        return status.error
    return status.done if status.done else status.error
