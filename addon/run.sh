#!/bin/bash
# 0.2.7 — kein s6/with-contenv (init: false).
set -euo pipefail

log() { echo "[bridge] $*" ; }

log "HA Grok Bridge Add-on 0.2.7 — nur Git, keine API"

mkdir -p /data/ssh /data/.ssh
chmod 700 /data/ssh /data/.ssh

KEY=""
if [[ -f /ssl/ha-grok-bridge ]]; then
  KEY=/ssl/ha-grok-bridge
elif [[ -f /data/ssh/id_ed25519 ]]; then
  KEY=/data/ssh/id_ed25519
elif [[ -f /data/ssh/id_rsa ]]; then
  KEY=/data/ssh/id_rsa
fi

if [[ -z "$KEY" ]]; then
  log "WARN kein Git-SSH-Key"
else
  chmod 600 "$KEY" || true
  log "Git-SSH-Key: $KEY"
  export GIT_SSH_COMMAND="ssh -i ${KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o UserKnownHostsFile=/data/.ssh/known_hosts"
fi

ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> /data/.ssh/known_hosts 2>/dev/null || true
chmod 600 /data/.ssh/known_hosts || true

if [[ ! -d /config && -d /homeassistant ]]; then
  ln -sfn /homeassistant /config
  log "WARN /config Link -> /homeassistant"
fi
if [[ ! -d /config ]]; then
  log "FATAL /config nicht gemappt"
  ls -la / || true
  exit 1
fi
log "/config ok"

export HOME=/data
export BRIDGE_HOME=/data
export GIT_AUTHOR_NAME="ha-grok-bridge"
export GIT_AUTHOR_EMAIL="bridge@local"
export GIT_COMMITTER_NAME="ha-grok-bridge"
export GIT_COMMITTER_EMAIL="bridge@local"

exec python3 /opt/ha-grok-bridge/cloud_poll.py
