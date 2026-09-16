@echo off
REM ============================================================================
REM  Build BOM-IQ.exe on Windows.
REM
REM  Prerequisites: Python 3.9+ (python.org or the Microsoft Store build).
REM  Run this from a normal command prompt in the project root:
REM
REM      packaging\build_windows.bat
REM
REM  Output: dist\BOM-IQ\BOM-IQ.exe  (plus its folder of dependencies)
REM ============================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0\.."

echo.
echo === BOM-IQ Windows build =========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: python was not found on PATH.
  echo Install Python 3.9 or newer from https://python.org and tick
  echo "Add python.exe to PATH" during setup.
  exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Python %PYVER%

echo.
echo [1/5] Creating the build virtual environment...
if not exist .build-venv (
  python -m venv .build-venv || exit /b 1
)
call .build-venv\Scripts\activate.bat

echo [2/5] Installing build dependencies...
python -m pip install --upgrade pip --quiet
python -m pip install --quiet pyinstaller || exit /b 1
REM Optional runtime extras. The app works without them; they add
REM legacy .xls reading, faster fuzzy matching, the OS keyring and a
REM native window instead of a browser window.
python -m pip install --quiet openpyxl "xlrd>=2.0.1" rapidfuzz keyring pywebview
if errorlevel 1 (
  echo   Note: some optional extras failed to install. Continuing without them.
)

echo [3/5] Running the test suite...
python -m unittest discover -s tests -q
if errorlevel 1 (
  echo ERROR: tests failed. Fix them before packaging.
  exit /b 1
)

echo [4/5] Cleaning previous output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [5/5] Packaging with PyInstaller...
pyinstaller packaging\bomiq.spec --noconfirm || exit /b 1

echo.
echo === Done =========================================================
echo   Executable: dist\BOM-IQ\BOM-IQ.exe
echo   Ship the whole dist\BOM-IQ folder, or zip it.
echo.
echo   To build a single .exe instead (slower to start):
echo       set BOMIQ_ONEFILE=1 ^&^& pyinstaller packaging\bomiq.spec --noconfirm
echo.
echo   To keep a console window for diagnostics:
echo       set BOMIQ_CONSOLE=1 ^&^& pyinstaller packaging\bomiq.spec --noconfirm
echo.
endlocal
