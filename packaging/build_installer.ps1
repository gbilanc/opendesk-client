# Build script: produces dist/OpenDeskHostSetup-1.0.0.exe
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot  # packaging/ -> repo root
Set-Location $Root

# Read version from pyproject.toml (single source of truth)
$Version = (Select-String -Path "$Root\pyproject.toml" -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $Version) { throw "Unable to read version from pyproject.toml" }
Write-Host "==> Version: $Version"

# Optional code signing (set OPENDESK_SIGN_PFX to a .pfx path, optionally
# OPENDESK_SIGN_PASS for the password) — signs the installer if configured
$SignPfx = $env:OPENDESK_SIGN_PFX
$SignPass = $env:OPENDESK_SIGN_PASS

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
& $ISCC "/DMyAppVersion=$Version" "packaging\installer\opendesk-host.iss"

$SetupExe = "$Root\dist\OpenDeskHostSetup-$Version.exe"

# Optional signing of the final installer
if ($SignPfx -and (Test-Path $SignPfx)) {
    $Signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $Signtool) {
        Write-Warning "signtool.exe not found in PATH - skipping signing"
    } else {
        Write-Host "==> Signing $SetupExe"
        if ($SignPass) {
            & $Signtool sign /f $SignPfx /p $SignPass /fd sha256 /tr http://timestamp.digicert.com /td sha256 $SetupExe
        } else {
            & $Signtool sign /f $SignPfx /fd sha256 /tr http://timestamp.digicert.com /td sha256 $SetupExe
        }
    }
} else {
    Write-Warning "Installer unsigned (set OPENDESK_SIGN_PFX to sign) - SmartScreen may warn users"
}

Write-Host ""
Write-Host "==> DONE: $SetupExe"
