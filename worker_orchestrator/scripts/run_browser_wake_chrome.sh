#!/usr/bin/env bash
set -euo pipefail

chrome_bin="${CHATGPT_CHROME_BIN:-/usr/bin/google-chrome}"
profile_dir="${CHATGPT_PROFILE_DIR:?CHATGPT_PROFILE_DIR is required}"
browser_url="${CHATGPT_BROWSER_URL:-http://127.0.0.1:9224}"

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
if [[ -z "${XAUTHORITY:-}" ]]; then
  xauth_candidate="$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -type f -name '.mutter-Xwaylandauth.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true)"
  if [[ -n "$xauth_candidate" ]]; then export XAUTHORITY="$xauth_candidate"; fi
fi

install -d -m 0700 "$profile_dir"
exec "$chrome_bin" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9224 \
  --user-data-dir="$profile_dir" \
  --no-first-run \
  --disable-sync \
  https://chatgpt.com/
