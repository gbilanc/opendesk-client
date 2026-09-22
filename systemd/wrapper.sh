#!/bin/bash
# wrapper.sh — wrapper per avviare opendesk-host in background per systemd
# Per Windows: usare scdump / Start Menu
# Per macOS: usare launchd

LOG_FILE="/tmp/opendesk-host.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting OpenDesk Host" >> "$LOG_FILE"

# Avvia in background con nohup
nohup /usr/bin/python3 /home/giampaolo/Codium/opendesk-client/opendesk-host --log-level=WARNING >> "$LOG_FILE" 2>&1 &

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Opendesk Host started with PID $!" >> "$LOG_FILE"

exit 0
