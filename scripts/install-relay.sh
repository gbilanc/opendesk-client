#!/usr/bin/env bash
#
# Install OpenDesk Relay Server as a systemd service (Linux).
#
# Usage:
#   sudo ./scripts/install-relay.sh              # install from project dir
#   sudo ./scripts/install-relay.sh --port 9443   # custom port
#
# Prerequisites:
#   - Python 3.12+ with uv or pip
#   - systemd (Linux only)
#   - ffmpeg (optional, for video encoding)
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="opendesk-relay"
SERVICE_FILE="$SCRIPT_DIR/opendesk-relay.service"
SYSTEMD_DIR="/etc/systemd/system"
PORT="${PORT:-8474}"

# Parse --port argument
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --help) echo "Usage: $0 [--port PORT]"; exit 0 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

echo "============================================"
echo " OpenDesk Relay Server Installer"
echo "============================================"
echo "Project : $PROJECT_DIR"
echo "Port    : $PORT"
echo ""

# ── Check systemd ──
if ! command -v systemctl &>/dev/null; then
    echo "❌ systemd not found — this script is for Linux with systemd only."
    echo "   On other platforms, run: uv run opendesk-relay --port $PORT"
    exit 1
fi

# ── Check uv or pip ──
if command -v uv &>/dev/null; then
    PYTHON_CMD="uv run --directory $PROJECT_DIR opendesk-relay"
    echo "✅ Using uv"
elif command -v python3 &>/dev/null; then
    PYTHON_CMD="python3 -m relay_server.server"
    echo "✅ Using python3"
else
    echo "❌ Neither uv nor python3 found."
    exit 1
fi

# ── Install dependencies ──
if command -v uv &>/dev/null; then
    echo "→ Installing dependencies with uv..."
    cd "$PROJECT_DIR"
    uv sync --frozen
else
    echo "→ Installing dependencies with pip..."
    pip install -e "$PROJECT_DIR"
fi

# ── Create config directory ──
mkdir -p "$HOME/.opendesk"
echo "✅ Config dir: $HOME/.opendesk"

# ── Create systemd service ──
if [ ! -f "$SERVICE_FILE" ]; then
    echo "❌ Service file not found: $SERVICE_FILE"
    exit 1
fi

# Read the service template and update paths
sed -e "s|ExecStart=.*$|ExecStart=$PYTHON_CMD --port $PORT|" \
    -e "s|User=.*|User=$(whoami)|" \
    -e "s|Group=.*|Group=$(id -gn)|" \
    "$SERVICE_FILE" > "/tmp/$SERVICE_NAME.service"

sudo cp "/tmp/$SERVICE_NAME.service" "$SYSTEMD_DIR/$SERVICE_NAME.service"
rm "/tmp/$SERVICE_NAME.service"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "✅ Service installed: $SERVICE_NAME"
echo ""
echo "   Start:  sudo systemctl start $SERVICE_NAME"
echo "   Stop:   sudo systemctl stop $SERVICE_NAME"
echo "   Status: sudo systemctl status $SERVICE_NAME"
echo "   Logs:   sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "   Relay will listen on port $PORT"
echo "   Configure clients to use: --relay-host YOUR_IP --relay-port $PORT"
