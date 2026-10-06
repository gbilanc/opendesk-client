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

- **Windows:** `OPENDESK_SIGN_PFX` (+ `OPENDESK_SIGN_PASS`) → firma con `signtool`.
- **macOS:** `OPENDESK_SIGN_IDENTITY` (Developer ID) → firma `.app` e `.dmg` con `codesign`.

## CI

`.github/workflows/release.yml` costruisce gli installer sui runner nativi
(Ubuntu/Windows/macOS) a ogni tag `v*` e li pubblica nella GitHub Release.
Il build **non è cross-platform**: ogni installer va generato sul proprio OS.
