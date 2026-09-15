from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class JobInputs:
    """Raw provider row: fields keyed by mapping column IDs plus current status."""

    ref_label: str
    fields: dict = field(default_factory=dict)
    status: str | None = None


@dataclass(frozen=True)
class PublishedArtifact:
    filename: str
    url: str


@dataclass(frozen=True)
class CertResult:
    label: str
    ok: bool
    code: str = ""
    message: str = ""
    filename: str = ""
    pdf_path: str = ""


@dataclass(frozen=True)
class JobReport:
    ref_label: str
    skipped: bool
    status: str | None
    ok: int
    failed: int
    lines: tuple[str, ...] = ()
    artifacts: tuple[PublishedArtifact, ...] = ()


class TableProvider(ABC):
    """Table side of the ingest contract. Airtable today; any other base
    tomorrow by implementing these methods. Never sees certificates."""

    name: str

    def check_ref(self, ref: dict) -> str | None:
        """Synchronous ref validation for HTTP 400s. None means acceptable;
        the job itself may still fail later (logged, redacted)."""
        return None

    @abstractmethod
    def fetch(self, ref: dict) -> JobInputs:
        """Load raw fields + current status for one job reference."""

    @abstractmethod
    def claim(self, ref: dict, processing_value: str, claim_from: tuple[str, ...]) -> JobInputs | None:
        """Atomically-ish mark a job processing. Returns inputs, or None
        when the row is not in a claimable state (already done/processing)."""

    @abstractmethod
    def publish(
        self,
        ref: dict,
        *,
        status: str,
        summary: str,
        attachment_urls: list[str] | None,
    ) -> None:
        """Write status + summary lines, and replace attachments when the
        list is not None. Must never receive PII beyond what the mapping
        already stores in the base."""

    @abstractmethod
    def column_report(self, table: str, columns: list[str]) -> dict[str, str]:
        """Per-column existence info for --check (presence only, no values)."""


class ArtifactStore(ABC):
    """File side of the ingest contract. Local public dir today, S3/R2
    tomorrow by implementing publish/discard."""

    @abstractmethod
    def publish(self, filename: str, pdf_path: str) -> str:
        """Make a PDF fetchable; return its public URL."""

    @abstractmethod
    def discard(self, url: str) -> None:
        """Best-effort removal after a successful write-back."""

    @abstractmethod
    def sweep(self) -> None:
        """Best-effort cleanup of leftovers."""
