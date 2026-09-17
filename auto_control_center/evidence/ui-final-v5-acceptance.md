# AUTO Control Center — Final Product Polish v5 Acceptance

Baseline: `fe1ec92854612f5390a82a7508b846a445f14d26`
Goal: `auto-control-center-final-polish-v5`
Mode: isolated read-only/simulation acceptance; no live deployment or ledger mutation.

## Final product changes

- Mobile navigation is a five-segment tab bar with an obvious active state and no clipping at 393 px.
- Normal product surfaces use human labels (`Action required`, `Failed pre-send`, `Cancelled / superseded`) instead of raw underscore enums. Raw source state remains available inside Evidence details.
- Attention ordering is user action -> error -> blocked/stalled -> degraded -> uncertain -> stale. The highest item receives the primary visual treatment, while up to two system-signal slots remain visible inside the six-item cap so uncertainty/degraded/stale cannot disappear behind worker volume.
- Mobile KPIs use a fixed 3x2 compact grid, removing the partial-card carousel edge from v4.
- Normal worker cards stay compact; problem workers keep their actionable reason visible. Evidence remains progressively disclosed.
- Worker search and read-only filters cover Action, Running, Error, Blocked, Done/Ready plus an independent Only-problems toggle.
- Evidence summaries expose CI, short HEAD and freshness before worker details are expanded.
- Global degraded/offline/reconnect banners are explicit and preserve the last rendered view.
- Master progress is shown once with a subdued relative timestamp; exact time remains available as detail/title text.
- Realistic fixtures use current read-only runner worker names/source shapes, including long goals, shared targets and long blocker text.

## Chromium acceptance

- `1440x1100`: body width 1440/1440; three worker columns; nav contained.
- `393x852`: body width 393/393; all five mobile tabs contained; one worker column.
- `430x932`: body width 430/430; all five mobile tabs contained; one worker column.
- Search scenario returns exactly one `Mähroboter` worker.
- Action filter: 3 workers; Only-problems mode: 11 workers in the representative payload.
- Worker evidence expand/collapse verified.
- Uncertain attention is visible in the normal mixed view; stale acceptance exposes 6 stale worker badges plus a visible stale system signal.
- Degraded and reconnect states verified; Reduced Motion reports zero transition duration.
- Normal surface raw-enum scan: zero matches for `WAITING_FOR_USER`, `FAILED_PRE_SEND`, `CANCELLED_SUPERSEDED`, `STATE_CHANGED`.
- Debug/raw-path/secret surface scan: zero matches; console errors: 0; page errors: 0.

## Stability preservation

- Large payload renders 80 workers, 80 evidence rows, 80 events, 40 wake rows and 18/36 child goals (+ master) with truthful overflow labels.
- Large DOM: 4171 nodes; idempotent repeat keeps the same first worker node and the same node count.
- 72 churn iterations: every sampled large render remains exactly 4171 DOM nodes.
- No horizontal body overflow in any required viewport.

Evidence:
- `ui-final-v5-desktop-1440x1100.png`
- `ui-final-v5-iphone-393x852.png`
- `ui-final-v5-iphone-wide-430x932.png`
- `ui-final-v5-interaction.gif` (7 interaction frames)
- `ui-final-v5-metrics.json`

Known limitation: this is intentionally not a live deployment. Filters are client-side view state and reset on page reload. Final visual approval remains a user gate.
