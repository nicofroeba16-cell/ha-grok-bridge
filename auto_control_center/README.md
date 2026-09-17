# AUTO Control Center

Local evidence-first operator UI for the Master / Worker Orchestrator / Browser Wake stack.

## Safety contract

The dashboard data plane remains read-only and the supported launcher binds to 127.0.0.1 only. One narrowly scoped optional mutation exists: the guarded Alle Worker aufwecken action may enqueue eligible workers into the canonical Browser-Wake pending queue. That capability is disabled by default and never changes worker goals or automates ChatGPT directly from the dashboard. Retry, restart, merge, deploy, approval, secret, Home Assistant, device, network and service-management actions remain unavailable.

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

Normal dashboard adapters are read-only:

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

## v4 product UI refinement

v4 keeps every v3 safety/stability invariant but changes the operator presentation so important state is reached faster:

- a sticky anchor navigation links Overview, Worker, Evidence, Activity and System without adding any mutation action
- the Master card no longer stretches to match a taller attention panel; it uses its own intrinsic height and adds compact progress/update facts
- attention is explicitly triaged: `WAITING_FOR_USER` first, then `ERROR`, `BLOCKED`/`STALLED`, followed by degraded-source, uncertain-delivery and stale-evidence signals
- the six operational counters are compact signals rather than large dashboard cards; on iPhone widths they become an internally scrollable strip instead of consuming multiple rows
- desktop worker density increases from two to three columns at wide width, while medium remains two columns and iPhone remains one column
- worker cards keep state/goal/actionable notices visible and move repository/branch/HEAD/CI/raw worker report evidence behind progressive disclosure
- evidence older than 24 hours is marked `STALE >24H` from the source `updated_at` timestamp; the threshold is explicit and does not alter resolved state
- logical SSE idempotence ignores only the generated health timestamp, so an otherwise unchanged payload does not rebuild the worker DOM every refresh
- empty, degraded and reconnect copy remains explicit and read-only; no state is inferred as healthy/READY from missing evidence

### Before / after rationale

The v3 acceptance screenshot showed a large empty Master/Goal surface because the hero grid stretched the shorter Master card to the height of the attention list, six large statistic cards consumed a full row, and desktop workers used only two columns. On iPhone, the first worker card began below the initial viewport, making triage depend on long vertical scanning.

v4 removes the stretch, adds operator anchors, compresses status into a mobile-scrollable signal strip, prioritizes action-required rows, and uses three desktop worker columns. Evidence remains available through `Evidence details`; no source row is collapsed or canonicalized. This is a presentation change only: state precedence, redaction, route minimization, read-only API surface, loopback launcher and large-data caps are unchanged.

## v5 final product polish

The v5 surface keeps the same read-only API and source-of-truth rules, but presents the data as an operator product rather than a test dashboard:

- five-section navigation becomes a thumb-friendly segmented tab bar on iPhone and tracks the active section
- raw source enums are mapped to human presentation labels; raw/internal values remain available only in evidence details
- attention is ordered as user action -> error -> blocked/stalled -> degraded source -> uncertain delivery -> stale evidence
- mobile operational signals use a deliberate 3x2 compact grid with no clipped-card affordance
- worker cards stay compact for healthy states while problem workers keep the current action visible
- client-only worker search, state filters and **Only problems** never mutate source state
- Evidence rows show CI, short HEAD and source freshness before any worker detail is opened
- global degraded/offline/reconnecting states are explicit and preserve the last view
- relative times are shown on the surface while exact timestamps stay available in title/detail text

The deterministic fixture set uses current read-only worker naming and source shape (including long goals, shared targets and a long blocker) captured from the runner without writing the real ledger. Source-derived project names are intentionally preserved even when they contain words such as “Simulation”.

Final v5 acceptance covers 1440x1100, 393x852 and 430x932, worker search/filter/Only-problems, expand/collapse, attention changes, degraded/reconnect, reduced motion, large-data caps, idempotence and 72 refresh/churn iterations. Screenshots, interaction GIF and machine-readable metrics live under `auto_control_center/evidence/`.

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
- ACC_WAKE_ALL_ENABLED (default off)
- ACC_MASTER_REF (wake payload master reference only)

