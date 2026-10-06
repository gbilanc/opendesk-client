# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OpenDesk Host (opendesk-host).

Builds a onedir bundle in ``dist/opendesk-host/`` (Windows/Linux) or a
``.app`` bundle (macOS).  Used by:
  - packaging/build_installer.ps1        (Windows → Inno Setup)
  - packaging/build_installer_linux.sh   (Linux   → .deb)
  - packaging/build_installer_macos.sh   (macOS   → .dmg)
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # repo root (packaging/..)

datas = collect_data_files("opendesk", includes=["**/*.qss", "**/*.svg", "**/*.png"])
datas += [(str(ROOT / "packaging" / "opendesk-host.ico"), ".")]

hiddenimports = collect_submodules("opendesk")
hiddenimports += [
    "argon2",
    "nacl",
    "nacl.public",
    "msgpack",
    "mss",
    "PIL",
    "PIL.Image",
    "cv2",
    "av",
    "numpy",
]

if sys.platform.startswith("linux"):
    hiddenimports += [
        "Xlib",
        "Xlib.display",
        "Xlib.ext.xtest",
        "dbus_next",
        "dbus_next.aio",
        "evdev",
        "gi",
        "gi.repository.Gst",
    ]
    # PyInstaller non risolve i typelib GObject via import; copiali se presenti.
    try:
        from PyInstaller.utils.hooks import collect_typelib_data

        datas += collect_typelib_data(["Gst-1.0", "GstApp-1.0"], "gi_typelibs", "gi_typelibs")
    except Exception:  # noqa: BLE001 — GStreamer/gi opzionali
        pass

a = Analysis(
    [str(ROOT / "packaging" / "host_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="opendesk-host",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "opendesk-host.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="opendesk-host",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="OpenDesk Host.app",
        icon=str(ROOT / "packaging" / "opendesk-host.ico"),
        bundle_identifier="io.opendesk.host",
        info_plist={
            "CFBundleName": "OpenDesk Host",
            "CFBundleDisplayName": "OpenDesk Host",
            "CFBundleShortVersionString": "1.6.0",
            "NSHighResolutionCapable": True,
            "LSUIElement": True,
        },
    )
