# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for QuantsMind-Agent.

Build with ``python scripts/build_executable.py``. Two layouts are supported:

* **onedir** (default) -- ``dist/QuantsMindAgent/QuantsMindAgent[.exe]`` plus a
  folder of Qt plugins and shared libraries. Starts faster, and antivirus
  engines flag large single-file executables far more often, so this is the
  layout we ship and smoke-test.
* **onefile** (``QM_ONEFILE=1``) -- a single self-extracting executable. Convenient
  to copy, slower to start, and more likely to be quarantined by security
  software for that reason.

Deliberately *not* bundled, because they must stay outside the application and be
readable and writable by the user: configuration, credentials, logs, and any
workspace the user opens. ``platformdirs`` resolves them at runtime, and
``--config-dir`` / ``QM_CONFIG_DIR`` override the location.

The spec imports nothing from ``quantsmind_agent`` so that building cannot
depend on the package importing cleanly.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - injected by PyInstaller
REPO = SPEC_DIR.parent
VERSION_FILE = REPO / "src" / "quantsmind_agent" / "version.py"

match = re.search(r'__version__: Final\[str\] = "([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"))
VERSION = match.group(1) if match else "0.0.0"

ONEFILE = os.environ.get("QM_ONEFILE", "0") == "1"
APP_NAME = "QuantsMindAgent"

# Development and documentation tooling has no business in a shipped binary.
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtMultimedia",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtNetworkAuth",
    "PySide6.QtPositioning",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "tkinter",
    "unittest",
    "pydoc_data",
    "doctest",
    "pytest",
    "_pytest",
    "mypy",
    "ruff",
    "sphinx",
    "IPython",
    "notebook",
]

# keyring selects a backend through entry points at runtime, so its backends are
# not visible to static analysis and must be collected explicitly. The
# application degrades to a file-backed store when no backend is usable.
HIDDEN_IMPORTS = [
    "quantsmind_agent",
    *collect_submodules("keyring.backends"),
]

block_cipher = None

a = Analysis(
    [str(REPO / "scripts" / "pyinstaller_entry.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Transitive dependency metadata that a few of our runtime dependencies read
    # at import time.
    metadata_files=collect_data_files("keyring"),
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        # No console: this is a GUI application. Startup diagnostics go to the
        # log file, and --check-config is the supported way to get console
        # output when debugging a packaged build.
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(SPEC_DIR / "quantsmind.ico") if (SPEC_DIR / "quantsmind.ico").exists() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
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
        icon=str(SPEC_DIR / "quantsmind.ico") if (SPEC_DIR / "quantsmind.ico").exists() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )

if sys.platform == "darwin":
    app = BUNDLE(
        coll if not ONEFILE else exe,
        name=f"{APP_NAME}.app",
        icon=str(SPEC_DIR / "quantsmind.icns") if (SPEC_DIR / "quantsmind.icns").exists() else None,
        bundle_identifier="com.quantsmind.agent",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            # The agent edits and runs commands inside the folder the user
            # opens, so it needs access to arbitrary user-selected paths.
            "NSDesktopFolderUsageDescription": "QuantsMind-Agent works inside project folders you choose.",
            "NSDocumentsFolderUsageDescription": "QuantsMind-Agent works inside project folders you choose.",
            "NSDownloadsFolderUsageDescription": "QuantsMind-Agent works inside project folders you choose.",
        },
    )