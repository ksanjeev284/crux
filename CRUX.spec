# PyInstaller spec for the CRUX desktop GUI.
# Run from the repository root:
#   pyinstaller --noconfirm --clean CRUX.spec

import os
import sys

from PyInstaller.building.api import EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_submodules

SPECDIR = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821
repo = SPECDIR

hidden = collect_submodules("crux")
hidden += [
    "miner",
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.messagebox",
]

# A console on Linux makes `--version` visible when launched from a terminal.
# Windows and macOS stay windowed so double-click does not flash a terminal.
console = sys.platform.startswith("linux")

a = Analysis(
    [os.path.join(repo, "gui.py")],
    pathex=[repo],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CRUX",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
