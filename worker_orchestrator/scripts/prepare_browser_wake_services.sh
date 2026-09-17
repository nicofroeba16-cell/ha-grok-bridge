#!/usr/bin/env bash
set -euo pipefail

home_dir="${HOME:?HOME is required}"
unit_src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../systemd" && pwd)"
unit_dst="$home_dir/.config/systemd/user"
runtime_dir="$home_dir/.config/worker-orchestrator-browser-wake"
runtime_env="$runtime_dir/runtime.env"
current="$home_dir/.local/share/browser-wake/current"

if [[ ! -L "$current" || ! -e "$current/worker_orchestrator/browser_chatgpt_send.mjs" ]]; then
  echo "browser-wake current release is missing or invalid: $current" >&2
  exit 66
fi
if [[ ! -f "$runtime_env" ]]; then
  echo "runtime env is missing: $runtime_env" >&2
  exit 66
fi

install -d -m 0700 "$runtime_dir"
install -d -m 0755 "$unit_dst"
for unit in browser-wake-chrome.service browser-wake.service master-autonomous-orchestration.target; do
  install -m 0644 "$unit_src/$unit" "$unit_dst/$unit"
done

python3 - "$runtime_env" "$home_dir" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
home = sys.argv[2]
updates = {
    'BROWSER_WAKE_COMMAND': f'node {home}/.local/share/browser-wake/current/worker_orchestrator/browser_chatgpt_send.mjs',
    'CHATGPT_PROFILE_DIR': f'{home}/.local/share/browser-wake/chrome-profile-web-v2',
    'CHATGPT_CHROME_BIN': '/usr/bin/google-chrome',
    'CHATGPT_BROWSER_HEADLESS': 'false',
    'CHATGPT_BROWSER_URL': 'http://127.0.0.1:9224',
    'PYTHONPATH': f'{home}/.local/share/browser-wake/current/worker_orchestrator/src',
}
lines = path.read_text(encoding='utf-8').splitlines()
seen = set()
out = []
for line in lines:
    if '=' in line and not line.lstrip().startswith('#'):
        key = line.split('=', 1)[0]
        if key in updates:
            out.append(f'{key}={updates[key]}')
            seen.add(key)
            continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f'{key}={value}')
path.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
path.chmod(0o600)
PY

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
systemctl --user daemon-reload
systemd-analyze --user verify \
  "$unit_dst/browser-wake-chrome.service" \
  "$unit_dst/browser-wake.service" \
  "$unit_dst/master-autonomous-orchestration.target"

for unit in browser-wake-chrome.service browser-wake.service master-autonomous-orchestration.target; do
  state="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
  if [[ "$state" == "enabled" ]]; then
    echo "refusing prepared state: $unit is already enabled" >&2
    exit 73
  fi
done

echo "PREPARED_NOT_ACTIVATED"
