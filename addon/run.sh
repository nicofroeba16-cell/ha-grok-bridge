#!/bin/bash
# 0.5.1 – structured Git file synchronization
set -euo pipefail

log() { echo "[file-bridge] $*"; }

VERSION="0.5.1"
KEY_SOURCE="/ssl/ha-grok-bridge"
KEY_DEST="/data/ssh/id_ed25519"

log "HA File Sync Bridge Add-on $VERSION"

if [[ ! -f "$KEY_SOURCE" ]]; then
  log "FATAL SSH-Key fehlt: $KEY_SOURCE"
  exit 1
fi

mkdir -p /data/ssh /data/.ssh
cp -f "$KEY_SOURCE" "$KEY_DEST"
chmod 600 "$KEY_DEST"
chmod 700 /data/ssh /data/.ssh

ssh-keyscan -t rsa,ecdsa,ed25519 github.com > /data/.ssh/known_hosts 2>/dev/null || true
chmod 600 /data/.ssh/known_hosts

export GIT_SSH_COMMAND="ssh -i $KEY_DEST -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/data/.ssh/known_hosts -o BatchMode=yes"

log "SSH-Key verfügbar: $KEY_SOURCE"
log "Starte file_bridge.py ..."
exec python3 /opt/ha-file-sync-bridge/file_bridge.py
