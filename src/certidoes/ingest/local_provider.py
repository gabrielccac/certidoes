from __future__ import annotations

import json
from pathlib import Path

from certidoes.ingest.providers import JobInputs, TableProvider


class LocalProvider(TableProvider):
    """Unplugged provider for local validation (and future scripting use).

    Job refs carry everything inline, no table involved::

        {"label": "docker-validation",
         "status": "PENDENTE",
         "fields": {"documento": "...", "data_nascimento": "..."}}

    publish() writes a redacted ``report.json`` next to the generated PDFs
    instead of touching any base. Claim state is in-memory: reposting the
    same label skips, exactly like a repost to a table provider.
    """

    name = "local"

    def __init__(self, out_dir: Path | None = None) -> None:
        self._out_dir = Path(out_dir) if out_dir else None
        self._claimed: set[str] = set()
        self.reports: dict[str, dict] = {}

    def _label(self, ref: dict) -> str:
        return str(ref.get("label") or "local")

    def fetch(self, ref: dict) -> JobInputs:
        fields = ref.get("fields") or {}
        if not isinstance(fields, dict):
            raise ValueError("local ref needs a fields object")
        return JobInputs(ref_label=self._label(ref), fields=dict(fields),
                         status=ref.get("status"))

    def claim(self, ref: dict, processing_value: str, claim_from: tuple[str, ...]) -> JobInputs | None:
        inputs = self.fetch(ref)
        if inputs.status not in claim_from or inputs.ref_label in self._claimed:
            return None
        self._claimed.add(inputs.ref_label)
        return inputs

    def publish(
        self,
        ref: dict,
        *,
        status: str,
        summary: str,
        attachment_urls: list[str] | None,
    ) -> dict:
        report = {
            "label": self._label(ref),
            "status": status,
            "summary": summary,
            "attachments": attachment_urls or [],
        }
        self.reports[self._label(ref)] = report
        if self._out_dir is not None:
            self._out_dir.mkdir(parents=True, exist_ok=True)
            (self._out_dir / f"{self._label(ref)}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return report

    def column_report(self, table: str, columns: list[str]) -> dict[str, str]:
        return {column: "inline (provided at request time)" for column in columns}
