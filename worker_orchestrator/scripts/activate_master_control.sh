#!/usr/bin/env bash
set -euo pipefail

service_name="worker-orchestrator.service"
source_head="$(git -C "$GITHUB_WORKSPACE" rev-parse HEAD)"
if [[ "$source_head" != "$GITHUB_SHA" ]]; then
  echo "Checked-out source does not match the workflow head." >&2
  exit 1
fi
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XDG_RUNTIME_DIR="$runtime_dir"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$runtime_dir/bus}"

if [[ ! -S "$runtime_dir/bus" ]]; then
  echo "User systemd bus is unavailable at the expected runtime path." >&2
  exit 1
fi

systemctl --user is-enabled --quiet "$service_name"
exec_start="$(systemctl --user show "$service_name" --property=ExecStart --value)"
worker_bin=""
while IFS= read -r candidate; do
  if [[ -f "$candidate" && -x "$candidate" && "$(basename "$candidate")" == "worker-orchestrator" ]]; then
    worker_bin="$candidate"
  fi
done < <(grep -oE '/[^ ;"]+' <<<"$exec_start" || true)
if [[ -z "$worker_bin" ]]; then
  worker_bin="$(sed -nE 's/.*path=([^ ;]+).*/\1/p' <<<"$exec_start")"
fi
if [[ -z "$worker_bin" || ! -x "$worker_bin" ]]; then
  echo "Unable to resolve the installed worker-orchestrator executable." >&2
  exit 1
fi

python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
  echo "Python 3 is unavailable on the runner." >&2
  exit 1
fi

current_pid="$(systemctl --user show "$service_name" --property=MainPID --value)"
runtime_repo=""
candidate_paths=("$(readlink -f "/proc/$current_pid/cwd")" "$(dirname "$worker_bin")")
while IFS= read -r -d '' entry; do
  if [[ "$entry" == PYTHONPATH=* ]]; then
    IFS=':' read -r -a python_paths <<<"${entry#PYTHONPATH=}"
    candidate_paths+=("${python_paths[@]}")
  fi
done <"/proc/$current_pid/environ"
while IFS= read -r candidate; do
  candidate_paths+=("$(dirname "$candidate")")
done < <(grep -oE '/[^ ;"]+' "$worker_bin" 2>/dev/null || true)

for candidate in "${candidate_paths[@]}"; do
  [[ -d "$candidate" ]] || continue
  root="$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$root" && -f "$root/worker_orchestrator/pyproject.toml" ]]; then
    runtime_repo="$root"
    break
  fi
done
if [[ -z "$runtime_repo" ]]; then
  echo "The durable worker-orchestrator source checkout could not be resolved." >&2
  exit 1
fi

if [[ -n "$(git -C "$runtime_repo" status --short --untracked-files=no)" ]]; then
  echo "The durable runtime source has protected local changes; activation stopped." >&2
  exit 1
fi
git -C "$runtime_repo" fetch origin main
runtime_remote_head="$(git -C "$runtime_repo" rev-parse FETCH_HEAD)"
if [[ "$runtime_remote_head" != "$GITHUB_SHA" ]]; then
  echo "The durable runtime fetch did not resolve to the workflow head." >&2
  exit 1
fi
git -C "$runtime_repo" checkout main
git -C "$runtime_repo" merge --ff-only "$runtime_remote_head"

PYTHONPATH="$GITHUB_WORKSPACE/worker_orchestrator/src" \
  "$python_bin" -m unittest discover -s "$GITHUB_WORKSPACE/worker_orchestrator/tests" -q
"$python_bin" -m compileall -q \
  "$GITHUB_WORKSPACE/worker_orchestrator/src" \
  "$GITHUB_WORKSPACE/worker_orchestrator/tests"
cmp -s \
  "$GITHUB_WORKSPACE/worker_orchestrator/src/worker_orchestrator/control_plane.py" \
  "$runtime_repo/worker_orchestrator/src/worker_orchestrator/control_plane.py"
