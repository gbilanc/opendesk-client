# OpenDesk — Installazione

## 💻 Installazione (utenti finali e sviluppatori)

### Requisiti

- **Python 3.12+**
- **Git**
- **[uv](https://docs.astral.sh/uv/)** (gestore pacchetti)

### Installazione rapida

```bash
git clone https://github.com/opendesk/opendesk-client
cd opendesk-client
uv sync
uv run opendesk
```

Per avviare la modalità **host** (solo su invito):

```bash
uv run opendesk-host
```

> **Nota:** `uv sync` crea automaticamente un ambiente virtuale isolato e installa
> tutte le dipendenze necessarie. Non serve installare Python globalmente.

### Aggiornamento

```bash
cd opendesk-client
git pull
uv sync
```

### Disinstallazione

```bash
rm -rf opendesk-client
```

### Dipendenze di sistema

Alcune funzionalità richiedono dipendenze di sistema. Per verificarle:

```bash
uv run opendesk --install-system-deps
```

#### Linux (X11)

```bash
sudo apt-get install -y \
    ffmpeg \
    libx11-dev \
    libxext-dev \
    libxrender-dev \
    libxtst-dev
```

#### Linux (Wayland)

```bash
sudo apt-get install -y \
    ffmpeg \
    pipewire \
    pipewire-audio \
    wireplumber
```

#### Linux (entrambi)

```bash
sudo apt-get install -y \
    ffmpeg \
    libx11-dev \
    libxext-dev \
    libxrender-dev \
    libxtst-dev \
    pipewire \
    pipewire-audio \
    wireplumber
```

#### Windows

- **ffmpeg** — Scarica da [ffmpeg.org](https://ffmpeg.org/download.html) e aggiungi al PATH

#### macOS

```bash
brew install ffmpeg
```

## ⚙️ Sviluppo

```bash
git clone https://github.com/opendesk/opendesk-client
cd opendesk-client
uv sync --extra dev
uv run opendesk
```

Con dipendenze audio:

```bash
uv sync --extra audio
```

### Eseguire i test

```bash
uv run pytest tests/ -v
```

### Formattazione e lint

```bash
uv run black opendesk/ tests/
uv run ruff check opendesk/ tests/
uv run mypy opendesk/
```

## 📁 Struttura

```
opendesk-client/
├── opendesk/              ← Codice sorgente principale
│   ├── app.py             ← Entry point client
│   ├── host_app.py        ← Entry point host
│   ├── core/              ← Screen, audio, camera, input, file transfer
│   ├── crypto/            ← E2E encryption, autenticazione
│   ├── network/           ← Protocollo, relay, NAT traversal
│   ├── services/          ← Connessione, stream, pipeline
│   ├── ui/                ← Interfaccia Qt (main_window, viewer, chat, ...)
│   └── utils/             ← Logger, utility di piattaforma
├── tests/                 ← Test
├── docs/                  ← Documentazione
├── pyproject.toml         ← Configurazione progetto
├── uv.lock                ← Lock file dipendenze
└── README.md
```
