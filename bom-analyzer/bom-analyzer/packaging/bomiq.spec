# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for BOM-IQ.

Build from the project root:

    pip install pyinstaller
    pyinstaller packaging/bomiq.spec --noconfirm

Output:
    dist/BOM-IQ/BOM-IQ          (Linux / one-folder)
    dist/BOM-IQ.app             (macOS bundle)
    dist/BOM-IQ/BOM-IQ.exe      (Windows / one-folder)

One-folder is the default rather than one-file: start-up is several times
faster, the UI assets stay inspectable, and antivirus software is far less
suspicious of it. Set BOMIQ_ONEFILE=1 to build a single executable instead.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 (PyInstaller global)
ONEFILE = os.environ.get("BOMIQ_ONEFILE") == "1"
APP_NAME = "BOM-IQ"

sys.path.insert(0, str(ROOT))
from bomiq.version import __version__           # noqa: E402

# The UI is loaded from disk at runtime, so it must be bundled as data.
datas = [
    (str(ROOT / "bomiq" / "server" / "ui"), "bomiq/server/ui"),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "docs"), "docs"),
    (str(ROOT / "samples"), "samples"),
]

# Optional accelerators: included when present in the build environment,
# skipped silently when not. The app works either way.
hiddenimports = [
    "bomiq.providers.digikey", "bomiq.providers.mouser",
    "bomiq.providers.nexar", "bomiq.providers.arrow",
    "bomiq.providers.farnell", "bomiq.providers.lcsc",
    "bomiq.providers.trustedparts", "bomiq.providers.mock",
]
for optional in ("openpyxl", "xlrd", "rapidfuzz", "keyring",
                 "keyring.backends", "webview"):
    try:
        __import__(optional.split(".")[0])
        hiddenimports += collect_submodules(optional)
    except Exception:
        pass

excludes = [
    "tkinter", "test", "unittest", "pydoc_data", "lib2to3", "idlelib",
    "matplotlib", "numpy", "pandas", "scipy", "PIL", "IPython", "pytest",
    "setuptools", "pip", "wheel",
]

block_cipher = None

a = Analysis(                                    # noqa: F821
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)   # noqa: F821

icon = None
for candidate in ("icon.ico", "icon.icns", "icon.png"):
    path = ROOT / "packaging" / candidate
    if path.exists():
        icon = str(path)
        break

common = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX trips antivirus heuristics
    console=(os.environ.get("BOMIQ_CONSOLE") == "1"),
    disable_windowed_traceback=False,
    argv_emulation=False,         # we do our own argument handling
    target_arch=None,
    codesign_identity=os.environ.get("BOMIQ_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
    icon=icon,
)

if ONEFILE:
    exe = EXE(                                   # noqa: F821
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)   # noqa: F821
    coll = COLLECT(                              # noqa: F821
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=False, name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(                                # noqa: F821
        coll if not ONEFILE else exe,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="com.ltts.bomiq",
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": "BOM-IQ",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSHumanReadableCopyright": "Local BOM analysis tool.",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Bill of Materials",
                    "CFBundleTypeRole": "Viewer",
                    "LSItemContentTypes": [
                        "public.comma-separated-values-text",
                        "public.tab-separated-values-text",
                        "org.openxmlformats.spreadsheetml.sheet",
                        "com.microsoft.excel.xls",
                        "org.oasis-open.opendocument.spreadsheet",
                        "public.json",
                    ],
                }
            ],
        },
    )
