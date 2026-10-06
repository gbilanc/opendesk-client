# Packaging — installer OpenDesk Host

Script per generare gli installer nativi di **OpenDesk Host** su ogni piattaforma.
Tutti usano PyInstaller (bundle `onedir`/`.app`) tramite lo spec condiviso
`opendesk-host.spec`.

| Piattaforma | Script | Output |
|-------------|--------|--------|
| Windows | `build_installer.ps1` | `dist/OpenDeskHostSetup-<ver>.exe` (Inno Setup) |
| Linux   | `build_installer_linux.sh` | `dist/opendesk-host_<ver>_<arch>.deb` + `.tar.gz` |
| macOS   | `build_installer_macos.sh` | `dist/OpenDeskHost-<ver>.dmg` |

## Prerequisiti

- **Windows:** [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`)
- **Linux:** `dpkg-deb` (`dpkg-dev`), `ffmpeg`
- **macOS:** `hdiutil` (di serie), `ffmpeg`
- **Tutti:** [uv](https://docs.astral.sh/uv/) (consigliato) o `pip`

## Build

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1
```

```bash
# Linux
bash packaging/build_installer_linux.sh

# macOS
bash packaging/build_installer_macos.sh
```

## Firma del codice (opzionale)

Gli script firmano se le variabili d'ambiente sono presenti:

| Piattaforma | Variabili | Effetto |
|-------------|-----------|---------|
| Windows | `OPENDESK_SIGN_PFX`, `OPENDESK_SIGN_PASS` | firma `.exe` con `signtool` |
| macOS | `OPENDESK_SIGN_IDENTITY` | firma `.app`/`.dmg` con `codesign` |
| macOS | `OPENDESK_NOTARY_APPLE_ID`, `OPENDESK_NOTARY_TEAM_ID`, `OPENDESK_NOTARY_PASSWORD` | notarizzazione con `notarytool` + `stapler` |
| Linux | `OPENDESK_GPG_KEY`, `OPENDESK_GPG_PASSPHRASE` | firma detached `.deb.asc` con GPG |

## CI — firma automatica

`.github/workflows/release.yml` costruisce gli installer sui runner nativi
(Ubuntu/Windows/macOS) a ogni tag `v*` e li pubblica nella GitHub Release.
Se i secrets sono configurati, i binari vengono firmati automaticamente.
Il build **non è cross-platform**: ogni installer va generato sul proprio OS.

### Secrets del repository

| Secret | Contenuto |
|--------|-----------|
| `WINDOWS_CERT_PFX_BASE64` | `.pfx` in base64 (`base64 -w0 cert.pfx`) |
| `WINDOWS_CERT_PASSWORD` | password del `.pfx` |
| `MACOS_CERT_P12_BASE64` | certificato Developer ID `.p12` in base64 |
| `MACOS_CERT_PASSWORD` | password del `.p12` |
| `MACOS_KEYCHAIN_PASSWORD` | password arbitraria del keychain temporaneo |
| `MACOS_SIGN_IDENTITY` | es. `Developer ID Application: Nome (TEAMID)` |
| `MACOS_NOTARY_APPLE_ID` | Apple ID per la notarizzazione |
| `MACOS_NOTARY_TEAM_ID` | Team ID Apple Developer |
| `MACOS_NOTARY_PASSWORD` | app-specific password per `notarytool` |
| `LINUX_GPG_PRIVATE_KEY` | chiave privata GPG in base64 (`gpg --export-secret-keys -a KEY | base64 -w0`) |
| `LINUX_GPG_PASSPHRASE` | passphrase della chiave GPG |

Se un secret è assente, il relativo passo di firma viene saltato e il binario
resta non firmato (nessun errore).
