# OpenDesk

Multi-platform remote desktop application (TeamViewer / AnyDesk-like).

- **Platforms:** Windows, macOS, Linux
- **Tech:** Python 3.12+, PySide6 (Qt6), WebRTC, E2E encryption
- **Network:** P2P via WebRTC with relay fallback
- **Features:** Screen sharing, remote control, file transfer, clipboard sync,
  audio, chat, multi-monitor

## Quick start (uv — recommended)

```bash
# Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and run
cd opendesk
uv sync          # creates venv + installs deps
uv run opendesk  # start the app
```

## Alternative (pip)

```bash
pip install -e .
opendesk
```

## Development

```bash
uv sync --dev          # install with dev dependencies
uv run pytest          # run tests (92 tests)
uv run pytest -v       # verbose
uv run black .         # format code
uv run ruff check .    # lint
uv run mypy opendesk/  # type check
```

### Optional features

```bash
# Wayland support (Linux)
uv sync --extra wayland

# Audio streaming
uv sync --extra audio

# macOS input backend
uv sync --extra macos
```

### Wayland setup (Linux)

Wayland requires both **Python packages** and **system packages**:

```bash
# 1. Python dependencies
uv sync --extra wayland      # installs dbus-next, evdev

# 2. System packages (Ubuntu/Debian)
sudo apt install gstreamer1.0-pipewire python3-gi       \
                 xdg-desktop-portal pipewire

# Optional: accurate absolute mouse positioning
sudo apt install ydotool

# Required: uinput permissions for remote input
sudo usermod -aG input $USER
# (log out and back in)
```

**Supported backends** (auto-detected in order):

| Backend | Capture | Input | Notes |
|---------|---------|-------|-------|
| **PORTAL** | D-Bus + GStreamer | — | Reuses portal session, no double dialog |
| **PIPEWIRE** | GStreamer pipewiresrc | — | Shows its own screen-selection dialog |
| **MSS** | X11 | X11 (Xlib) | Fallback via XWayland |
| **uinput** | — | evdev uinput | Requires `input` group |
| **ydotool** | — | ydotool | Absolute mouse on Wayland |

## Architecture

```
opendesk/
├── opendesk/          # Main application (37 files, ~11.5k LOC)
│   ├── core/          # Screen capture, input, codec, audio, recording
│   ├── network/       # Protocol, P2P (aiortc), relay, NAT traversal
│   ├── crypto/        # E2E encryption (NaCl Box), Argon2 auth
│   ├── ui/            # PySide6 widgets + QSS themes (light/dark)
│   └── utils/         # Logging, platform detection
├── tests/             # 92 tests — unit, integration, edge cases
└── uv.lock            # Locked dependencies (53 packages)
```

## Commands

```bash
uv run opendesk  # Start the remote desktop client
uv run pytest    # Run all tests
```

## Relay server

Il relay server è ora un'app standalone separata in **`../opendesk-relay`** (o
[github.com/opendesk/opendesk-relay](https://github.com/opendesk/opendesk-relay)).

Documentazione completa e istruzioni nel README del progetto relay:

```bash
cd ../opendesk-relay
cat README.md
```

### Avvio rapido

```bash
cd ../opendesk-relay
uv sync
uv run relay-server --port 8474
```

### Installazione come servizio systemd (Linux)

```bash
sudo ./opendesk-relay/install-relay.sh --port 8474
# (dalla directory opendesk, o esegui dal progetto opendesk-relay)
```

### Configurazione client OpenDesk

Nelle impostazioni del client OpenDesk (Tools → Settings → Network), imposta:

| Campo | Valore |
|-------|--------|
| **Relay Host** | IP pubblico del server |
| **Relay Port** | 8474 (o la porta configurata) |
| **Enable relay** | ✅ |
