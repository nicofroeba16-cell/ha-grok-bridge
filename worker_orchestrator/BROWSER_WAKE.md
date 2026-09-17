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

The desired steady-state property is:

```text
new GitHub information -> one wake -> processing -> quiet
```

## Live safety

Source code and tests do not activate browser automation.

Every live-system mutation requires separate explicit user approval. This includes installing/enabling a service, starting or stopping a browser relay, changing runtime files, changing secrets, modifying the authenticated browser profile, sending a production wake, restarting the orchestrator, or enabling automatic startup.

A browser wake is not an authorization for merge, deployment, restart, device/network/Home Assistant changes, secret rotation, destructive cleanup, release/tag mutation, or any other gated action.

## Route file

Routes are kept outside the public repository because conversation URLs identify private conversation targets.

```json
{
  "__master__": {
    "url": "https://chatgpt.com/c/<master-conversation-id>"
  },
  "Projekt: Example → Chat: Worker": {
    "url": "https://chatgpt.com/g/g-p-<project-id>/c/<worker-conversation-id>"
  }
}
```

Only credential-free `https://chatgpt.com/.../c/<conversation-id>` URLs are accepted.

## Browser helper

`browser_chatgpt_send.mjs` uses Puppeteer and accepts exactly one JSON object on stdin:

```json
{
  "message_id": "worker-wake:req:v1:child",
  "destination": "https://chatgpt.com/c/...",
  "payload": "WORKER_WAKE ..."
}
```

Required runtime variables:

- `CHATGPT_PROFILE_DIR` — authenticated Chrome user-data directory.
- `CHATGPT_CHROME_BIN` — Chrome executable, default `/usr/bin/google-chrome`.
- `CHATGPT_BROWSER_HEADLESS` — defaults to headless; set `false` only for an explicitly approved interactive runtime.

The helper reports only delivery metadata such as `sent` or `failed`; it does not return page or model output.

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
