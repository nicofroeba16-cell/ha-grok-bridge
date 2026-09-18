#!/usr/bin/env bash
set -euo pipefail

chrome_bin="${CHATGPT_CHROME_BIN:-/usr/bin/google-chrome}"
profile_dir="${CHATGPT_PROFILE_DIR:?CHATGPT_PROFILE_DIR is required}"
browser_url="${CHATGPT_BROWSER_URL:-http://127.0.0.1:9224}"
ready_timeout="${BROWSER_DISPLAY_READY_TIMEOUT_SECONDS:-180}"
ready_poll="${BROWSER_DISPLAY_READY_POLL_SECONDS:-2}"

if [[ "$browser_url" != "http://127.0.0.1:9224" ]]; then
  echo "refusing non-canonical browser endpoint: expected http://127.0.0.1:9224" >&2
  exit 64
fi
if [[ ! -x "$chrome_bin" ]]; then
  echo "Chrome executable not found: $chrome_bin" >&2
  exit 66
fi
if ss -ltn | grep -qE '127[.]0[.]0[.]1:9224[[:space:]]'; then
  echo "browser endpoint 127.0.0.1:9224 is already in use; refusing to replace an existing browser" >&2
  exit 75
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
export DISPLAY="${DISPLAY:-:0}"

refresh_xauthority() {
  if [[ -n "${XAUTHORITY:-}" && -r "$XAUTHORITY" ]]; then return 0; fi
  local candidate
  candidate="$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -type f -name '.mutter-Xwaylandauth.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true)"
  if [[ -n "$candidate" ]]; then export XAUTHORITY="$candidate"; fi
}

display_ready() {
  refresh_xauthority
  if command -v xset >/dev/null 2>&1; then
    xset -display "$DISPLAY" q >/dev/null 2>&1 && return 0
  fi
  if command -v xdpyinfo >/dev/null 2>&1; then
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && return 0
  fi
  return 1
}

start_epoch="$(date +%s)"
echo "DISPLAY_READINESS_WAIT display=$DISPLAY timeout=${ready_timeout}s" >&2
while ! display_ready; do
  now_epoch="$(date +%s)"
  if (( now_epoch - start_epoch >= ready_timeout )); then
    echo "BLOCKED_DISPLAY_NOT_READY display=$DISPLAY waited=${ready_timeout}s" >&2
    exit 75
  fi
  sleep "$ready_poll"
done

echo "DISPLAY_READY display=$DISPLAY" >&2
if [[ "${BROWSER_WAKE_CHROME_READINESS_ONLY:-0}" == "1" ]]; then
  echo "DISPLAY_READY readiness_only=1"
  exit 0
fi

install -d -m 0700 "$profile_dir"
exec "$chrome_bin" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9224 \
  --user-data-dir="$profile_dir" \
  --no-first-run \
  --disable-sync \
  https://chatgpt.com/
