#!/bin/bash
set -euo pipefail
log(){ echo "[file-bridge] $*"; }
VERSION="1.13"
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
log "Verwende passphrasenlosen SSH-Key"
log "KI file API: GET /read?path=<relative/path>"
log "AI control channel: .ai-control/commands -> .ai-control/results"
log "Starte file_bridge.py ..."
exec python3 "$APP_DIR/file_bridge.py"
