#!/bin/bash
# 0.5.0 – structured Git file synchronization
set -euo pipefail

log() { echo "[file-bridge] $*"; }

VERSION="0.5.0"
KEY_SOURCE="/ssl/ha-grok-bridge-new"
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

# Same GitHub host-key setup as the working 0.3.x release.
ssh-keyscan -t rsa,ecdsa,ed25519 github.com > /data/.ssh/known_hosts 2>/dev/null || true
chmod 600 /data/.ssh/known_hosts

export GIT_SSH_COMMAND="ssh -i $KEY_DEST -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/data/.ssh/known_hosts -o BatchMode=yes"

if [[ ! -d /config && -d /homeassistant ]]; then
  ln -sfn /homeassistant /config
fi

if [[ ! -d /config ]]; then
  log "FATAL /config nicht gemappt"
  exit 1
fi

log "/config ok"
log "Git-SSH-Key: $KEY_DEST (von $KEY_SOURCE)"
log "Starte file_bridge.py ..."
exec python3 /opt/ha-file-sync-bridge/file_bridge.py
