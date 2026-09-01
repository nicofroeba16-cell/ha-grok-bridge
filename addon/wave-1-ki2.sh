#!/bin/bash
# Welle 1 Patch — nur ki2. Erst nach Freigabe „OK ki2 stop“ ausfuehren.
# Macht: Poller stop+disable. Startet das HA-Add-on NICHT.
set -euo pipefail

if [[ ! -d /home/vboxuser/grok-agent ]]; then
  echo "ABBRUCH: kein ki2 (fehlt ~/grok-agent)."
  exit 1
fi
if [[ -d /addons || -d /config ]]; then
  echo "ABBRUCH: sieht nach HA-Box aus. Script nur auf ki2."
  exit 1
fi

echo "WELLE1 stop ha-grok-bridge.service"
systemctl stop ha-grok-bridge.service
systemctl disable ha-grok-bridge.service
echo -n "WELLE1 active="
systemctl is-active ha-grok-bridge.service || true
echo
echo "WELLE1 danach SOFORT auf HA: Add-on HA Grok Bridge Start."
echo "Log erwarten: first boot: arm last_id=... skip exec"
echo "Rollback: systemctl enable --now ha-grok-bridge.service"