No token or secret environment variable is required.

## Tests and visual acceptance

The approved v5 baseline suite covered adapters/no-write behavior, route minimization, secret redaction, state/CI precedence, Shared-Target preservation, wake classes, loopback-only launch, simulation scenarios and responsive/stability UI contracts.

The approved v5 visual acceptance was performed in an isolated browser with simulation payloads at desktop 1440x1100, iPhone 393x852 and a wider modern-iPhone width. Large-data and repeated-render checks verified bounded DOM size and idempotent rendering.

No existing Orchestrator, Browser Wake, Home Assistant, service, device, route or secret state was mutated by the v5 acceptance. The additional guarded Wake-All acceptance is documented below.

## Guarded Wake All v2

Alle Worker aufwecken is a route-registry-driven operator action layered on the existing Browser-Wake queue contract.

Safety and eligibility rules:

- The master route is never included.
- Worker names and counts are derived from the current worker ledger and route registry for every preview.
- Missing, malformed or ambiguous routes and duplicate route destinations fail closed.
- DONE workers are skipped unless the orchestrator has already materialized a new non-DONE state; the Control Center never invents that change.
- Existing pending deliveries are skipped.
- A latest UNCERTAIN delivery is skipped and is never automatically retried.
- ASSIGNED, RUNNING, BLOCKED, WAITING_FOR_USER, READY, ERROR and STALLED may be explicitly re-awakened when all other safety checks pass.
- Current project, chat and goal version are copied into the wake payload unchanged. The action never creates or broadens a goal.
- Route destinations are used internally only and are not exposed by preview or result responses.

Operator flow:

1. Open Alle Worker aufwecken.
2. Review routed, eligible and skipped counts plus exact skip reasons.
3. Tick the explicit confirmation control.
4. Submit the operation once. One idempotency key identifies the batch.
5. Repeated submits with the same key are deduplicated. A reload/new preview sees already-pending workers as protected.
6. The only write inserts deterministic messages into browser_wake_pending_worker.
7. The existing Browser-Wake coordinator performs delivery.
8. Result polling reports queued, verified, blocked, failed-pre-send and uncertain states from the canonical ledger without reading model output.

Security model:

- ACC_WAKE_ALL_ENABLED=1 explicitly enables the capability; absent or false means disabled.
- The POST endpoint accepts loopback clients only and requires exact same-origin validation.
- An operation-bound expiring CSRF token and matching HttpOnly SameSite cookie are required.
- The preview hash includes worker, goal, state and an internal route-target fingerprint, so route drift forces a new preview.
- Control-plane/source failures fail closed and there is no fallback sender.
- No route destination, token, secret, sensitive local path or browser profile data is exposed.
- Enabling the capability in a production runtime remains a separate live-system approval.

Additional action endpoints:

- GET /api/actions/wake-all/preview
- GET /api/actions/wake-all/result
- POST /api/actions/wake-all/submit

The POST endpoint is the only mutating HTTP surface and is disabled by default. There is no generic command or direct ChatGPT automation endpoint.

Optional local capability enablement, only after separate runtime approval:

    ACC_WAKE_ALL_ENABLED=1 python -m auto_control_center

The launcher still binds only to 127.0.0.1. Enabling Wake All does not authorize starting or restarting Browser-Wake services or changing production route/runtime configuration.

## Wake All test and acceptance model

The Wake-All unit suite uses temporary SQLite and route fixtures for eligibility, skip reasons, DONE protection, pending and UNCERTAIN protection, stale-preview detection, idempotency, concurrency, CSRF and truthful result mapping.

Integration acceptance launches only an ephemeral loopback Control Center process against temporary Orchestrator, Browser-Wake and route fixtures. It verifies missing/cross-origin rejection, valid confirmed enqueue and replay deduplication without touching the production Browser-Wake database.

Browser acceptance uses mocked temporary action responses and verifies desktop 1440x1100, iPhone 393x852 and 430x932 layouts, preview, cancellation, explicit confirmation, disabled capability, double-click protection and mixed verified/queued/uncertain/blocked/failed-pre-send/skipped outcomes.

No real worker is woken during tests. Production ledgers, routes, services, Home Assistant, devices, networks and secrets remain outside the test write path.
