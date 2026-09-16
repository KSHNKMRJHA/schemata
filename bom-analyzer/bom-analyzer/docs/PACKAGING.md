# Packaging

## One command

```bash
# Windows
packaging\build_windows.bat

# macOS / Linux
./packaging/build_unix.sh
```

Each script creates a build virtualenv, installs PyInstaller and the optional
runtime extras, **runs the test suite**, and packages the app. The build fails
if the tests fail.

Output:

| Platform | Result |
|---|---|
| Windows | `dist\BOM-IQ\BOM-IQ.exe` plus its dependency folder |
| macOS | `dist/BOM-IQ.app` |
| Linux | `dist/BOM-IQ/BOM-IQ` |

Ship the whole folder (or zip it). One-folder is the default because start-up
is several times faster than one-file, the UI assets stay inspectable, and
antivirus heuristics are far less suspicious of it.

## Options

| Variable | Effect |
|---|---|
| `BOMIQ_ONEFILE=1` | build a single executable instead of a folder |
| `BOMIQ_CONSOLE=1` | keep a console window attached, for diagnostics |
| `BOMIQ_CODESIGN_IDENTITY` | macOS signing identity passed to PyInstaller |

```bash
BOMIQ_ONEFILE=1 ./packaging/build_unix.sh
```

## Icons

Drop any of these into `packaging/` and the spec picks it up automatically:
`icon.ico` (Windows), `icon.icns` (macOS), `icon.png` (Linux).

## Windows notes

- Nothing needs to be installed on the target machine — the bundle carries its
  own Python.
- Unsigned executables may trigger SmartScreen on first run. Sign with an
  EV code-signing certificate to avoid it:
  ```
  signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 ^
      /a dist\BOM-IQ\BOM-IQ.exe
  ```
- The app binds only `127.0.0.1`, so it does not trigger a firewall prompt.

## macOS notes

Gatekeeper blocks an unsigned app on first launch. Either right-click → Open,
or sign and notarise:

```bash
codesign --deep --force --options runtime \
         --sign "Developer ID Application: Your Org (TEAMID)" dist/BOM-IQ.app
ditto -c -k --keepParent dist/BOM-IQ.app BOM-IQ.zip
xcrun notarytool submit BOM-IQ.zip --apple-id … --team-id … --wait
xcrun stapler staple dist/BOM-IQ.app
```

Build on the oldest macOS you intend to support; PyInstaller bundles are not
backward compatible across major versions.

## Linux notes

`pywebview` needs PyGObject and WebKitGTK from the system packages:

```bash
# Debian / Ubuntu
sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1
```

Without them the app opens a chromeless Chrome/Edge window, or your default
browser. Both are fully functional.

## What goes in the bundle

The spec includes `bomiq/server/ui/` (the single-page UI is loaded from disk at
runtime), `docs/`, `samples/` and `README.md`. It excludes tkinter, numpy,
pandas, matplotlib, pytest, setuptools and the test modules, which keeps the
bundle to roughly 15–25 MB depending on platform and which optional extras
were installed.

## Reproducibility

The build is deterministic apart from PyInstaller's own bootloader. To pin
everything, install exact versions before building:

```bash
pip install pyinstaller==6.* openpyxl==3.1.* rapidfuzz==3.*
```

## Verifying a build

```bash
dist/BOM-IQ/BOM-IQ --cli doctor        # environment diagnostics
dist/BOM-IQ/BOM-IQ --cli analyse samples/01-altium-export.csv --offline
```

`doctor` confirms the data directory is writable, which spreadsheet reader is
active, which providers are configured, and where the database lives.
