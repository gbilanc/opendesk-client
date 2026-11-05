#!/usr/bin/env bash
# Build a Linux installer for OpenDesk (client) or OpenDesk Host.
#
# Usage: bash packaging/build_installer_linux.sh [host|client]
#
# Output (host):
#   dist/opendesk-host_<version>_amd64.deb
#   dist/opendesk-host-<version>-linux.tar.gz
# Output (client):
#   dist/opendesk_<version>_amd64.deb
#   dist/opendesk-<version>-linux.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' pyproject.toml | head -1)"
[ -n "$VERSION" ] || { echo "Cannot read version from pyproject.toml" >&2; exit 1; }
echo "==> Version: $VERSION"

COMPONENT="${1:-host}"
case "$COMPONENT" in
    host)
        PKG="opendesk-host"
        APP_NAME="OpenDesk Host"
        APP_DESC="OpenDesk Host (incoming-only)"
        APP_LONG_DESC="$(printf 'Cross-platform remote desktop host: accepts incoming connections,\n streams the screen and injects remote input.')"
        SPEC="packaging/opendesk-host.spec"
        ICON="opendesk-host.ico"
        EXEC_ARGS="--minimized"
        WITH_SYSTEMD=1
        ;;
    client)
        PKG="opendesk"
        APP_NAME="OpenDesk"
        APP_DESC="OpenDesk"
        APP_LONG_DESC="$(printf 'Cross-platform remote desktop client: connects to remote hosts,\n views/controls their screen and transfers files.')"
        SPEC="packaging/opendesk-client.spec"
        ICON="opendesk.ico"
        EXEC_ARGS=""
        WITH_SYSTEMD=0
        ;;
    *)
        echo "Usage: $0 [host|client]" >&2
        exit 2
        ;;
esac

echo "==> Component: $COMPONENT ($PKG)"

ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
STAGE="build/deb/${PKG}_${VERSION}_${ARCH}"
DIST="dist"

echo "==> [1/6] Installing dependencies"
if command -v uv >/dev/null 2>&1; then
    uv sync
    uv pip install --python .venv/bin/python pyinstaller
    PY=".venv/bin/python"
else
    python3 -m pip install --user -e . pyinstaller
    PY="python3"
fi

echo "==> [2/6] Generating icon"
"$PY" packaging/make_icon.py || echo "  (icon generation skipped)"

echo "==> [3/6] Building onedir bundle with PyInstaller"
"$PY" -m PyInstaller "$SPEC" --noconfirm

echo "==> [4/6] Staging .deb tree"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
    "$STAGE/opt/$PKG" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/icons/hicolor/256x256/apps"
if [ "$WITH_SYSTEMD" = 1 ]; then
    mkdir -p "$STAGE/usr/lib/systemd/user"
fi

cp -a "dist/$PKG/." "$STAGE/opt/$PKG/"
ln -sf "/opt/$PKG/$PKG" "$STAGE/usr/bin/$PKG"

# PNG icon (extract the 256x256 frame from the generated .ico)
"$PY" - <<PY
from PIL import Image
img = Image.open("packaging/$ICON")
img.size = (256, 256)
img.convert("RGBA").save("$STAGE/usr/share/icons/hicolor/256x256/apps/$PKG.png")
PY

cat > "$STAGE/usr/share/applications/$PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Remote Desktop Application
Icon=$PKG
Exec=${PKG}${EXEC_ARGS:+ $EXEC_ARGS}
Terminal=false
Categories=Network;RemoteAccess;
StartupWMClass=opendesk
EOF

if [ "$WITH_SYSTEMD" = 1 ]; then
    cat > "$STAGE/usr/lib/systemd/user/$PKG.service" <<EOF
[Unit]
Description=$APP_DESC
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/$PKG --minimized --log-level=WARNING
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
fi

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: net
Priority: optional
Architecture: $ARCH
Maintainer: OpenDesk <noreply@opendesk.io>
Depends: ffmpeg, libx11-6, libxext6, libxrender1, libxtst6, libgl1
Description: $APP_DESC
 $APP_LONG_DESC
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst"

echo "==> [5/6] Building .deb"
mkdir -p "$DIST"
DEB="$DIST/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB"

echo "==> [6/6] Building portable tarball"
TAR="$DIST/${PKG}-${VERSION}-linux.tar.gz"
tar -C dist -czf "$TAR" "$PKG"

# Firma GPG opzionale del .deb (detached ASCII signature).
if [ -n "${OPENDESK_GPG_KEY:-}" ]; then
    echo "==> Signing $DEB with GPG key $OPENDESK_GPG_KEY"
    GPG_ARGS=(--batch --yes --armor --local-user "$OPENDESK_GPG_KEY")
    if [ -n "${OPENDESK_GPG_PASSPHRASE:-}" ]; then
        GPG_ARGS+=(--pinentry-mode loopback --passphrase "$OPENDESK_GPG_PASSPHRASE")
    fi
    gpg "${GPG_ARGS[@]}" --detach-sign "$DEB"
fi

echo ""
echo "==> DONE"
echo "    $DEB"
echo "    $TAR"
