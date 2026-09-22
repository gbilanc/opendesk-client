# Opendesk Host — Servizio di sistema

Questa cartella contiene i file per installare opendesk-host come servizio in background.

## Struttura

```
systemd/
├── opendesk-host.user.service     # ✅ Servizio UTENTE (consigliato — GUI nella sessione grafica)
├── opendesk-host.service          # Servizio di SISTEMA (root) — richiede accesso al display
├── opendesk-host-wait.service     # Servizio tipo "Type=oneshot" (attivo dopo esito)
├── wrapper.sh                     # Wrapper per systemd (Linux/macOS)
├── wrapper_windows.sh             # Wrapper per Windows
└── wrapper_macos.sh               # Wrapper per macOS
```

> ⚠️ `opendesk-host` è un'app **GUI** (deve mostrare ID + password). Un servizio
> di **sistema** gira come root senza accesso alla sessione grafica e va in
> crash-loop (`could not connect to display :0` → `SIGABRT`). Usare il servizio
> **utente** descritto sotto.
>
> 🖥️ I servizi sono configurati con `--minimized`: l'app parte **nascosta nella
> system tray** (icona accanto all'orologio) invece che con la finestra aperta.
> Click sull'icona per riaprire la finestra; il menu dell'icona offre
> *New Session*, *Copy ID*, *Copy password* e *Quit*. Chiudere la finestra con
> la ✕ la riduce di nuovo a icona senza terminare l'host.

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

### Linux — servizio UTENTE (consigliato)

Gira dentro la sessione grafica dell'utente, quindi eredita automaticamente
`DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY` e `XDG_RUNTIME_DIR` da `systemd --user`
(verifica con `systemctl --user show-environment`). Nessun privilegio root.

```bash
# 1. Genera desktop entry + autostart XDG e installa il pacchetto (via uv)
python3 cross_platform_installer.py --target-dir /home/giampaolo/Codium/opendesk-client
#    (aggiungi --skip-deps se le dipendenze di sistema sono già presenti)
#    L'installer crea ~/.config/autostart/opendesk-host.desktop con
#    Exec=opendesk-host --minimized, cosi' l'host parte a icona al login.

# 2. (Alternativa all'autostart) Installa il servizio utente
mkdir -p ~/.config/systemd/user
cp systemd/opendesk-host.user.service ~/.config/systemd/user/opendesk-host.service
systemctl --user daemon-reload
systemctl --user enable --now opendesk-host
systemctl --user status opendesk-host

# 3. Configura il client OpenDesk (Tools → Settings → Network)
#    Relay Host = IP del server
#    Relay Port = 8474
```

Il servizio parte al login dell'utente. Per farlo partire anche senza login
(senza finestra visibile) abilitare il *linger* e usare `QT_QPA_PLATFORM=offscreen`:

```bash
sudo loginctl enable-linger $USER
```

### Linux — servizio di SISTEMA (alternativa, richiede setup display)

Se proprio serve un servizio di sistema, va eseguito come utente grafico e con
le variabili di autenticazione X11/Wayland corrette:

```bash
sudo cp systemd/opendesk-host.service /etc/systemd/system/
sudo systemctl edit opendesk-host   # aggiungi User=, XAUTHORITY=, ecc.
sudo systemctl daemon-reload
sudo systemctl enable --now opendesk-host
```

Esempio override:

```ini
[Service]
User=giampaolo
Environment=DISPLAY=:0
Environment=XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.XXXXXX
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-0
```

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
# Servizio UTENTE
systemctl --user status opendesk-host
journalctl --user -u opendesk-host -f
systemctl --user restart opendesk-host
systemctl --user stop opendesk-host
systemctl --user disable opendesk-host

# Verifica che le variabili grafiche siano disponibili al servizio
systemctl --user show-environment | grep -E 'DISPLAY|WAYLAND|XAUTHORITY'

# Servizio di SISTEMA (se usato)
journalctl -u opendesk-host -f
sudo systemctl stop opendesk-host
sudo systemctl disable opendesk-host

# Log dell'applicazione (file)
tail -f ~/.local/share/opendesk/logs/opendesk.log
```

## Cosa fa

- Avvia opendesk-host in background continuamente, **ridotto a icona nella system tray**
- Connette al relay (porta 8474)
- Mostra ID + password nella finestra compatta (riapribile dalla tray)
- Accetta solo connessioni in ingresso
- Streaming, input remoto, chat e file transfer automatici
