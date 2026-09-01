#!/bin/bash
set -euo pipefail
log(){ echo "[file-bridge] $*"; }
VERSION="0.5.4"
KEY_SOURCE="/ssl/ha-grok-bridge"
KEY_DEST="/data/ssh/id_ed25519"
APP_DIR="/opt/ha-file-sync-bridge"
PASSPHRASE_FILE="/config/.ssh/ha-grok-bridge.passphrase"
log "HA File Sync Bridge Add-on $VERSION"
[[ -f "$KEY_SOURCE" ]] || { log "FATAL SSH-Key fehlt: $KEY_SOURCE"; exit 1; }
mkdir -p /data/ssh /data/.ssh
cp -f "$KEY_SOURCE" "$KEY_DEST"
chmod 600 "$KEY_DEST"
chmod 700 /data/ssh /data/.ssh
cp -f "$APP_DIR/known_hosts" /data/.ssh/known_hosts
chmod 600 /data/.ssh/known_hosts
if [[ -f "$PASSPHRASE_FILE" ]]; then
  chmod 600 "$PASSPHRASE_FILE"
  ASKPASS="/data/ssh/askpass.sh"
  printf '#!/bin/sh\ncat %q\n' "$PASSPHRASE_FILE" > "$ASKPASS"
  chmod 700 "$ASKPASS"
  export SSH_ASKPASS="$ASKPASS"
  export SSH_ASKPASS_REQUIRE=force
  export DISPLAY=:0
  log "SSH-Key ist passphrase-geschützt; SSH_ASKPASS aktiviert"
else
  log "SSH-Key ohne Passphrase-Datei; direkter SSH-Test wird verwendet"
fi
export GIT_SSH_COMMAND="ssh -i $KEY_DEST -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/data/.ssh/known_hosts"
log "SSH-Key verfügbar: $KEY_SOURCE"
log "Starte file_bridge.py ..."
exec python3 "$APP_DIR/file_bridge.py"
