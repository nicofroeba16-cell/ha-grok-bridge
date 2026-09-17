# Chat Relay API v1

## Purpose

`Chat Relay API v1` is the project-owned receiving endpoint for the Worker Orchestrator
`chat_relay` transport.

It deliberately uses the official OpenAI API conversation model. It does **not** automate
the ChatGPT web/app UI, reuse browser sessions, or claim that an existing ChatGPT UI
conversation was awakened.

The delivery chain is:

`Master goal -> Worker Orchestrator -> chat_relay -> Chat Relay API -> OpenAI Responses API conversation`

## HTTP contract

### Health

```http
GET /healthz
```

A healthy server returns JSON containing:

```json
{
  "status": "ok",
  "service": "chat-relay",
  "api_version": "v1",
  "delivery_semantics": "openai_api_conversation",
  "chatgpt_ui_injection": false
}
```

### Dispatch

```http
POST /v1/chat-relay/messages
Authorization: Bearer <CHAT_RELAY_TOKEN>
Content-Type: application/json
Idempotency-Key: <optional client header>
```

Request body:

```json
{
  "message_id": "request-123:v1:worker-a",
  "destination": "chat-route:project/worker-a",
  "payload": "GOAL PROMPT\n..."
}
```

The server uses `message_id` as its durable project-level idempotency key even if the
client also sends an `Idempotency-Key` HTTP header.

Success is `202 Accepted`. Replaying the same `message_id` with identical destination
and payload remains accepted without a second provider call. Reusing a `message_id` for
different content returns `409 Conflict`.

Unknown routes, unsupported providers, malformed requests and failed authentication are
fail-closed.

## Route configuration

`CHAT_RELAY_ROUTES_JSON` is a server-side mapping. Only the `openai_responses` provider is
accepted in v1, and the target must be an OpenAI API conversation id (`conv_...`).

Example:

```json
{
  "chat-route:project/worker-a": {
    "provider": "openai_responses",
    "conversation": "conv_REPLACE_WITH_API_CONVERSATION_ID",
    "model": "gpt-5.6"
  }
}
```

A ChatGPT UI URL or chat id is intentionally rejected as a route target.

## Environment variables

Values are runtime secrets/configuration and must not be committed.

- `CHAT_RELAY_TOKEN` — bearer token required by the relay endpoint.
- `CHAT_RELAY_ROUTES_JSON` — exact route-to-API-conversation mapping.
- `OPENAI_API_KEY` — OpenAI API credential used only by the relay server.
- `OPENAI_MODEL` — default model when a route omits `model`; default `gpt-5.6`.
- `OPENAI_RESPONSES_URL` — default `https://api.openai.com/v1/responses`.
- `CHAT_RELAY_HOST` — default `127.0.0.1`.
- `CHAT_RELAY_PORT` — default `8790`.
- `CHAT_RELAY_DB` — SQLite idempotency ledger; default `./chat-relay.sqlite3`.

## Local start

From the repository root with `worker_orchestrator/src` on `PYTHONPATH` or after installing
the worker-orchestrator package:

```bash
python -m worker_orchestrator.relay_server --host 127.0.0.1 --port 8790
```

The matching Worker Orchestrator setting introduced by the relay client workstream is:

```text
CHAT_RELAY_URL=http://127.0.0.1:8790/v1/chat-relay/messages
```

Keep the listener on loopback unless an authenticated TLS reverse proxy or equivalent
transport protection is explicitly designed and approved.

## OpenAI transport semantics

For each accepted route, the relay calls:

```text
POST https://api.openai.com/v1/responses
```

with:

- `model`
- `conversation` set to the configured `conv_...` API conversation id
- `input` set to the relay payload

The OpenAI response id (`resp_...`) is stored in the local idempotency ledger and returned
as relay evidence.

OpenAI API conversations and ChatGPT application conversations are separate surfaces.
This endpoint therefore never reports `chatgpt_ui_injection=true`.

## Durability and recovery

The relay stores message ids, content hashes, state and response ids in SQLite/WAL.

- completed messages are deduplicated across restarts;
- same id + changed content is rejected;
- an interrupted `IN_FLIGHT` row is marked retryable on restart.

That last case provides at-least-once recovery semantics around a process crash; it is not
a claim of exactly-once delivery across an unknown provider-side outcome.

## Safety boundary

This source implementation does not install a service, start a runner daemon, expose a
public listener, merge a PR, or mutate Home Assistant/devices/network/secrets. Those are
separate user-gated runtime actions.
