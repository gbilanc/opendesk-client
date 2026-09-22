#!/bin/bash
# macOS equivalent: launchd plist
# Per Windows: scdump
# Per Linux: systemd

LOG_FILE="/tmp/opendesk-host-macos.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting OpenDesk Host (macOS)" >> "$LOG_FILE"

# Avvia in background con nohup
nohup /usr/bin/python3 /home/giampaolo/Codium/opendesk-client/opendesk-host --log-level=WARNING >> "$LOG_FILE" 2>&1 &

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Opendesk Host started with PID $!" >> "$LOG_FILE"

exit 0
