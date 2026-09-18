# Browser Wake — loop-safe Master/Worker activation

`browser_wake` is an input-only companion to the Worker Orchestrator. It wakes existing ChatGPT conversations through an already authenticated local browser profile without using the OpenAI API.

It is intentionally **not** a ChatGPT output scraper. GitHub remains the canonical control plane and status log.

## Flow

```text
Master writes MASTER_REQUEST to GitHub
  -> browser_wake detects a new request
  -> exactly one WORKER_WAKE is sent to each configured worker chat
  -> worker reads its GitHub workstream and performs owned work
  -> worker posts canonical WORKER_STATUS / WORKER_DONE to GitHub
  -> browser_wake detects a new master-relevant status
  -> one debounced MASTER_WAKE is sent to the configured Master chat
  -> Master reads GitHub and coordinates the next step
```

The browser relay never reads ChatGPT responses and never copies ChatGPT output back into GitHub.

## Loop guards

1. GitHub item IDs are consumed through a persistent SQLite cursor.
2. Worker wake IDs are deterministic: `request_id + goal_version + child_id`.
3. Canonical worker `FINGERPRINT` values are deduplicated independently of GitHub comment IDs.
4. Multiple worker status events are collapsed into one Master wake after a debounce window.
5. `MASTER_*` and `BROWSER_WAKE_*` comments never wake Master.
6. A possible post-click crash is `UNCERTAIN` and is never retried automatically.
7. Only failures proven to happen before Send may retry, with a hard maximum of three attempts.
8. First activation bootstraps to the current GitHub cursor; historical events are not replayed unless `--replay-existing` is explicitly used.
9. Canonical `RUNNING` is not inferred from Send, navigation, `IN_FLIGHT`,
   `UNCERTAIN`, or helper prose. The web helper must verify persistence of the
   wake after reload in the same exact conversation. Browser-Wake persists that
   receipt, publishes one `BROWSER_WAKE_DELIVERY` source event, and the
   Orchestrator promotes only the matching current assigned goal.
10. Verified-delivery publication is retried from SQLite without re-sending the
    wake. Stale goal versions and terminal worker states fail closed.

The desired steady-state property is:

```text
new GitHub information -> one wake -> processing -> quiet
```

## Live safety

Source code and tests do not activate browser automation.

Every live-system mutation requires separate explicit user approval. This includes installing/enabling a service, starting or stopping a browser relay, changing runtime files, changing secrets, modifying the authenticated browser profile, sending a production wake, restarting the orchestrator, or enabling automatic startup.

A browser wake is not an authorization for merge, deployment, restart, device/network/Home Assistant changes, secret rotation, destructive cleanup, release/tag mutation, or any other gated action.

## Route file

Routes may use an exact ChatGPT conversation URL or an exact visible chat title. Title routing keeps private conversation IDs out of the repository and is the preferred form for the free local transports.

```json
{
  "__master__": {"title": "Master-Verteilung"},
  "Projekt: Example → Chat: Worker": {"title": "Worker Chat"}
}
```

`browser_wake_search` converts titles to `chat-title:<exact title>` and fails closed if the title is missing or ambiguous. URL locators remain supported by the legacy web helper.

## Local transport backends

### Web browser helper

`browser_chatgpt_send.mjs` supports a long-lived authenticated Chrome process through `CHATGPT_BROWSER_URL`. Production uses the loopback-only endpoint `http://127.0.0.1:9224` and the dedicated profile `chrome-profile-web-v2`. The sender connects to the existing browser, sends one input, verifies assistant completion without reading assistant content, verifies persistence after reload, and then disconnects without closing Chrome.

`browser_chatgpt_health.mjs` is the read-only health probe. It reports only technical states such as `HEALTHY`, `BLOCKED_AUTH`, `BLOCKED_CHALLENGE`, `DEGRADED_UI`, or `BROWSER_DOWN`; it never reads model output. Challenges are never bypassed: an auth or protection gate must be completed normally before automation resumes.

### ChatGPT desktop helper

`chatgpt_desktop_send.mjs` is the zero-cost fallback for the official local ChatGPT desktop app. It connects only to a pre-existing loopback DevTools endpoint supplied through `CHATGPT_DESKTOP_DEBUG_URL`; it does not launch or reconfigure the app itself.

The desktop helper accepts the same one-line JSON request contract as the web helper. It resolves `chat-title:<exact title>`, targets exactly one visible composer and one send control, types the wake, and returns delivery metadata only. It never reads assistant/model output.

A `desktop-thread:<UUID>` locator is parsed but intentionally disabled until mapping between an existing regular ChatGPT conversation and the desktop app's internal thread identity has been verified live. Static bundle inspection confirms `codex://threads/<UUID>` deep links for local app threads, but those IDs must not be guessed from ordinary `chatgpt.com/c/...` URLs.

Security properties:

- debug endpoint must be `http(s)` on loopback only; credentials in the URL are rejected;
- ordinary web conversation URLs are not silently converted to desktop thread IDs;
- missing or ambiguous title/search/composer/send controls fail before Send;
- once the send click is initiated, any failure is treated as uncertain and is never retried automatically by the coordinator;
- no API key and no paid OpenAI API path are used.

The current live gate includes starting/reconfiguring the desktop app, enabling a DevTools endpoint, attaching the production helper, changing services/runtime files, or sending any real wake.


## systemd service integration

The production service layout is deliberately split into three user units:

- `browser-wake-chrome.service` owns one long-lived authenticated Chrome process and exposes DevTools only on `127.0.0.1:9224`.
- `browser-wake.service` owns only the GitHub reconcile/dispatch loop. It requires the Chrome service and runs the read-only browser health probe before starting.
- `master-autonomous-orchestration.target` groups both units for one explicit activation point.

All runtime code is referenced through `~/.local/share/browser-wake/current`, which remains an atomic symlink to an immutable release directory. The prepared runtime keeps its SQLite delivery ledger and route files outside the release tree.

`worker_orchestrator/scripts/prepare_browser_wake_services.sh` is intentionally **prepare-only**. It installs the three unit files, normalizes the runtime environment to the long-lived browser profile and loopback endpoint, runs `systemctl --user daemon-reload`, verifies the units, and refuses a state in which any of the new units is already enabled. It never starts, restarts, enables, or uses `--now`.

Activation therefore remains a separate explicit gate:

```text
source + CI green
  -> prepare units/config (disabled + inactive)
  -> explicit activation approval
  -> enable/start target
  -> TEST_ONLY full E2E
  -> restart/recovery matrix
  -> short soak
  -> autonomous operation
```

The existing independent `worker-orchestrator.service` is not modified or restarted by browser-wake preparation.

## Daemon

Example command after a separate live activation approval:

```bash
python -m worker_orchestrator.browser_wake \
  --db /path/to/browser-wake.sqlite3 \
  --routes-file /path/to/browser-chat-routes.json \
  --browser-command "node /path/to/browser_chatgpt_send.mjs" \
  run
```

Default poll interval is 15 seconds and Master debounce is 10 seconds.

Do not use `--replay-existing` in production unless a deliberate historical replay has been explicitly approved and reviewed; the normal first start intentionally begins at the current Master Issue cursor to prevent a wake storm.
