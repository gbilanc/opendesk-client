# Opendesk Host — Servizio di sistema

Questa cartella contiene i file per installare opendesk-host come servizio in background.

## Struttura

```
systemd/
├── opendesk-host.service          # Servizio tipo "Type=simple" (sviluppo)
├── opendesk-host-wait.service     # Servizio tipo "Type=oneshot" (attivo dopo esito)
├── wrapper.sh                     # Wrapper per systemd (Linux/macOS)
├── wrapper_windows.sh             # Wrapper per Windows
└── wrapper_macos.sh               # Wrapper per macOS
```

## Prerequisiti

- **uv** (consigliato): https://docs.astral.sh/uv/ — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Le dipendenze di sistema (ffmpeg, libX11, …) possono essere installate
  automaticamente dall'installer (richiede sudo), oppure manualmente:

```bash
sudo apt-get install -y ffmpeg libx11-6 libxext6 libxrender1 libxtst6
```

> **Nota su PEP 668**: sulle distribuzioni Debian/Ubuntu recenti (incluse
> Mint 21.3+/22.x) il pip di sistema rifiuta l'installazione di pacchetti
> ("externally-managed-environment"). Per questo motivo l'installer usa
> **uv** quando disponibile. Installare uv *prima* di procedere.

## Installazione

### Linux / macOS (systemd)

```bash
# 1. Genera il file di desktop entry e installa il pacchetto (via uv)
python3 cross_platform_installer.py --target-dir /home/giampaolo/Codium/opendesk-client
#    (aggiungi --skip-deps se le dipendenze di sistema sono già presenti)

# 2. Avvia il servizio
sudo cp systemd/opendesk-host.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable opendesk-host
sudo systemctl start opendesk-host
sudo systemctl status opendesk-host

# 3. Configura il client OpenDesk (Tools → Settings → Network)
#    Relay Host = IP del server
#    Relay Port = 8474
```

> **Nota GUI**: il servizio esegue un'app grafica (mostra ID + password).
> Il file `opendesk-host.service` imposta `DISPLAY=:0`; su Wayland o display
> diverso adattare la variabile `Environment=DISPLAY=` nel service file.

### Windows

```cmd
# 1. Genera il file di desktop entry
python cross_platform_installer_windows.py

# 2. Avvia il servizio (wrapper)
bash systemd/wrapper_windows.sh

# 3. Configura il client OpenDesk (Tools → Settings → Network)
```

### macOS

```bash
# 1. Genera il file di desktop entry
python3 cross_platform_installer_darwin.py

# 2. Avvia il servizio (wrapper, equivalente launchd)
bash systemd/wrapper_macos.sh

# 3. Configura il client OpenDesk (Tools → Settings → Network)
```

## Comandi utili

```bash
# Verifica il log di systemd
journalctl -u opendesk-host -f

# Verifica il log del wrapper
tail -f /tmp/opendesk-host.log

# Disattiva il servizio
sudo systemctl stop opendesk-host
sudo systemctl disable opendesk-host

# Elimina log
sudo rm /tmp/opendesk-host.log
```

## Cosa fa

- Avvia opendesk-host in background continuamente
- Connette al relay (porta 8474)
- Mostra ID + password in una finestra compatta
- Accetta solo connessioni in ingresso
- Streaming, input remoto, chat e file transfer automatici
