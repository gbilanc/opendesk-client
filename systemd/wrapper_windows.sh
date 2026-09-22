#!/bin/bash
# Windows equivalent: scdump / Start Menu shortcut
# Per macOS: launchd equivalent

set -e

LOG_FILE="/tmp/opendesk-host-windows.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting OpenDesk Host (Windows)" >> "$LOG_FILE"

# Avvia in background con nohup (Windows: use powershell)
nohup "C:\Users\<username>\AppData\Local\Programs\Python\Python\312\python.exe" /home/giampaolo/Codium/opendesk-client/opendesk-host --log-level=WARNING >> "$LOG_FILE" 2>&1 &

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Opendesk Host started with PID $!" >> "$LOG_FILE"

exit 0
