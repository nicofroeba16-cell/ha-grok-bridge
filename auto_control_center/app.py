from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from . import __version__
from . import data

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "static" / "index.html"

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


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    async def event_source():
        while True:
            payload = data.dashboard()
            payload["version"] = __version__
            payload["mode"] = "read-only"
            yield f"event: dashboard\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
