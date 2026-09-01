#!/bin/sh
set -eu
KEY_SOURCE=/ssl/ha-grok-bridge-new
KEY_DEST=/data/ssh/id_ed25519
if [ ! -f "$KEY_SOURCE" ]; then echo "[file-bridge] FATAL: SSH key missing at $KEY_SOURCE"; exit 1; fi
mkdir -p /data/ssh /data/.ssh
cp "$KEY_SOURCE" "$KEY_DEST"
chmod 600 "$KEY_DEST"
cp /opt/ha-file-sync-bridge/known_hosts /data/.ssh/known_hosts
chmod 600 /data/.ssh/known_hosts
export GIT_SSH_COMMAND="ssh -i $KEY_DEST -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/data/.ssh/known_hosts"
exec python3 /opt/ha-file-sync-bridge/file_bridge.py
