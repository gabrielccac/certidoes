from __future__ import annotations

import mimetypes
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from pathlib import Path

from certidoes.ingest.providers import ArtifactStore

ORPHAN_TTL_S = 3600


class LocalPublicDir(ArtifactStore):
    """Serve generated PDFs at unguessable URLs the table base can fetch.

    publish() copies the PDF into the served directory under a random token:
    the original stays wherever generate() wrote it (audit trail), and served
    copies remain available long enough for asynchronous attachment fetches.
    """

    def __init__(self, directory: Path, public_base_url: str) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._public_base_url = public_base_url.rstrip("/")
        self._tokens: dict[str, tuple[Path, str]] = {}
        self._lock = threading.Lock()

    def publish(self, filename: str, pdf_path: str) -> str:
        token = secrets.token_urlsafe(24)
        copy = self._directory / token
        shutil.copy2(pdf_path, copy)
        with self._lock:
            self._tokens[token] = (copy, filename)
        return f"{self._public_base_url}/files/{token}/certificate.pdf"

    def discard(self, url: str) -> None:
        token = self._token_of(url)
        with self._lock:
            entry = self._tokens.pop(token, None) if token else None
        if entry is not None:
            try:
                entry[0].unlink(missing_ok=True)
            except OSError:
                pass

    def sweep(self) -> None:
        now = time.time()
        try:
            entries = list(self._directory.iterdir())
        except OSError:
            return
        for path in entries:
            try:
                if path.is_file() and now - path.stat().st_mtime > ORPHAN_TTL_S:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def resolve(self, token: str) -> tuple[Path, str] | None:
        """Local path + filename for the API file route (None = 404)."""
        with self._lock:
            entry = self._tokens.get(token)
        if entry is not None:
            return entry
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
            return None
        path = self._directory / token
        return (path, "certificate.pdf") if path.is_file() else None

    @staticmethod
    def _token_of(url: str) -> str | None:
        try:
            parts = urllib.parse.urlparse(url).path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "files":
                return parts[1]
        except ValueError:
            pass
        return None

    @staticmethod
    def content_type(filename: str) -> str:
        return mimetypes.guess_type(filename)[0] or "application/pdf"
