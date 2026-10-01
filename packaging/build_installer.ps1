# Build script: produces dist/OpenDeskHostSetup-1.0.0.exe
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot  # packaging/ -> repo root
Set-Location $Root

Write-Host "==> [1/5] Installing project dependencies (uv sync)"
uv sync

Write-Host "==> [2/5] Installing PyInstaller"
uv pip install --python "$Root\.venv\Scripts\python.exe" pyinstaller

Write-Host "==> [3/5] Generating opendesk-host.ico"
& "$Root\.venv\Scripts\python.exe" packaging\make_icon.py

Write-Host "==> [4/5] Building opendesk-host.exe with PyInstaller"
& "$Root\.venv\Scripts\python.exe" -m PyInstaller packaging\opendesk-host.spec --noconfirm

Write-Host "==> [5/5] Compiling installer with Inno Setup"
$ISCC = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $ISCC)) { $ISCC = "C:\Program Files\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $ISCC)) { $ISCC = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $ISCC)) { throw "Inno Setup not found (ISCC.exe). Install it via: winget install JRSoftware.InnoSetup" }
& $ISCC "packaging\installer\opendesk-host.iss"

Write-Host ""
Write-Host "==> DONE: $Root\dist\OpenDeskHostSetup-1.0.0.exe"
