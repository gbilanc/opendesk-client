#!/usr/bin/env bash
# Build a macOS installer for OpenDesk Host.
#
# Output:
#   dist/OpenDeskHost-<version>.dmg
#
# Usage: bash packaging/build_installer_macos.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' pyproject.toml | head -1)"
[ -n "$VERSION" ] || { echo "Cannot read version from pyproject.toml" >&2; exit 1; }
echo "==> Version: $VERSION"

APP_NAME="OpenDesk Host"
APP="dist/$APP_NAME.app"
DIST="dist"

echo "==> [1/5] Installing dependencies"
if command -v uv >/dev/null 2>&1; then
    uv sync
    uv pip install --python .venv/bin/python pyinstaller
    PY=".venv/bin/python"
else
    python3 -m pip install --user -e . pyinstaller
    PY="python3"
fi

echo "==> [2/5] Generating icon"
"$PY" packaging/make_icon.py || echo "  (icon generation skipped)"

echo "==> [3/5] Building .app bundle with PyInstaller"
"$PY" -m PyInstaller packaging/opendesk-host.spec --noconfirm

# Optional code signing (set OPENDESK_SIGN_IDENTITY to a Developer ID)
if [ -n "${OPENDESK_SIGN_IDENTITY:-}" ]; then
    echo "==> Signing $APP"
    codesign --deep --force --options runtime \
        --sign "$OPENDESK_SIGN_IDENTITY" "$APP"
fi

echo "==> [4/5] Staging DMG contents"
STAGE="build/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -a "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

echo "==> [5/5] Building .dmg"
mkdir -p "$DIST"
DMG="$DIST/OpenDeskHost-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "OpenDesk Host" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

if [ -n "${OPENDESK_SIGN_IDENTITY:-}" ]; then
    codesign --force --sign "$OPENDESK_SIGN_IDENTITY" "$DMG" || true
fi

# Notarizzazione (richiede credenziali Apple Developer).
if [ -n "${OPENDESK_NOTARY_APPLE_ID:-}" ] && [ -n "${OPENDESK_NOTARY_TEAM_ID:-}" ] \
    && [ -n "${OPENDESK_NOTARY_PASSWORD:-}" ]; then
    echo "==> Notarizing $DMG"
    xcrun notarytool submit "$DMG" \
        --apple-id "$OPENDESK_NOTARY_APPLE_ID" \
        --team-id "$OPENDESK_NOTARY_TEAM_ID" \
        --password "$OPENDESK_NOTARY_PASSWORD" \
        --wait
    xcrun stapler staple "$DMG"
fi

echo ""
echo "==> DONE: $DMG"
