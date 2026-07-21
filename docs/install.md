# OpenDesk — Guida alla pubblicazione e installazione

## 📦 Procedura di pubblicazione (per maintainer)

### 1. Build dei pacchetti

Per generare i pacchetti installabili serve eseguire la build **sulla piattaforma target**. Non è possibile cross-compilare (es. generare un `.exe` da Linux).

#### Linux

```bash
# Prerequisiti (Ubuntu/Debian)
sudo apt install ffmpeg libx11-dev libxext-dev libxtst-dev python3-gi

# Build
bash scripts/build.sh --clean --version 1.0.0
```

**Output:** `dist/opendesk-1.0.0-linux-x86_64.tar.gz`  
Se `appimagetool` è installato, produce anche `.AppImage`.

#### Windows (Git Bash o PowerShell)

```powershell
# Prerequisiti
# - Python 3.12+
# - Git Bash (per eseguire lo script bash)
# - NSIS (per l'installer .exe, opzionale)

bash scripts/build.sh --clean --version 1.0.0
```

**Output:** `dist/opendesk-1.0.0-windows-x86_64.zip`  
Se `makensis` è installato, produce anche `.exe`.

#### macOS

```bash
# Prerequisiti
brew install create-dmg   # opzionale, per il .dmg

bash scripts/build.sh --clean --version 1.0.0
```

**Output:** `dist/opendesk-1.0.0-macos-x86_64.dmg`

---

### 2. Upload sul server

```bash
# Carica tutti i pacchetti e gli script di install
OPENDESK_SERVER=utente@tuo-server:/var/www/html bash scripts/upload.sh
```

Lo script:
- Carica i pacchetti in `dl/`
- Carica `install.sh` e `install.ps1` nella root
- Crea i symlink `latest` → versione corrente

### 3. Struttura attesa sul server

```
https://tuo-server.it/
├── install.sh                   ← Installer Linux/macOS
├── install.ps1                  ← Installer Windows
└── dl/
    ├── opendesk-1.0.0-linux-x86_64.tar.gz
    ├── opendesk-1.0.0-windows-x86_64.zip   (o .exe)
    ├── opendesk-1.0.0-macos-x86_64.dmg
    ├── opendesk-latest-linux-x86_64.tar.gz       → symlink
    ├── opendesk-latest-windows-x86_64.zip        → symlink
    └── opendesk-latest-macos-x86_64.dmg          → symlink
```

---

## 💻 Procedura di installazione (per utenti finali)

### Linux

L'installer richiede **curl** e **bash** (preinstallati su tutte le distribuzioni).

```bash
curl -fsSL http://tuo-server.it/install.sh | bash
```

Cosa fa:
1. Rileva automaticamente la distribuzione (Ubuntu, Fedora, Arch, ecc.)
2. Installa le dipendenze di sistema (`ffmpeg`, `libx11`, `libxtst`, `pipewire`) via `apt`, `dnf` o `pacman`
3. Scarica il pacchetto da `http://tuo-server.it/dl/`
4. Estrae in `~/.local/opendesk/`
5. Crea symlink in `~/.local/bin/opendesk`
6. Crea il desktop entry per il menu applicazioni

**Avviare l'app:**

```bash
opendesk
```

Oppure cerca "OpenDesk" nel menu applicazioni.

**Disinstallare:**

```bash
rm -rf ~/.local/opendesk ~/.local/bin/opendesk ~/.local/share/applications/opendesk.desktop
```

---

### Windows

Apri **PowerShell come Amministratore**:

```powershell
iwr -useb http://tuo-server.it/install.ps1 | iex
```

Cosa fa:
1. Rileva l'architettura (x86_64 / x86)
2. Scarica l'installer `.exe` (o `.zip`)
3. Installa in `%LOCALAPPDATA%\OpenDesk`
4. Crea collegamenti nel menu Start e sul Desktop

**Avviare l'app:** Dal menu Start → "OpenDesk", oppure:

```powershell
%LOCALAPPDATA%\OpenDesk\opendesk.exe
```

**Disinstallare:** Dal Pannello di controllo → Programmi → OpenDesk, oppure:

```powershell
rmdir /s %LOCALAPPDATA%\OpenDesk
```

---

### macOS

```bash
curl -fsSL http://tuo-server.it/install.sh | bash
```

Cosa fa:
1. Scarica il `.dmg`
2. Monta l'immagine disco
3. Copia `OpenDesk.app` in `/Applications`
4. Smonta il `.dmg`

**Avviare l'app:** Da Launchpad o Finder → Applicazioni → OpenDesk.

**Disinstallare:** Trascina `OpenDesk.app` nel Cestino.

---

## 🔧 Variabili d'ambiente

L'installer supporta le seguenti variabili per override:

| Variabile | Default | Descrizione |
|---|---|---|
| `OPENDESK_DOWNLOAD_BASE` | `http://tuo-server.it/dl` | URL base per i pacchetti |
| `OPENDESK_VERSION` | `latest` | Versione da installare |

**Esempio — installare una versione specifica:**

```bash
curl -fsSL http://tuo-server.it/install.sh | OPENDESK_VERSION=1.0.0 bash
```

**Esempio — usare un server mirror:**

```bash
curl -fsSL http://tuo-server.it/install.sh | OPENDESK_DOWNLOAD_BASE=https://mirror.example.com/dl bash
```

---

## 📁 File del progetto

| File | Descrizione |
|---|---|
| `opendesk.spec` | Specifica per PyInstaller |
| `scripts/build.sh` | Orchestratore di build |
| `scripts/package-linux.sh` | Confeziona pacchetto Linux (AppImage / tar.gz) |
| `scripts/package-windows.sh` | Confeziona pacchetto Windows (NSIS / zip) |
| `scripts/package-macos.sh` | Confeziona pacchetto macOS (DMG) |
| `scripts/install.sh` | Installer utente per Linux e macOS |
| `scripts/install.ps1` | Installer utente per Windows |
| `scripts/opendesk.desktop` | Desktop entry per Linux |
| `scripts/opendesk.nsi` | Template per installer NSIS (Windows) |
| `scripts/upload.sh` | Carica i file sul server via rsync |
