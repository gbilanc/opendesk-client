# OpenDesk

<p align="center">
  <img src="opendesk/ui/resources/opendesk.svg" width="100" alt="OpenDesk">
</p>

[English](README.md) · **Italiano**

Applicazione di desktop remoto multipiattaforma (simile a TeamViewer / AnyDesk).

[![PyPI version](https://img.shields.io/pypi/v/opendesk?color=blue)](https://pypi.org/project/opendesk/)
[![Python versions](https://img.shields.io/pypi/pyversions/opendesk)](https://pypi.org/project/opendesk/)
[![License](https://img.shields.io/pypi/l/opendesk?color=green)](https://github.com/opendesk/opendesk-client/blob/main/LICENSE)
[![CI](https://github.com/opendesk/opendesk-client/actions/workflows/ci.yml/badge.svg)](https://github.com/opendesk/opendesk-client/actions/workflows/ci.yml)

- **Piattaforme:** Windows, macOS, Linux
- **Tecnologie:** Python 3.12+, PySide6 (Qt6), PyAV (FFmpeg), crittografia E2E
- **Rete:** relay TCP con supporto P2P
- **Funzionalità:** condivisione schermo, controllo remoto, trasferimento file,
  sincronizzazione clipboard, streaming microfono, streaming webcam con overlay
  PiP, audio, chat, multi-monitor

---

## Installazione

### Installazione rapida (bootstrap)

```bash
# Linux / macOS
curl -fsSL https://opendesk.io/bootstrap.sh | bash
```

```powershell
# Windows (PowerShell come Amministratore)
iwr -useb https://opendesk.io/bootstrap.ps1 | iex
```

Lo script di bootstrap gestisce:
1. ✅ Installazione di Python 3.12+ (se mancante)
2. ✅ Dipendenze di sistema (ffmpeg, libxtst, pipewire, ecc.)
3. ✅ `pip install opendesk` (o `pipx install opendesk`)
4. ✅ Voce desktop / scorciatoia menu Start

### Tramite pip

```bash
pip install opendesk
```

> **Suggerimento:** usa [pipx](https://pipx.pypa.io/) per un'installazione
> isolata dell'app: `pipx install opendesk`

### Tramite uv (sviluppo)

```bash
git clone https://github.com/opendesk/opendesk-client
cd opendesk-client
uv sync
uv run opendesk
```

### Avvio rapido dal repository

Dopo `uv sync`, avvia il client con lo script incluso (usa il venv locale,
con fallback a `uv`):

```bash
./avvia.sh                # Linux / macOS
```

```bat
avvia.bat                 # Windows
```

Gli argomenti vengono inoltrati a `opendesk`, es. `./avvia.sh --log-level=WARNING`.

### Post-installazione: dipendenze di sistema

Dopo l'installazione, esegui questo comando per assicurarti che tutti i
pacchetti di sistema siano presenti:

```bash
opendesk --install-system-deps
```

## Utilizzo

```bash
opendesk                   # Avvia il client desktop remoto
opendesk --log-level=WARNING  # Log meno verboso
opendesk-host              # Avvia la versione solo-host (solo connessioni in ingresso)
opendesk --help            # Mostra tutte le opzioni
```

## OpenDesk Host (solo incoming)

Versione ridotta di OpenDesk che funge solo da **host** (nessuna connessione
in uscita).  Si connette al relay, mostra **Your ID** e **Password** in una
finestra compatta, e accetta connessioni in ingresso con streaming, input
remoto, chat e file transfer.

```bash
uv run opendesk-host                      # Avvia la versione host-only
uv run opendesk-host --minimized          # Avvia ridotto a icona nella system tray
uv run opendesk-host --log-level=WARNING
```

### Avvio automatico ridotto a icona

`--minimized` (alias `--tray`) avvia l'app nascosta nella **system tray**:
resta in esecuzione in background, mostra l'icona con il menu contestuale
(*Show window*, *New Session*, *Copy ID*, *Copy password*, *Quit*) e la
sessione continua anche se si chiude la finestra con la ✕.

Per avviare l'host automaticamente al login/boot del sistema (Linux):

```bash
# Metodo 1 — servizio systemd utente (parte al login grafico)
cp systemd/opendesk-host.user.service ~/.config/systemd/user/opendesk-host.service
systemctl --user daemon-reload
systemctl --user enable --now opendesk-host

# Metodo 2 — autostart XDG (creato automaticamente dall'installer)
# ~/.config/autostart/opendesk-host.desktop
```

Su Windows aggiungere un collegamento a `opendesk-host --minimized` nella
cartella *Esecuzione automatica*; su macOS usare un LaunchAgent.

### Cosa fa

- All'avvio si connette al relay e mostra ID + password
- Con `--minimized` parte direttamente nella **system tray** senza finestra
- Accetta solo connessioni **in ingresso** — nessun pannello dispositivi
- Quando un client remoto si connette: **streaming schermo**, **input remoto**
- **Chat** e **File transfer** si attivano automaticamente su richiesta del remoto
- **Settings** per pre-autorizzazione dispositivi (bypass password), video, rete

### Cosa NON fa (rispetto a OpenDesk completo)

- ❌ Connessioni in uscita verso altri computer
- ❌ Viewer remoto (schermo di un altro PC)
- ❌ Ricerca dispositivi nella rete
- ❌ Pulsanti manuali per chat / file transfer (solo da remoto)
- ❌ Clipboard sync, webcam remota

## Sviluppo

```bash
# Clona e installa dai sorgenti
git clone https://github.com/opendesk/opendesk-client
cd opendesk-client
uv sync --dev          # installa con le dipendenze di sviluppo

# Oppure con pip
pip install -e ".[dev]"

# Esegui i test
uv run pytest          # esegui i test
uv run pytest -v       # verboso

# Qualità del codice
uv run black .         # formatta il codice
uv run ruff check .    # lint
uv run mypy opendesk/  # type check
```

### Funzionalità opzionali

```bash
# Streaming audio (microfono) — richiede soundcard
pip install opendesk[audio]
```

### Configurazione Wayland (Linux)

Il supporto Wayland è **integrato** (dbus-next, evdev vengono installati
automaticamente su Linux).  Servono comunque i **pacchetti di sistema**:

```bash
# Ubuntu/Debian
sudo apt install gstreamer1.0-pipewire python3-gi       \
                 xdg-desktop-portal pipewire

# Opzionale: posizionamento assoluto preciso del mouse
sudo apt install ydotool

# Richiesto: permessi uinput per l'input remoto
sudo usermod -aG input $USER
# (esci e rientra)
```

**Backend supportati** (rilevati automaticamente in ordine):

| Backend | Cattura | Input | Note |
|---------|---------|-------|------|
| **PORTAL** | D-Bus + GStreamer | — | Riutilizza la sessione portal, nessun doppio dialogo |
| **PIPEWIRE** | GStreamer pipewiresrc | — | Mostra il proprio dialogo di selezione schermo |
| **MSS** | X11 | X11 (Xlib) | Fallback via XWayland |
| **uinput** | — | evdev uinput | Richiede il gruppo `input` |
| **ydotool** | — | ydotool | Mouse assoluto su Wayland |

## Architettura

```
opendesk/
├── opendesk/          # Applicazione principale (~51 file, ~21k LOC)
│   ├── core/          # Cattura schermo, input, codec, audio, camera, registrazione
│   ├── network/       # Protocollo, P2P, relay, NAT traversal
│   ├── crypto/        # Crittografia E2E (NaCl Box), autenticazione Argon2
│   ├── services/      # Pipeline di streaming, connection service
│   ├── ui/            # Widget PySide6 + temi QSS (chiaro/scuro)
│   ├── host_app.py    # OpenDesk Host: entry point solo in ingresso
│   └── utils/         # Logging, rilevamento piattaforma, task asincroni off-thread
├── tests/             # 150+ test — unit, integrazione, edge case
└── uv.lock            # Dipendenze bloccate
```

## Codifica video

OpenDesk usa **PyAV** (binding FFmpeg) per la codifica video H.264/H.265 con
supporto all'accelerazione hardware.

### Preset di qualità

| Livello | CRF | Bitrate (legacy) | Caso d'uso |
|---------|-----|-------------------|------------|
| **LOW** | 32 | ~0.5 Mbps | Connessioni lente |
| **MEDIUM** | 27 | ~2 Mbps | Bilanciato |
| **HIGH** (default) | 23 | ~8 Mbps | Buona qualità |
| **LOSSLESS** | 16 | ~20+ Mbps | LAN / quasi lossless |

CRF (Constant Rate Factor) è la modalità di controllo di rate predefinita:
garantisce una qualità visiva costante allocando dinamicamente i bit dove
servono.

### Codec supportati

OpenDesk supporta diversi codec, rilevati automaticamente in ordine di
preferenza:

| Codec | Tipo | Quando disponibile |
|-------|------|--------------------|
| `hevc_nvenc` | HW (NVIDIA) | GPU NVIDIA + driver |
| `h264_nvenc` | HW (NVIDIA) | GPU NVIDIA + driver |
| `hevc_amf` | HW (AMD) | GPU AMD + driver |
| `h264_amf` | HW (AMD) | GPU AMD + driver |
| `hevc_vaapi` | HW (Intel/AMD) | Driver VAAPI (Linux) |
| `h264_vaapi` | HW (Intel/AMD) | Driver VAAPI (Linux) |
| `hevc_videotoolbox` | HW (Apple) | macOS |
| `h264` (libx264) | SW | Sempre disponibile |

Seleziona il codec in **Strumenti → Impostazioni → Video → Encoder**.

### Ridimensionamento risoluzione

Riduci la risoluzione prima della codifica per risparmiare banda
(**Strumenti → Impostazioni → Video → Risoluzione**):

- **Completa (1:1)** — qualità massima
- **75%, 50%, 25%** — per connessioni più lente

Il ridimensionamento prima della codifica è più efficace che abbassare il
bitrate: un'immagine più piccola e nitida è meglio di una più grande e sfocata.

## Pipeline di streaming

Cattura schermo, codifica e invio in rete girano su **3 thread worker
indipendenti**:

```
┌────────────────┐    queue(max=3)   ┌────────────────┐   queue(max=30)   ┌────────────────┐
│ CaptureWorker  │─── frame_queue ──►│ EncoderWorker  │─── pkt_queue ────►│ NetworkWorker  │
│ (thread)       │                   │ (thread)       │                   │ (thread)       │
│ 30fps costanti │                   │ H.264/H.265    │                   │ relay.send()   │
│ scaling        │                   │ CRF / bitrate  │                   │ frame + tile   │
│ risoluzione    │                   │ keyframe full  │                   │                │
└────────────────┘                   │ tile JPEG      │                   └────────────────┘
                                     └────────────────┘
```

- **Back-pressure:** se l'encoder è lento, la coda dei frame si riempie e i
  frame vengono scartati invece di accumulare latenza.
- **Watchdog:** se CaptureWorker si blocca (es. nessun accesso allo schermo),
  l'EncoderWorker rileva lo stallo e ferma la pipeline.  Il timeout è di 60 s
  per tollerare l'avvio lento di Wayland (dialogo portal + PipeWire).

## Aggiornamenti incrementali a tile

Quando cambiano solo piccole regioni dello schermo (es. digitazione, movimento
del mouse), OpenDesk usa **tile JPEG 128×128** invece di un keyframe H.264
completo:

- I tile cambiati vengono rilevati con un diff vettorizzato (OpenCV + NumPy)
  **prima** di qualsiasi codifica
- Ogni tile cambiato viene codificato in JPEG al livello di qualità configurato
- Il ricevente compone i tile sull'ultimo keyframe di riferimento
- Se cambia >30% dei tile, viene inviato un keyframe completo (più efficiente)
  e i JPEG dei tile non vengono mai codificati (decisione a due fasi)

Questo approccio risparmia banda e CPU di codifica nell'uso tipico del desktop.

## Prestazioni

Interventi di ottimizzazione recenti (dettagli in
[`docs/performance-optimizations.md`](docs/performance-optimizations.md)):

- **Argon2 fuori dal main thread:** la creazione della sessione non blocca più
  il main thread Qt (prima fino a ~2.5 s su Windows).
- **Niente doppio hash:** la creazione di una nuova sessione calcola l'hash una
  sola volta (prima ne calcolava due e lasciava una sessione orfana).
- **Tile a due fasi:** quando cambia più del 30% dei tile, viene inviato un
  keyframe senza prima codificare tutti i JPEG dei tile.
- **Probe codec in cache:** il rilevamento degli encoder hardware non viene
  ripetuto a ogni rebuild dell'encoder.
- **Serializzazione fuori dall'event loop:** `msg.encode()` (msgpack dei frame)
  gira sul thread di rete, non sul loop asyncio.

## Microfono e Webcam

OpenDesk può trasmettere microfono e webcam al peer remoto, abilitando
comunicazione voce e video insieme al desktop remoto.

### Microfono 🎤

- Cattura l'audio dal microfono predefinito, lo codifica con **Opus** (via
  PyAV) e lo invia come messaggi `AUDIO_FRAME` sul relay.
- Sul lato ricevente, l'audio viene decodificato e riprodotto dallo speaker
  predefinito.
- Richiede la libreria opzionale `soundcard`:

  ```bash
  uv sync --extra audio
  ```

- Abilita in **Strumenti → Impostazioni → Generale → Audio (Microfono)** o
  clicca il pulsante **🎤 Mic** nella toolbar durante una sessione.

> **Nota:** se `soundcard` non è installata o il codec Opus non è disponibile,
> la funzione microfono viene disabilitata in modo controllato e la pipeline di
> streaming continua a funzionare senza problemi.

### Webcam 📷

- Cattura video dalla webcam predefinita usando **OpenCV**
  (`cv2.VideoCapture`), codifica i frame in JPEG e li invia come messaggi
  `CAMERA_FRAME`.
- Sul lato ricevente, il feed della webcam appare come **overlay
  picture-in-picture** nell'angolo in alto a destra del viewer del desktop
  remoto.
- OpenCV è già una dipendenza core — nessun pacchetto extra necessario.

  ```
  ┌──────────────────────────────────┐
  │                                  │
  │  Desktop remoto                  │
  │              ┌──────────┐        │
  │              │ 📷 Cam   │        │
  │              │ 240×180  │        │
  │              └──────────┘        │
  │                                  │
  └──────────────────────────────────┘
  ```

- Configura in **Strumenti → Impostazioni → Generale → Camera (Webcam)**:
  - Seleziona il dispositivo camera (rilevato automaticamente)
  - Scegli il preset di qualità (Low / Medium / High)
- Attiva/disattiva durante una sessione con il pulsante **📷 Camera** della
  toolbar.

### Controlli della toolbar

Quando una sessione remota è attiva, la toolbar mostra:

| Pulsante | Azione |
|----------|--------|
| **🎤 Mic** | Attiva/disattiva lo streaming del microfono (verde = attivo) |
| **📷 Camera**  | Attiva/disattiva lo streaming della webcam (verde = attivo) |

La barra di stato mostra anche gli indicatori **Mic On/Off** e **Cam On/Off**.

### Overlay PiP della camera

Il feed della webcam remota appare come **overlay picture-in-picture
trascinabile** nell'angolo in alto a destra della finestra del viewer:

- **Trascina** l'overlay per riposizionarlo ovunque nel viewer
- Clicca il **pulsante ✖** (rosso) per chiudere/nascondere l'overlay
- Riabilitalo con il pulsante **📷 Camera** della toolbar

### Architettura

```
┌─ HOST ──────────────────────────────┐
│                                      │
│  AudioManager (thread)               │
│    → soundcard.record()              │
│    → codifica Opus                   │
│    → AUDIO_FRAME → relay             │
│                                      │
│  CameraManager (thread)              │
│    → cv2.VideoCapture()              │
│    → codifica JPEG                   │
│    → CAMERA_FRAME → relay            │
│                                      │
│  StreamingPipeline (3 thread)        │
│    → cattura schermo → H.264 → relay │
└──────────────────────────────────────┘

┌─ CLIENT ─────────────────────────────┐
│                                       │
│  AudioManager.play_audio_frame()      │
│    → decodifica Opus → soundcard.play()│
│                                       │
│  ViewerWindow                         │
│    ├── RemoteViewer (schermo)         │
│    └── Overlay Camera PiP (alto dx)   │
│                                       │
│  CAMERA_FRAME → update_camera_frame() │
└───────────────────────────────────────┘
```

## Comandi

```bash
opendesk                     # Avvia il client desktop remoto
opendesk-release             # Avvia in modalità release (solo messaggi WARNING+)
opendesk-host                # Avvia la versione solo-host (solo in ingresso)
opendesk --log-level=WARNING # Livello di log personalizzato
opendesk --install-system-deps  # Mostra la guida all'installazione delle dipendenze di sistema

# Sviluppo (dai sorgenti)
uv run opendesk              # Avvia dai sorgenti con uv
uv run pytest                # Esegui tutti i test
```

### Livello di log

La verbosità dei log è controllata da (precedenza decrescente):

1. **argomento CLI `--log-level`** — `opendesk --log-level=WARNING`
2. **variabile d'ambiente `OPENDESK_LOG_LEVEL`** — `OPENDESK_LOG_LEVEL=ERROR opendesk`
3. **default dell'entry point** — `opendesk` usa `DEBUG` (sviluppo),
   `opendesk-release` usa `WARNING` (distribuzione)

Valori validi: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.

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
