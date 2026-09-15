from __future__ import annotations

from pathlib import Path

from certidoes.errors import CertificateError
from certidoes.ingest.mapping import CertOutcome, build_kwargs, derive_status, summary_lines
from certidoes.ingest.providers import (
    ArtifactStore,
    JobReport,
    PublishedArtifact,
    TableProvider,
)
from certidoes.registry import get_source


def _coerce_kwargs(request_cls, kwargs: dict, wraps: dict | None = None) -> dict:
    """Convert list params to tuples when the dataclass field expects a tuple,
    and wrap scalars into 1-tuples for params the mapping marks wraps.
    Keeps mappings generic: TOML only has arrays and scalars, requests
    may want tuples (e.g. Trf1Request.orgao_siglas).
    """
    import typing

    try:
        hints = typing.get_type_hints(request_cls)
    except Exception:
        return dict(kwargs)
    coerced = dict(kwargs)
    for name, value in kwargs.items():
        hint = hints.get(name)
        if isinstance(value, list) and (hint is tuple or typing.get_origin(hint) is tuple):
            coerced[name] = tuple(value)
    for name in wraps or {}:
        if (wraps or {})[name] == "tuple" and name in coerced and not isinstance(coerced[name], tuple):
            coerced[name] = (coerced[name],)
    return coerced


def run_job(
    provider: TableProvider,
    mapping,
    ref: dict,
    store: ArtifactStore,
    output_base: Path,
) -> JobReport:
    """Provider-neutral job: claim, generate every mapped certificate,
    publish status + summary + attachments. Returns a redacted report
    (labels and codes only, never documents or filenames... note filenames
    may contain document digits, so they stay inside artifacts only)."""
    store.sweep()
    status_field = mapping.outputs.status
    enriched = dict(ref)
    enriched.setdefault("status_field", status_field)
    enriched.setdefault("summary_field", mapping.outputs.summary)
    enriched.setdefault("files_field", mapping.outputs.files)

    inputs = provider.fetch(enriched)
    if inputs.status not in mapping.status.claim_from:
        return JobReport(ref_label=inputs.ref_label, skipped=True,
                         status=inputs.status, ok=0, failed=0)
    claimed = provider.claim(enriched, mapping.status.processing, mapping.status.claim_from)
    if claimed is None:
        return JobReport(ref_label=inputs.ref_label, skipped=True,
                         status=inputs.status, ok=0, failed=0)
    fields = claimed.fields

    record_dir = output_base / claimed.ref_label
    record_dir.mkdir(parents=True, exist_ok=True)

    outcomes: list[CertOutcome] = []
    for source in mapping.sources:
        try:
            spec = get_source(source.source)
        except ValueError as exc:
            outcomes.append(CertOutcome(source.label, False, "fonte_desconhecida", str(exc)))
            continue
        kwargs, missing = build_kwargs(source, fields)
        if missing:
            outcomes.append(
                CertOutcome(source.label, False, "coluna_ausente", f"missing: {', '.join(missing)}")
            )
            continue
        try:
            request = spec.request_cls(
                **_coerce_kwargs(spec.request_cls, kwargs, source.wraps), output_dir=record_dir
            )
        except TypeError as exc:
            outcomes.append(CertOutcome(source.label, False, "mapeamento_invalido", str(exc)))
            continue
        try:
            result = spec.generate(request)
        except CertificateError as exc:
            outcomes.append(CertOutcome(source.label, False, exc.code, exc.message))
        except Exception:
            outcomes.append(CertOutcome(source.label, False, "erro_interno", "unexpected failure"))
        else:
            outcomes.append(
                CertOutcome(source.label, True, filename=result.filename, pdf_path=str(result.pdf_path))
            )

    urls: list[str] = []
    published = []
    for outcome in outcomes:
        if not outcome.ok or not outcome.pdf_path:
            continue
        url = store.publish(outcome.filename, outcome.pdf_path)
        urls.append(url)
        published.append(outcome)

    lines = summary_lines(outcomes)
    status_value = derive_status(outcomes, mapping.status)
    provider.publish(
        enriched,
        status=status_value,
        summary="\n".join(lines),
        attachment_urls=urls if published else None,
    )
    ok_count = sum(1 for o in outcomes if o.ok)
    return JobReport(
        ref_label=claimed.ref_label, skipped=False, status=status_value,
        ok=ok_count, failed=len(outcomes) - ok_count, lines=tuple(lines),
        artifacts=tuple(
            PublishedArtifact(filename=o.filename, url=u) for o, u in zip(published, urls)
        ),
    )
