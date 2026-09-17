# AUTO Control Center

Read-only visual control plane for the Master / Worker Orchestrator / Browser Wake stack.

## Scope of v0.1

The first version is deliberately read-only. It visualizes current state but exposes no endpoints for wake, retry, restart, merge, deploy, approval, secret handling, device control, network changes, or other mutations.

## Data sources

- Worker Orchestrator SQLite database
- Browser Wake SQLite database
- Browser Wake route registry
- read-only `systemctl --user show` state for selected services

SQLite databases are opened with SQLite URI `mode=ro`.

## UI

The dashboard shows:

- newest Master request and child graph
- worker cards with lifecycle state, goal, repo, branch, HEAD, CI and blockers
- Orchestrator event timeline
- Browser Wake delivery history and pending queues
- route-binding summary without exposing destination URLs
- systemd service health
- live updates over Server-Sent Events

## API

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/master`
- `GET /api/workers`
- `GET /api/events`
- `GET /api/wakes`
- `GET /api/routes`
- `GET /api/stream`

FastAPI docs are available at `/api/docs`.

## Local runtime target

The service is intended to run on the authorized runner and bind only to:

`127.0.0.1:8877`

The example user-service is `auto-control-center.service.example`.

## Environment

Optional path overrides:

- `ACC_ORCHESTRATOR_DB`
- `ACC_BROWSER_WAKE_DB`
- `ACC_BROWSER_ROUTES`
- `ACC_SYSTEMD_SERVICES`

No token or secret environment variable is required by this MVP.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r auto_control_center/requirements.txt
python -m unittest auto_control_center.tests.test_data
uvicorn auto_control_center.app:app --host 127.0.0.1 --port 8877
```

## Safety

The UI must not become an alternative source of truth. GitHub, the Orchestrator database, Browser Wake ledger, exact-head CI and runtime evidence remain authoritative. A worker message alone must never be rendered as verified completion unless the canonical state sources support it.

Any future mutation endpoint must be introduced separately with explicit user-gate semantics, audit logging, exact target display and fail-closed behavior.
