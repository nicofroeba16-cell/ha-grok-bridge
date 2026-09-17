# Guarded Wake All v2 acceptance

Repository: nicofroeba16-cell/ha-grok-bridge
Branch: feature/auto-control-center-readonly-v1
Approved visual baseline: 68ed3e62ba5f62c89edf0e388535d508e16dbe2f
Validation target: the exact feature-branch HEAD recorded in the canonical Issue #42 integration dossier.

## Scope

This acceptance covers the guarded Alle Worker aufwecken function only. The approved v5 product layout remains the visual baseline.

The action uses the canonical Browser-Wake pending queue contract. Tests never point the action at the production queue and never send a real worker wake.

## Unit and security coverage

- route-registry-driven preview and counts
- master exclusion
- missing and ambiguous route protection
- DONE-without-changed-goal protection
- pending-delivery protection
- UNCERTAIN-delivery no-auto-retry protection
- current goal preservation
- route-target drift invalidates stale preview
- one idempotency key per operation
- repeated submit and concurrent same-key deduplication
- reload/new-preview protection through existing pending state
- truthful queued, verified, blocked, failed-pre-send and uncertain result mapping
- capability disabled by default
- loopback-only and exact same-origin validation
- operation-bound expiring CSRF protection
- no generic command endpoint

## Ephemeral API integration

An isolated loopback Control Center process was started against temporary Orchestrator, Browser-Wake and route fixtures.

Observed:
- preview: one eligible route, DONE and UNCERTAIN routes skipped with exact reasons
- submit with no Origin: HTTP 403
- submit with cross-origin localhost vs 127.0.0.1: HTTP 403
- valid same-origin confirmed submit: HTTP 200
- repeated same-key submit: idempotent replay, no duplicate queue row
- result endpoint: one truthful queued outcome
- private route titles were present only in the temporary queue fixture and did not appear in public API responses

## Browser acceptance

Targets:
- desktop 1440x1100
- iPhone 393x852
- iPhone-wide 430x932

Observed:
- action visible at all target widths
- no body or navigation horizontal overflow
- preview shows routed, eligible and skipped counts plus skip reasons
- cancellation performs no submit
- explicit checkbox confirmation is required
- rapid double click produces one submit request
- disabled capability keeps confirm disabled
- result UI distinguishes verified, queued, uncertain, blocked, failed-pre-send and skipped
- no browser console errors
- no page errors

## Evidence

- ui-wake-all-v2-desktop-preview-1440x1100.png
- ui-wake-all-v2-iphone-confirm-393x852.png
- ui-wake-all-v2-iphone-result-430x932.png
- ui-wake-all-v2-metrics.json

The canonical Issue #42 completion dossier binds these assets and this manifest to the final exact commit SHA after commit creation.

## Candidate validation before commit

- full unittest discovery: 55 tests passed
- inline browser JavaScript: node --check passed
- git diff --check passed
- runner Orchestrator DB, Browser-Wake DB and route-registry SHA-256 values were unchanged across the final unit/Node/diff validation window
- API integration used only temporary fixture paths
- browser acceptance used mocked temporary action responses and did not address any real route destination

The exact final commit is revalidated again after commit creation and is recorded in the canonical Issue #42 dossier.
