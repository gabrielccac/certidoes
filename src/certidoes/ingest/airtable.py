from __future__ import annotations

import time

import requests

BASE_URL = "https://api.airtable.com/v0"


class AirtableError(RuntimeError):
    """Transport or API failure talking to Airtable (never carries row PII)."""


class AirtableClient:
    """Thin Airtable REST client. Rows are keyed by field ID when requested."""

    def __init__(self, token: str, base_id: str, *, timeout_s: int = 30) -> None:
        if not token or not base_id:
            raise AirtableError("Airtable token and base ID are required")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})
        self._base_id = base_id
        self._timeout_s = timeout_s

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{BASE_URL}/{self._base_id}/{path}"
        try:
            response = self._session.request(method, url, json=payload, timeout=self._timeout_s)
        except requests.RequestException as exc:
            raise AirtableError("Airtable request failed") from exc
        if response.status_code == 429:
            time.sleep(30)
            try:
                response = self._session.request(method, url, json=payload, timeout=self._timeout_s)
            except requests.RequestException as exc:
                raise AirtableError("Airtable request failed after 429 backoff") from exc
        if response.status_code >= 400:
            raise AirtableError(f"Airtable request failed (HTTP {response.status_code})")
        try:
            body = response.json()
        except ValueError as exc:
            raise AirtableError("Airtable response was not valid JSON") from exc
        return body if isinstance(body, dict) else {}

    def get_record(self, table: str, record_id: str) -> dict:
        """Fetch one record with fields keyed by field ID (stable across renames)."""
        body = self._request("GET", f"{table}/{record_id}?returnFieldsByFieldId=true")
        fields = body.get("fields")
        if not isinstance(fields, dict):
            raise AirtableError("Airtable record has no fields object")
        return fields

    def present_columns(self, table: str, max_records: int = 100) -> set[str]:
        """Union of field IDs seen across a sample. Presence-only: used
        to tell a renamed/deleted column (absent everywhere) apart from an
        empty cell (absent on one record). Never returns values. Heuristic
        on sparse tables; prefer describe_table when the PAT allows it."""
        body = self._request(
            "GET", f"{table}?returnFieldsByFieldId=true&maxRecords={max(1, max_records)}"
        )
        records = body.get("records")
        if not isinstance(records, list):
            raise AirtableError("Airtable list response was malformed")
        seen: set[str] = set()
        for record in records:
            fields = record.get("fields") if isinstance(record, dict) else None
            if isinstance(fields, dict):
                seen.update(fields.keys())
        return seen

    def describe_table(self, table_id: str) -> dict[str, tuple[str, str]] | None:
        """Exact {field_id: (name, type)} via the schema API. Returns None
        when the PAT lacks the schema.bases:read scope (caller falls back
        to sampling). Never returns values."""
        url = f"{BASE_URL}/meta/bases/{self._base_id}/tables"
        try:
            response = self._session.get(url, timeout=self._timeout_s)
        except requests.RequestException as exc:
            raise AirtableError("Airtable schema request failed") from exc
        if response.status_code == 403:
            return None
        if response.status_code >= 400:
            raise AirtableError(f"Airtable schema request failed (HTTP {response.status_code})")
        try:
            body = response.json()
        except ValueError as exc:
            raise AirtableError("Airtable schema response was not valid JSON") from exc
        for table in body.get("tables", []):
            if table.get("id") == table_id:
                return {
                    field["id"]: (field.get("name", ""), field.get("type", ""))
                    for field in table.get("fields", [])
                    if isinstance(field, dict) and "id" in field
                }
        raise AirtableError("table not found in base schema")

    def update_record(self, table: str, record_id: str, fields: dict) -> dict:
        """PATCH a record. Returns the raw response (may carry partialSuccess)."""
        return self._request("PATCH", f"{table}/{record_id}", {"fields": fields})


def attachment_objects(urls: list[str]) -> list[dict]:
    """Build an attachment-cell value. Airtable fetches each URL itself."""
    return [{"url": url} for url in urls]