cmp -s \
  "$GITHUB_WORKSPACE/worker_orchestrator/src/worker_orchestrator/goals.py" \
  "$runtime_repo/worker_orchestrator/src/worker_orchestrator/goals.py"

config_dir="$HOME/.config/worker-orchestrator"
dropin_dir="$HOME/.config/systemd/user/$service_name.d"
mkdir -p "$config_dir" "$dropin_dir"

routes_source="$GITHUB_WORKSPACE/worker_orchestrator/config/chat-routes.json"
routes_target="$config_dir/chat-routes.json"
install -m 600 "$routes_source" "$routes_target"

"$python_bin" - "$routes_target" "$config_dir/master-control.env" <<'PY'
import os
import sys
import tempfile

routes_file = sys.argv[1]
target = sys.argv[2]
payload = "\n".join([
    "MASTER_CONTROL_ENABLED=true",
    "MASTER_CONTROL_ISSUE=9",
    f"CHAT_ROUTES_FILE={routes_file}",
    "",
])
fd, temporary = tempfile.mkstemp(prefix="master-control.", dir=os.path.dirname(target), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY

dropin_tmp="$(mktemp "$dropin_dir/master-control.conf.XXXXXX")"
trap 'rm -f "$dropin_tmp"' EXIT
printf '%s\n' \
  '[Service]' \
  "EnvironmentFile=$config_dir/master-control.env" \
  "Environment=PYTHONPATH=$runtime_repo/worker_orchestrator/src" \
  >"$dropin_tmp"
chmod 600 "$dropin_tmp"
mv -f "$dropin_tmp" "$dropin_dir/master-control.conf"
trap - EXIT

runtime_start="$HOME/.local/share/worker-orchestrator/start.sh"
runtime_start_tmp="$(mktemp "$HOME/.local/share/worker-orchestrator/start.sh.XXXXXX")"
trap 'rm -f "$runtime_start_tmp"' EXIT
cp "$GITHUB_WORKSPACE/worker_orchestrator/scripts/start_dispatch_only.sh" "$runtime_start_tmp"
chmod 700 "$runtime_start_tmp"
mv -f "$runtime_start_tmp" "$runtime_start"
trap - EXIT

before_pid="$(systemctl --user show "$service_name" --property=MainPID --value)"
systemctl --user daemon-reload
systemctl --user restart "$service_name"

for _ in $(seq 1 30); do
  if systemctl --user is-active --quiet "$service_name"; then
    break
  fi
  sleep 1
done
systemctl --user is-active --quiet "$service_name"
systemctl --user is-enabled --quiet "$service_name"

after_pid="$(systemctl --user show "$service_name" --property=MainPID --value)"
if [[ -z "$after_pid" || "$after_pid" == "0" || "$after_pid" == "$before_pid" ]]; then
  echo "The controlled restart did not produce a new active MainPID." >&2
  exit 1
fi

version="$($python_bin - "$runtime_repo/worker_orchestrator/pyproject.toml" <<'PY'
import sys
import tomllib
with open(sys.argv[1], "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
PY
)"
if [[ "$version" != "0.2.0" ]]; then
  echo "Unexpected installed runner-worker-orchestrator version: $version" >&2
  exit 1
fi

"$python_bin" - "$after_pid" "$runtime_repo" <<'PY'
import json
import os
import sqlite3
import sys

pid = sys.argv[1]
runtime_repo = sys.argv[2]
raw = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
environment = {}
for entry in raw:
    if b"=" in entry:
        key, value = entry.split(b"=", 1)
        environment[key.decode(errors="replace")] = value.decode(errors="replace")
if environment.get("MASTER_CONTROL_ENABLED") != "true":
    raise SystemExit("MASTER_CONTROL_ENABLED is not effective")
if environment.get("MASTER_CONTROL_ISSUE") != "9":
    raise SystemExit("MASTER_CONTROL_ISSUE is not effective")
if environment.get("PYTHONPATH") != os.path.join(runtime_repo, "worker_orchestrator", "src"):
    raise SystemExit("The durable runtime source is not effective on PYTHONPATH")
if environment.get("WORKER_COMMAND"):
    raise SystemExit("WORKER_COMMAND must be absent in dispatch-only production")
routes_file = environment.get("CHAT_ROUTES_FILE", "")
if not routes_file or not os.path.isfile(routes_file):
    raise SystemExit("CHAT_ROUTES_FILE is not effective")
with open(routes_file, encoding="utf-8") as handle:
    routes = json.load(handle)
if len(routes) != 18:
    raise SystemExit(f"Unexpected chat route count: {len(routes)}")
required_routes = {
    "Projekt: HA Simulation → Chat: HA Testumgebung planen",
    "Projekt: Drucker → Chat: Brother Companion planen",
    "Projekt: Dashboards → Chat: Fire TV Medienkarte erweitern",
    "Projekt: Mähroboter → Chat: Status Mähroboter Read only",
    "Projekt: Health → Chat: Schlüsselinventur planen",
    "Projekt: IOS App → Chat: iOS Admin Chat Status",
    "Projekt: Run optimization → Chat: Stand zusammenfassen",
}
if not required_routes.issubset(routes):
    raise SystemExit("Required visible chat routes are missing")
if any(item.get("transport") != "chat_relay" for item in routes.values()):
    raise SystemExit("Production chat routes must use chat_relay")

cmdline = [x.decode(errors="replace") for x in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if x]
if "--allow-non-dry-run" in cmdline:
    raise SystemExit("Dispatch-only runtime must not use --allow-non-dry-run")
db = environment.get("ORCHESTRATOR_DB", "")
if not db and "--db" in cmdline:
    db = cmdline[cmdline.index("--db") + 1]
if not db:
    db = os.path.join(os.readlink(f"/proc/{pid}/cwd"), "worker-orchestrator.sqlite3")
elif not os.path.isabs(db):
    db = os.path.join(os.readlink(f"/proc/{pid}/cwd"), db)
connection = sqlite3.connect(db)
tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
required = {"workers", "events", "locks", "master_requests", "master_children", "master_audit", "master_meta"}
missing = required - tables
if missing:
    raise SystemExit("Missing runtime database tables: " + ", ".join(sorted(missing)))
worker_count = connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
if worker_count < 1:
    raise SystemExit("Existing worker registry was not preserved")
print(json.dumps({"database_tables_verified": sorted(required), "preserved_worker_count": worker_count}))
PY

wake_service() {
  "$python_bin" - "$after_pid" <<'PY'
import hashlib
import hmac
import os
import sys
import time
import urllib.error
import urllib.request

pid = sys.argv[1]
raw = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
environment = {}
for entry in raw:
    if b"=" in entry:
        key, value = entry.split(b"=", 1)
        environment[key.decode(errors="replace")] = value.decode(errors="replace")
body = b"{}"
headers = {"X-GitHub-Event": "issue_comment", "Content-Type": "application/json"}
secret = environment.get("GITHUB_WEBHOOK_SECRET", "")
if secret:
    headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
request = urllib.request.Request("http://127.0.0.1:8787", data=body, headers=headers, method="POST")
for attempt in range(30):
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 202:
                raise SystemExit(f"Unexpected wake response: {response.status}")
            break
    except urllib.error.URLError:
        if attempt == 29:
            raise
        time.sleep(1)
PY
}

wake_service
sleep 5
wake_service
sleep 10

systemctl --user is-active --quiet "$service_name"
restarts="$(systemctl --user show "$service_name" --property=NRestarts --value)"
if [[ ! "$restarts" =~ ^[0-9]+$ ]]; then
  echo "Unable to verify NRestarts." >&2
  exit 1
fi
if journalctl --user -u "$service_name" --since "5 minutes ago" --no-pager \
  | grep -Eiq 'Traceback|unhandled exception'; then
  echo "Recent service journal contains an unhandled failure." >&2
  exit 1
fi

echo "Master control plane activation verified: source=$source_head version=$version pid=$after_pid nrestarts=$restarts"
