# AUTO Control Center

Local read-only visual control plane for the Master / Worker Orchestrator / Browser Wake stack.

## v1/v2 read-only safety contract

The application has **no mutation endpoints**. It can read current state, but it cannot wake, retry, restart, merge, deploy, approve, rotate secrets, change devices, modify Home Assistant, or install/start services. The supported launcher binds to `127.0.0.1` only.

GitHub and the existing ledgers remain the source of truth. The UI never promotes prose such as `last_progress` into verified state. Display-state precedence is:

1. current `user_gate` -> `WAITING_FOR_USER`
2. current blockers -> `BLOCKED`
3. current failing CI state -> `BLOCKED`
4. otherwise the current Orchestrator ledger state

This implements the project rule **VERIFIED CURRENT STATE > worker report**. The UI labels its local worker/evidence source as `orchestrator_ledger`; it does not claim a GitHub verification that is not present in the ledger.

## Registry drift and delivery semantics

Registry rows are never silently collapsed. When two or more worker identities share the same `(repository, branch, goal_version)` target, every row remains visible and the UI marks the group as a **Shared Target**. This is drift evidence only: the Control Center does not infer which identity is canonical or legacy.

Browser-Wake delivery states are also kept semantically distinct:

- verified/success-like delivery -> green
- explicit failure/blocking -> red
- `UNCERTAIN` -> amber and explicitly described as **not verified**, neither success nor failure
- cancelled/superseded -> neutral/muted

## Data sources

All adapters are read-only:

- Worker Orchestrator SQLite database via SQLite URI `mode=ro`
- Browser Wake SQLite database via SQLite URI `mode=ro`
- Browser Wake route registry; only binding kind is exposed, never the destination URL/title value
- `systemctl --user show` for selected service state

The API intentionally does not expose configured filesystem paths. Route destinations are summarized, not returned. Text and structured data pass through recursive secret redaction before they reach the API/UI.

## UI / API

The responsive desktop/iPhone dashboard provides:

- Master/goal overview and progress
- visual child/dependency graph
- worker cards with resolved state, goal, repo, branch, issue, HEAD, CI, blockers and user gates
- explicit Shared-Target registry-drift indicators without canonical inference
- event timeline
- Browser Wake delivery/queue status with distinct uncertain/failure/success/cancelled semantics
- Evidence/CI overview from current ledger fields
- route-binding summary
- source and service health
- live refresh over Server-Sent Events

Read-only endpoints:

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/master`
- `GET /api/workers`
- `GET /api/events`
- `GET /api/wakes`
- `GET /api/routes`
- `GET /api/evidence`
- `GET /api/stream`

FastAPI docs are at `/api/docs`.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r auto_control_center/requirements.txt
python -m unittest discover -s auto_control_center/tests -t . -v
python -m auto_control_center
```

The supported launcher binds exactly to `127.0.0.1:8877`. Only the port can be changed:

```bash
ACC_PORT=8899 python -m auto_control_center
```

Do not launch this version with a public bind such as `0.0.0.0`.

## Optional source overrides

- `ACC_ORCHESTRATOR_DB`
- `ACC_BROWSER_WAKE_DB`
- `ACC_BROWSER_ROUTES`
- `ACC_SYSTEMD_SERVICES`
- `ACC_PORT`

No token or secret environment variable is required by this MVP.

## Tests and acceptance

The test suite covers:

- SQLite adapters and byte-for-byte no-write behavior
- route destination minimization
- recursive secret redaction
- state precedence and CI mapping
- registry shared-target preservation without canonical inference
- distinct Browser-Wake status classes, including `UNCERTAIN`
- loopback-only launcher behavior
- API surface (GET/HEAD only) and required endpoints
- responsive desktop/iPhone visual contracts

Visual acceptance can be performed in an isolated browser using safe representative records derived from read-only runner data. This does not install or start the Control Center on the runner and must not be described as a live deployment.

No existing Orchestrator, Browser Wake, Home Assistant, service, device or secret state is mutated by these tests.
