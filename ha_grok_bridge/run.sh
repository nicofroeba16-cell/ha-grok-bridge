#!/bin/bash
set -euo pipefail
log(){ echo "[file-bridge] $*"; }
VERSION="1.10"
KEY_SOURCE="/ssl/ha-file-sync-bridge-2026"
KEY_DEST="/data/ssh/id_ed25519"
APP_DIR="/opt/ha-file-sync-bridge"
log "HA File Sync Bridge Add-on $VERSION"
[[ -f "$KEY_SOURCE" ]] || { log "FATAL SSH-Key fehlt: $KEY_SOURCE"; exit 1; }
[[ -f "$APP_DIR/file_bridge.py" ]] || { log "FATAL file_bridge.py fehlt: $APP_DIR/file_bridge.py"; exit 1; }
mkdir -p /data/ssh /data/.ssh
cp -f "$KEY_SOURCE" "$KEY_DEST"
chmod 600 "$KEY_DEST"
chmod 700 /data/ssh /data/.ssh
cp -f "$APP_DIR/known_hosts" /data/.ssh/known_hosts
chmod 600 /data/.ssh/known_hosts
export GIT_SSH_COMMAND="ssh -i $KEY_DEST -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/data/.ssh/known_hosts"
log "SSH-Key verfügbar: $KEY_SOURCE"
log "Verwende passphraselosen SSH-Key"

# Inject the read-only AI file API into the existing bridge at startup.
# This keeps the established bridge implementation intact while adding:
#   GET /read?path=<relative/path>
# The same safe_config_path()/exclude rules used by /write are enforced.
python3 - "$APP_DIR/file_bridge.py" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
marker = '            elif parsed.path == "/state":\n'
branch = '''            elif parsed.path == "/read":
                raw_path = query.get("path", [""])[0]
                target = safe_config_path(raw_path, c)
                if not target.exists():
                    raise ValueError("file not found")
                if not target.is_file():
                    raise ValueError("path is not a file")
                data = target.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                requested = query.get("encoding", [""])[0].lower()
                try:
                    decoded = data.decode("utf-8")
                    if requested != "base64":
                        self._json(200, {"ok": True, "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": digest, "encoding": "utf-8", "content": decoded})
                    else:
                        self._json(200, {"ok": True, "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": digest, "encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii")})
                except UnicodeDecodeError:
                    self._json(200, {"ok": True, "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": digest, "encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii")})
'''
if 'elif parsed.path == "/read":' not in text:
    if marker not in text:
        raise SystemExit("/read injection marker not found")
    text = text.replace(marker, branch + marker, 1)
    path.write_text(text, encoding="utf-8")
PY

log "KI file read API enabled: GET /read?path=<relative/path>"
log "Starte file_bridge.py ..."
exec python3 "$APP_DIR/file_bridge.py"
