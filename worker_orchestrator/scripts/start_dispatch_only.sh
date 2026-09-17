#!/bin/sh
set -eu

STATE="${HOME}/.local/share/worker-orchestrator"
export ORCHESTRATOR_DB="${STATE}/state.sqlite3"
export MASTER_REPO="nicofroeba16-cell/ha-grok-bridge"
export MASTER_ISSUE="3"
export ALLOWED_REPOSITORIES="nicofroeba16-cell/ha-grok-bridge,nicofroeba16-cell/HA-CONFIG,nicofroeba16-cell/ha-grok-bridge-live,nicofroeba16-cell/File-Bridge-mcp,nicofroeba16-cell/ha-ios-next,nicofroeba16-cell/ha-ios-next-ios,nicofroeba16-cell/Intelligence-Suite-,nicofroeba16-cell/AmazonTV-App,nicofroeba16-cell/Brother-Printer-Companion"
export RECONCILE_SECONDS="300"

# Production is dispatch-only. No local AI/code worker is launched.
unset WORKER_COMMAND
export WORKSPACE_ROOT=""
export WORKER_ENV_ALLOWLIST=""

# Reuse the runner's authenticated GitHub CLI token without persisting it.
export GITHUB_TOKEN="$(gh auth token)"

exec "${STATE}/venv/bin/worker-orchestrator" run \
  --webhook-host 127.0.0.1 \
  --webhook-port 8787
