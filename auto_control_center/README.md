# AUTO Control Center

Local read-only visual control plane for the Master / Worker Orchestrator / Browser Wake stack.

## Read-only safety contract

The application has **no mutation endpoints**. It can read current state, but it cannot wake, retry, restart, merge, deploy, approve, rotate secrets, change devices, modify Home Assistant, or install/start services. The supported launcher binds to `127.0.0.1` only.

GitHub and the existing ledgers remain the source of truth. The UI never promotes prose such as `last_progress` into verified state. Display-state precedence is:

1. current `user_gate` -> `WAITING_FOR_USER`
2. current blockers -> `BLOCKED`
3. current failing CI state -> `BLOCKED`
4. otherwise the current Orchestrator ledger state

This implements **VERIFIED CURRENT STATE > worker report**. A worker report remains explicitly labeled unverified in the UI.

## Registry drift and delivery semantics

Registry rows are never silently collapsed. When two or more worker identities share the same `(repository, branch, goal_version)` target, every row remains visible and the UI marks the group as a **Shared Target**. This is drift evidence only: the Control Center does not infer which identity is canonical or legacy.

Browser-Wake delivery states remain distinct:

- verified/success-like delivery -> green
- explicit failure/blocking -> red
- `UNCERTAIN` -> amber and explicitly described as not verified, neither success nor failure
- cancelled/superseded -> neutral/muted

Worker lifecycle states also retain dedicated treatment for `READY`, `RUNNING`, `BLOCKED`, `WAITING_FOR_USER`, `DONE`, `DORMANT` and `ERROR`.

## Data sources

All adapters are read-only:

- Worker Orchestrator SQLite database via SQLite URI `mode=ro`
- Browser Wake SQLite database via SQLite URI `mode=ro`
- Browser Wake route registry; only binding kind is exposed, never destination URL/title values
- `systemctl --user show` for selected service state

The API does not expose configured filesystem paths. Text and structured data pass through recursive secret redaction before they reach the API/UI.

## v3 UI stability model

The v3 refinement pass keeps the same read-only API while reducing UI noise and bounding browser work:

- Master/goal and current attention items are shown first
- worker cards show state/goal immediately; operational metadata and worker prose are collapsed under **Details**
- Shared-Target drift remains visible but secondary
- identical SSE payloads are not re-rendered
- client DOM rendering is capped at 80 workers, 80 evidence rows, 80 events, 40 wake rows, 80 routes and 18 graph children
- overflow copy states how many additional records remain in the authoritative ledger
- reconnect preserves the last rendered view and says `Reconnecting · letzte Ansicht`
- unreadable/missing sources render `DEGRADED · Read-only`; they never imply healthy/READY state
- mobile layout keeps one worker column and body-width containment while the dependency graph scrolls internally
- `:focus-visible` and reduced-motion behavior are included

The caps are presentation limits only. They do not truncate or mutate source ledgers.

## Deterministic simulation harness

`auto_control_center/simulation.py` creates sanitized, deterministic payloads for isolated stability testing. `simulation_matrix()` provides:

- `mixed`: lifecycle and wake-state churn
- `partial`: missing/partial worker fields
- `degraded`: unavailable/read-only source health
- `stale`: intentionally old worker evidence timestamps
- `empty`: empty-state rendering
- `large`: 180 workers, 900 events, 500 wake deliveries and more graph children than the UI presentation cap

The simulation includes Shared-Target aliases, independent CI-failure precedence, dedicated `ERROR`, secret-like sample material that must be redacted, and minimized route records. It never opens or writes the real Orchestrator or Browser-Wake ledgers.

## UI / API

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

No token or secret environment variable is required.

## Tests and visual acceptance

The suite covers adapters/no-write behavior, route minimization, secret redaction, state/CI precedence, Shared-Target preservation, wake classes, loopback-only launch, read-only API surface, simulation scenarios and responsive/stability UI contracts.

Visual acceptance is performed only in an isolated browser with simulation payloads. Required target viewports are desktop `1440x1100`, iPhone `393x852`, plus a wider modern-iPhone sanity width. Large-data and repeated-render checks verify bounded DOM size and idempotent rendering. This is not a live deployment.

No existing Orchestrator, Browser Wake, Home Assistant, service, device, route or secret state is mutated by these tests.
