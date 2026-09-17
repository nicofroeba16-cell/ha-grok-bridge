# Product UI Refinement v4 — before / after rationale

Baseline: `9bc079212bb06d0901356a3c74f8be622049e72b` (v3 accepted read-only/stability UI).

## Major hierarchy changes

- **Before:** the desktop hero grid stretched the short Master card to the height of the attention list, leaving a large visually empty Master surface. **After:** the hero uses intrinsic card heights and the Master card carries compact progress, percent and update facts.
- **Before:** six large KPI cards consumed a complete row and multiple mobile rows. **After:** the same six source-backed signals are compact; iPhone widths use an internally scrollable strip so the body remains width-contained.
- **Before:** the page relied mostly on vertical scanning. **After:** a read-only anchor navigation exposes Overview, Worker, Evidence, Activity and System without introducing any action endpoint.
- **Before:** worker cards used two desktop columns. **After:** wide desktop uses three compact columns, medium uses two and iPhone remains one, with operational evidence preserved under progressive disclosure.
- **Before:** attention rows followed ledger order. **After:** the attention surface is a derived presentation only and prioritizes `WAITING_FOR_USER`, then `ERROR`, then `BLOCKED`/`STALLED`; degraded sources, uncertain wakes and stale evidence remain explicitly signaled without changing resolved state.

## Stability preserved

- large presentation caps remain 80 workers / 80 evidence / 80 events / 40 wakes / 80 routes / 18 graph children;
- logical SSE idempotence now ignores only `health.generated_at`, preventing needless worker DOM rebuilds when state is otherwise identical;
- 72 churn iterations retained a stable large-payload DOM size;
- degraded, reconnect and empty views remain truthful;
- `STALE >24H` is derived only from source `updated_at` age and never changes state precedence;
- no write endpoint, public binding, service install/start or source-ledger mutation was added.

## Visual acceptance snapshot

- Desktop `1440x1100`: body scroll width = viewport width; 3 worker columns; Master card 196 px high vs attention 367 px (no forced stretch).
- iPhone `393x852`: body scroll width = viewport width; 1 worker column; Worker Registry begins at y=705, within the first viewport after overview/triage/signals.
- Wider iPhone `430x932`: body scroll width = viewport width; 1 worker column; same hierarchy remains readable.
- Large dataset: 80 worker cards, 80 events, 80 evidence rows, 40 wake rows, 19 graph nodes (root + capped 18 children) with truthful overflow labels.

See `ui-product-v4-metrics.json` for the machine-captured acceptance values and the three adjacent PNG files for final visual evidence.
