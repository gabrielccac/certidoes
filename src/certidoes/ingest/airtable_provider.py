from __future__ import annotations

import os

from certidoes.ingest.airtable import AirtableClient, attachment_objects
from certidoes.ingest.providers import JobInputs, TableProvider


class AirtableProvider(TableProvider):
    """Airtable implementation of the table contract.

    Job refs look like {"record_id": "recXXX", "base_id": ..., "table_id": ...}
    with base/table falling back to the mapping and environment.
    """

    name = "airtable"

    def __init__(self, token: str = "", default_base_id: str = "") -> None:
        self._token = token or os.environ.get("AIRTABLE_PAT", "")
        self._default_base_id = default_base_id or os.environ.get("AIRTABLE_BASE_ID", "")

    def _client(self, ref: dict) -> tuple[AirtableClient, str, str]:
        base_id = ref.get("base_id") or self._default_base_id
        table_id = ref.get("table_id") or ref.get("table") or ""
        if not table_id:
            raise ValueError("job ref needs table_id")
        return AirtableClient(self._token, base_id), base_id, table_id

    def check_ref(self, ref: dict) -> str | None:
        if not ref.get("record_id") and not ref.get("recordId"):
            return "ref.record_id required"
        return None

    def _record_id(self, ref: dict) -> str:
        record_id = ref.get("record_id") or ref.get("recordId")
        if not record_id:
            raise ValueError("job ref needs record_id")
        return str(record_id)

    def fetch(self, ref: dict) -> JobInputs:
        client, _, table_id = self._client(ref)
        record_id = self._record_id(ref)
        fields = client.get_record(table_id, record_id)
        status_field = ref.get("status_field", "")
        status = fields.get(status_field) if status_field else None
        return JobInputs(ref_label=record_id, fields=fields,
                         status=status if isinstance(status, str) else None)

    def claim(self, ref: dict, processing_value: str, claim_from: tuple[str, ...]) -> JobInputs | None:
        inputs = self.fetch(ref)
        if inputs.status not in claim_from:
            return None
        client, _, table_id = self._client(ref)
        status_field = ref.get("status_field", "")
        client.update_record(table_id, inputs.ref_label, {status_field: processing_value})
        return inputs

    def publish(
        self,
        ref: dict,
        *,
        status: str,
        summary: str,
        attachment_urls: list[str] | None,
    ) -> dict:
        client, _, table_id = self._client(ref)
        status_field = ref.get("status_field", "")
        summary_field = ref.get("summary_field", "")
        files_field = ref.get("files_field", "")
        patch: dict = {}
        if status_field:
            patch[status_field] = status
        if summary_field:
            patch[summary_field] = summary
        if attachment_urls is not None and files_field:
            patch[files_field] = attachment_objects(attachment_urls)
        return client.update_record(table_id, self._record_id(ref), patch)

    def column_report(self, table: str, columns: list[str]) -> dict[str, str]:
        client = AirtableClient(self._token, self._default_base_id)
        try:
            schema = client.describe_table(table)
        except Exception as exc:
            return {column: f"schema error: {type(exc).__name__}" for column in columns}
        if schema is None:
            try:
                seen = client.present_columns(table)
            except Exception as exc:
                return {column: f"sample error: {type(exc).__name__}" for column in columns}
            return {
                column: ("seen in sample" if column in seen else "NOT SEEN in sample")
                for column in columns
            }
        return {
            column: (f"exists ({name} / {kind})" if column in schema else "NOT FOUND (renamed/deleted?)")
            for column in columns
            for name, kind in [schema.get(column, ("", ""))]
        }
