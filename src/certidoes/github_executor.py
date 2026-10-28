"""Run a certificate workflow on GitHub Actions and fetch the PDF it produces.

Generic counterpart of the Receita executor: dispatch the workflow with an
encrypted payload, find the run by its request id, wait for it, download the
single PDF artifact and delete it. The caller keeps its own source label so
errors stay attributable.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener, urlopen

from certidoes.errors import CertificateError


def _api_json(source: str, method: str, url: str, token: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = UrlRequest(url, data=body, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read()) if response.status != 204 else {}
    except (HTTPError, URLError, TimeoutError) as exc:
        status = exc.code if isinstance(exc, HTTPError) else None
        raise CertificateError(source, "github", "github_request_failed", "GitHub API request failed",
                               retryable=True, upstream_status=status) from exc


class _DropAuthRedirect(HTTPRedirectHandler):
    """Artifact downloads redirect to a signed storage URL that must not receive the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return UrlRequest(newurl, headers={"Accept": "application/zip"})


def run_and_fetch_pdf(
    *,
    source: str,
    repo: str,
    token: str,
    workflow: str,
    ref: str,
    inputs: dict,
    request_id: str,
    timeout_s: int = 900,
    poll_s: float = 5.0,
) -> tuple[bytes, int]:
    """Dispatch ``workflow`` and return ``(pdf_bytes, run_id)``.

    The workflow must set ``run-name`` to something containing ``request_id`` and
    upload one artifact whose name contains it, holding exactly one PDF.
    """
    api = f"https://api.github.com/repos/{repo}"
    _api_json(source, "POST", f"{api}/actions/workflows/{workflow}/dispatches", token,
              {"ref": ref, "inputs": {**inputs, "request_id": request_id}})

    deadline = time.monotonic() + timeout_s
    run = None
    while time.monotonic() < deadline:
        runs = _api_json(source, "GET", f"{api}/actions/runs?event=workflow_dispatch&branch={ref}&per_page=20", token)
        for candidate in runs.get("workflow_runs", []):
            if request_id in str(candidate.get("display_title", "")):
                run = candidate
                break
        if run and run.get("status") == "completed":
            break
        time.sleep(poll_s)
    if not run:
        raise CertificateError(source, "github", "github_run_not_found", "GitHub workflow run was not found", retryable=True)
    if run.get("conclusion") != "success":
        raise CertificateError(source, "github", "github_run_failed", "GitHub workflow failed", retryable=True)

    artifacts = _api_json(source, "GET", f"{api}/actions/runs/{run['id']}/artifacts", token).get("artifacts", [])
    artifact = next((item for item in artifacts if request_id in item.get("name", "") and not item.get("expired")), None)
    if not artifact:
        raise CertificateError(source, "github", "github_artifact_missing", "GitHub PDF artifact was not found", retryable=True)
    download = UrlRequest(artifact["archive_download_url"], headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with build_opener(_DropAuthRedirect()).open(download, timeout=60) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
    except (HTTPError, URLError, TimeoutError, zipfile.BadZipFile) as exc:
        raise CertificateError(source, "github", "github_artifact_download_failed",
                               "GitHub artifact download failed", retryable=True) from exc
    pdf_names = [name for name in archive.namelist() if name.lower().endswith(".pdf") and ".." not in Path(name).parts]
    if len(pdf_names) != 1:
        raise CertificateError(source, "artifact", "pdf_ausente", "GitHub artifact did not contain exactly one PDF")
    pdf = archive.read(pdf_names[0])
    if not pdf.startswith(b"%PDF-"):
        raise CertificateError(source, "artifact", "pdf_invalido", "GitHub artifact did not contain a valid PDF")
    try:  # the artifact holds personal data: remove it as soon as it is downloaded
        _api_json(source, "DELETE", artifact["url"], token)
    except CertificateError:
        pass
    return pdf, run["id"]
