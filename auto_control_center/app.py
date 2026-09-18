from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from . import data
from .security import issue_csrf, validate_loopback_request, verify_csrf
from .wake_all import WakeAllError, WakeAllService, capability_enabled

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"



class WakeAllSubmit(BaseModel):
    confirm: bool
    idempotency_key: str
    preview_hash: str


def _wake_service() -> WakeAllService:
    return WakeAllService(
        wake_db=data.BROWSER_WAKE_DB,
        routes_file=data.BROWSER_ROUTES,
        worker_provider=data.workers,
    )


def _require_loopback(request: Request, *, require_origin: bool = False) -> None:
    client_host = request.client.host if request.client else None
    origin = request.headers.get("origin") if require_origin else None
    if require_origin and not origin:
        raise HTTPException(status_code=403, detail="origin_required")
    if not validate_loopback_request(
        client_host=client_host,
        request_url=str(request.url),
        origin=origin,
    ):
        raise HTTPException(status_code=403, detail="loopback_origin_required")

app = FastAPI(
    title="AUTO Control Center",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_FILE)


@app.get("/api/health")
def health() -> dict:
    return {
        "version": __version__,
        "mode": "read-only",
        "sources": data.source_health(),
        "services": data.service_states(),
        "wake_path": data.wake_path_health(),
        "master_request_health": data.master_request_rejections(),
        "media_archive": data.media_archive_overview(),
    }


@app.get("/api/dashboard")
def dashboard() -> dict:
    payload = data.dashboard()
    payload["version"] = __version__
    payload["mode"] = "read-only"
    return payload


@app.get("/api/workers")
def workers() -> list[dict]:
    return data.workers()


@app.get("/api/events")
def events(limit: int = 80) -> list[dict]:
    return data.orchestrator_events(limit)


@app.get("/api/wakes")
def wakes(limit: int = 100) -> dict:
    return {"deliveries": data.wake_deliveries(limit), "queues": data.wake_queues()}


@app.get("/api/routes")
def routes() -> list[dict]:
    return data.route_summary()


@app.get("/api/master")
def master() -> dict | None:
    return data.latest_master()


@app.get("/api/evidence")
def evidence() -> list[dict]:
    return data.evidence_overview()


@app.get("/api/actions/wake-all/preview")
def wake_all_preview(request: Request) -> JSONResponse:
    _require_loopback(request)
    preview = _wake_service().preview()
    response = JSONResponse(preview)
    if preview.get("enabled") and preview.get("available") and preview.get("idempotency_key"):
        token = issue_csrf(str(preview["idempotency_key"]))
        preview["csrf_token"] = token
        response = JSONResponse(preview)
        response.set_cookie(
            "acc_csrf",
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=600,
            path="/api/actions/wake-all",
        )
    return response


@app.get("/api/actions/wake-all/result")
def wake_all_result(request: Request, idempotency_key: str) -> JSONResponse:
    _require_loopback(request)
    try:
        return JSONResponse(_wake_service().result(idempotency_key=idempotency_key))
    except WakeAllError as exc:
        code = str(exc)
        status = 503 if "unavailable" in code or "failed" in code else 400
        raise HTTPException(status_code=status, detail=code) from exc


@app.post("/api/actions/wake-all/submit")
def wake_all_submit(request: Request, body: WakeAllSubmit) -> JSONResponse:
    _require_loopback(request, require_origin=True)
    if not capability_enabled():
        raise HTTPException(status_code=403, detail="capability_disabled")
    header_token = request.headers.get("x-csrf-token") or ""
    cookie_token = request.cookies.get("acc_csrf") or ""
    if not header_token or header_token != cookie_token or not verify_csrf(header_token, body.idempotency_key):
        raise HTTPException(status_code=403, detail="csrf_failed")
    try:
        result = _wake_service().submit(
            idempotency_key=body.idempotency_key,
            preview_hash=body.preview_hash,
            confirmed=body.confirm,
        )
    except WakeAllError as exc:
        code = str(exc)
        status = 409 if code == "preview_changed_refresh_required" else 503 if "unavailable" in code or "failed" in code else 400
        raise HTTPException(status_code=status, detail=code) from exc
    return JSONResponse(result)


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    async def event_source():
        last_token = None
        last_signature = ""
        last_event_id = ""
        last_reconcile = 0.0
        last_heartbeat = 0.0
        while True:
            now = time.monotonic()
            token = data.source_change_token()
            should_reconcile = (
                last_token is None
                or token != last_token
                or now - last_reconcile >= 30.0
            )
            if should_reconcile:
                payload = data.dashboard()
                payload["version"] = __version__
                payload["mode"] = "read-only"
                signature = str(payload.get("refresh", {}).get("signature") or "")
                event_id = str(payload.get("refresh", {}).get("event_id") or signature[:24])
                if not last_signature or signature != last_signature:
                    yield (
                        f"id: {event_id}\n"
                        "event: dashboard\n"
                        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                    )
                    last_signature = signature
                    last_event_id = event_id
                    last_heartbeat = now
                last_token = token
                last_reconcile = now
            if now - last_heartbeat >= 15.0:
                heartbeat = {
                    "event_id": last_event_id,
                    "generated_at": time.time(),
                    "mode": "read-only",
                }
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat, separators=(',', ':'))}\n\n"
                last_heartbeat = now
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
