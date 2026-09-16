#!/usr/bin/env bash
# =============================================================================
#  Build BOM-IQ for macOS or Linux.
#
#  Usage, from the project root:
#      ./packaging/build_unix.sh              # one-folder build
#      BOMIQ_ONEFILE=1 ./packaging/build_unix.sh
#      BOMIQ_CONSOLE=1 ./packaging/build_unix.sh    # keep a terminal attached
#
#  Output:
#      macOS  dist/BOM-IQ.app   (and dist/BOM-IQ/ )
#      Linux  dist/BOM-IQ/BOM-IQ
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
OS="$(uname -s)"

echo
echo "=== BOM-IQ build ($OS) ============================================"
echo

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON not found. Install Python 3.9 or newer." >&2
  exit 1
fi
"$PYTHON" - <<'EOF'
import sys
if sys.version_info < (3, 9):
    sys.exit(f"ERROR: Python 3.9+ required, found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]}")
EOF

echo
echo "[1/5] Creating the build virtual environment..."
if [ ! -d .build-venv ]; then
  "$PYTHON" -m venv .build-venv
fi
# shellcheck disable=SC1091
source .build-venv/bin/activate

echo "[2/5] Installing build dependencies..."
python -m pip install --upgrade pip --quiet
python -m pip install --quiet pyinstaller
# Optional runtime extras: legacy .xls, faster fuzzy matching, OS keyring and a
# native window. The app runs fine without any of them.
python -m pip install --quiet openpyxl "xlrd>=2.0.1" rapidfuzz keyring || \
  echo "  Note: some optional extras failed to install; continuing."
if [ "$OS" = "Darwin" ]; then
  python -m pip install --quiet pywebview || \
    echo "  Note: pywebview unavailable; the app will open a browser window."
else
  # On Linux pywebview needs PyGObject + WebKitGTK from the system packages;
  # attempt it, but a browser window is a perfectly good fallback.
  python -m pip install --quiet pywebview 2>/dev/null || \
    echo "  Note: pywebview unavailable (needs WebKitGTK); the app will open a browser window."
fi

echo "[3/5] Running the test suite..."
python -m unittest discover -s tests -q

echo "[4/5] Cleaning previous output..."
rm -rf build dist

echo "[5/5] Packaging with PyInstaller..."
pyinstaller packaging/bomiq.spec --noconfirm

echo
echo "=== Done =========================================================="
if [ "$OS" = "Darwin" ]; then
  echo "  App bundle: $ROOT/dist/BOM-IQ.app"
  echo
  echo "  Gatekeeper will block an unsigned app on first launch."
  echo "  Either right-click -> Open, or sign and notarise it:"
  echo "      codesign --deep --force --sign \"Developer ID Application: ...\" dist/BOM-IQ.app"
  echo "      xcrun notarytool submit ... --wait"
else
  echo "  Executable: $ROOT/dist/BOM-IQ/BOM-IQ"
  echo "  Ship the whole dist/BOM-IQ directory, or make a tarball:"
  echo "      tar -C dist -czf bom-iq-linux-x86_64.tar.gz BOM-IQ"
fi
echo
