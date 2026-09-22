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

## Installazione

### Linux / macOS (systemd)

```bash
# 1. Genera il file di desktop entry
sudo python3 cross_platform_installer.py --target-dir /home/giampaolo/Codium/opendesk-client

# 2. Avvia il servizio
sudo systemctl daemon-reload
sudo systemctl enable opendesk-host
sudo systemctl start opendesk-host
sudo systemctl status opendesk-host

# 3. Configura il client OpenDesk (Tools → Settings → Network)
#    Relay Host = IP del server
#    Relay Port = 8474
```

### Windows

```cmd
# 1. Genera il file di desktop entry
python cross_platform_installer_windows.py

# 2. Avvia il servizio
nohup python cross_platform_installer_windows.py > opendesk-host.log 2>&1 &

# 3. Configura il client OpenDesk (Tools → Settings → Network)
```

### macOS

```bash
# 1. Genera il file di desktop entry
python3 cross_platform_installer_darwin.py

# 2. Avvia il servizio
nohup python3 cross_platform_installer_darwin.py > opendesk-host.log 2>&1 &

# 3. Configura il client OpenDesk (Tools → Settings → Network)
```

## Comandi utili

```bash
# Verifica il log
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
