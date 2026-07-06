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

## Architecture

```
opendesk/
├── opendesk/          # Main application (37 files, ~11.5k LOC)
│   ├── core/          # Screen capture, input, codec, audio, recording
│   ├── network/       # Protocol, P2P (aiortc), relay, NAT traversal
│   ├── crypto/        # E2E encryption (NaCl Box), Argon2 auth
│   ├── ui/            # PySide6 widgets + QSS themes (light/dark)
│   └── utils/         # Logging, platform detection
├── relay_server/      # Standalone TCP relay server
├── tests/             # 92 tests — unit, integration, edge cases
└── uv.lock            # Locked dependencies (53 packages)
```

## Commands

```bash
uv run opendesk            # Start the remote desktop client
uv run opendesk-relay --port 8474  # Start relay server
uv run pytest              # Run all tests
```

## Relay server

Il relay server permette la connessione quando P2P diretto (WebRTC) non è possibile
(NAT simmetrico, firewall restrittivi).

### Avvio rapido

```bash
# Con uv (raccomandato)
uv run opendesk-relay --port 8474

# Con pip
opendesk-relay --port 8474

# O direttamente
python3 -m relay_server.server --port 8474
```

### Installazione come servizio systemd (Linux)

```bash
# Installa come servizio di sistema
sudo ./scripts/install-relay.sh --port 8474

# Avvia
sudo systemctl start opendesk-relay

# Logs in tempo reale
sudo journalctl -u opendesk-relay -f

# Riavvia dopo aggiornamenti
sudo systemctl restart opendesk-relay
```

### Configurazione client

Nelle impostazioni del client OpenDesk (Tools → Settings → Network), imposta:

| Campo | Valore |
|-------|--------|
| **Relay Host** | IP pubblico del server |
| **Relay Port** | 8474 (o la porta configurata) |
| **Enable relay** | ✅ |

### Opzioni CLI

```
opendesk-relay --help
opendesk-relay --host 0.0.0.0 --port 8474 --debug
```

| Opzione | Default | Descrizione |
|---------|---------|-------------|
| `--host` | `0.0.0.0` | Indirizzo su cui ascoltare |
| `--port` | `8474` | Porta TCP |
| `--debug` | off | Logging dettagliato |
