from __future__ import annotations

import concurrent.futures
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from certidoes.ingest.airtable_provider import AirtableProvider
from certidoes.ingest.artifacts import LocalPublicDir
from certidoes.ingest.core import run_job
from certidoes.ingest.local_provider import LocalProvider
from certidoes.ingest.mapping import load_mapping

PROVIDERS = {"airtable": AirtableProvider, "local": LocalProvider}


class JobRequest(BaseModel):
    provider: str = "airtable"
    ref: dict[str, Any] = Field(default_factory=dict)


def _redacted(report) -> dict:
    return {
        "record": report.ref_label,
        "skipped": report.skipped,
        "status": report.status,
        "ok": report.ok,
        "failed": report.failed,
        "lines": list(report.lines),
    }


def create_app(config_path: str | Path) -> FastAPI:
    mapping = load_mapping(config_path)
    output_base = Path(os.environ.get("INGEST_OUTPUT_BASE", "runs/ingest"))
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "")
    if not public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL is required (table bases fetch attachments from it)")
    store = LocalPublicDir(output_base / "_served", public_base_url)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="certidoes-ingest", lifespan=lifespan)
    app.state.mapping = mapping
    app.state.store = store
    app.state.output_base = output_base

    def _provider(name: str):
        try:
            factory = PROVIDERS[name]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"unknown provider {name!r}")
        return factory()

    def _authorized(secret: str) -> bool:
        expected = os.environ.get("INGEST_SECRET", "")
        if not expected:
            return False
        return hmac.compare_digest(secret or "", expected)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/files/{token}/{filename:path}")
    def served_file(token: str, filename: str):
        entry = store.resolve(token)
        if entry is None:
            raise HTTPException(status_code=404)
        path, stored_filename = entry
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type=LocalPublicDir.content_type(stored_filename),
                            filename=stored_filename)

    @app.post("/jobs", status_code=202)
    def submit_job(
        request: JobRequest,
        x_ingest_secret: str = Header(default="", alias="X-Ingest-Secret"),
    ):
        if not _authorized(x_ingest_secret):
            raise HTTPException(status_code=401, detail="unauthorized")
        provider = _provider(request.provider)
        ref = dict(request.ref)
        ref_error = provider.check_ref(ref)
        if ref_error:
            raise HTTPException(status_code=400, detail=ref_error)
        ref.setdefault("table_id", mapping.table)

        def job() -> None:
            try:
                report = run_job(provider, mapping, ref, store, output_base)
                print(json.dumps(_redacted(report)), flush=True)
            except Exception as exc:
                print(json.dumps({"ref": ref.get("record_id") or ref.get("recordId"),
                                  "error": type(exc).__name__}), flush=True)

        executor.submit(job)
        return {"accepted": True}

    @app.exception_handler(404)
    def not_found(_request, _exc):
        return JSONResponse(status_code=404, content={"error": "not found"})

    return app
