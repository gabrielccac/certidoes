from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CertificateResult:
    source: str
    document_type: str
    filename: str
    pdf_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
