"""
Desktop launcher.

Starts the local server on a free loopback port, then opens the UI in the best
window the machine can provide, trying in order:

1. **pywebview** -- a real native window (WebKit on macOS, WebView2 on Windows,
   WebKitGTK on Linux). This is what the packaged build ships with.
2. **Chrome/Edge in app mode** -- ``--app=URL`` gives a chromeless window that
   looks and behaves like a desktop app, with no address bar.
3. **The default browser** -- always works.

The fallback chain matters: a packaged app must never fail to open just because
one GUI toolkit is missing. Whatever happens, the URL is printed so the user can
open it by hand.

The window and the server share a process, so closing the window shuts the
server down and releases the port.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config
from .server.app import create_server
from .server.http import find_free_port
from .util import log
from .version import APP_TITLE, __version__

LOG = log.get("desktop")

WINDOW_WIDTH = 1440
WINDOW_HEIGHT = 940
MIN_WIDTH = 1040
MIN_HEIGHT = 680


def _banner(url: str, token: str) -> None:
    line = "\u2500" * 62
    print(f"\n{line}\n {APP_TITLE}  v{__version__}\n{line}")
    print(f" Open in a browser:  {url}")
    print(f" Session token:      {token}")
    print(f" Close the window (or press Ctrl+C here) to stop.\n{line}\n")


# --------------------------------------------------------------------------- #
# Window strategies
# --------------------------------------------------------------------------- #

def _try_pywebview(url: str, on_close) -> bool:
    try:
        import webview  # type: ignore
    except Exception as exc:
        LOG.info("pywebview not available (%s); trying a browser window.", exc)
        return False
    try:
        window = webview.create_window(
            APP_TITLE, url, width=WINDOW_WIDTH, height=WINDOW_HEIGHT,
            min_size=(MIN_WIDTH, MIN_HEIGHT), resizable=True,
            text_select=True, confirm_close=False,
        )
        try:
            window.events.closed += on_close
        except Exception:  # pragma: no cover - older pywebview
            pass
        # gui=None lets pywebview pick the best available backend.
        webview.start(debug=bool(os.environ.get("BOMIQ_DEBUG")))
        return True
    except Exception as exc:
        LOG.warning("pywebview could not open a window (%s); "
                    "falling back to a browser.", exc)
        return False


def _chrome_candidates() -> list[str]:
    system = platform.system()
    if system == "Darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    if system == "Windows":
        program_files = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        relative = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"Chromium\Application\chrome.exe",
        ]
        return [str(Path(base) / rel) for base in program_files if base
                for rel in relative]
    return [
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("microsoft-edge") or "",
        shutil.which("brave-browser") or "",
    ]


def _try_app_mode(url: str, profile_dir: Path) -> subprocess.Popen | None:
    """Open a chromeless Chrome/Edge window (``--app=``)."""
    for candidate in _chrome_candidates():
        if not candidate or not Path(candidate).exists():
            continue
        try:
            process = subprocess.Popen(
                [
                    candidate,
                    f"--app={url}",
                    f"--user-data-dir={profile_dir}",
                    f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=Translate,MediaRouter",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            LOG.info("Opened an app window with %s", Path(candidate).name)
            return process
        except Exception as exc:
            LOG.debug("Could not launch %s: %s", candidate, exc)
    return None


def _try_default_browser(url: str) -> bool:
    try:
        import webbrowser

        return webbrowser.open(url)
    except Exception as exc:  # pragma: no cover
        LOG.warning("Could not open a browser: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(port: int = 0, open_file: str | None = None,
         data_dir: str | None = None, config_dir: str | None = None,
         headless: bool = False) -> int:
    config = Config(data_dir=data_dir, config_dir=config_dir)
    log.setup(config.log_dir, config.settings.log_level, console=True)

    host = "127.0.0.1"
    chosen_port = port or find_free_port(host, config.settings.port)
    server = create_server(config, host=host, port=chosen_port)
    url = server.app_url
    server.start_background()
    _banner(url, server.token)

    if open_file:
        path = Path(open_file).expanduser()
        if path.is_file():
            url = f"{url}&open={path}"
        else:
            print(f"Note: {path} was not found, starting with an empty "
                  f"workspace.")

    stopping = threading.Event()

    def shutdown(*_: Any) -> None:
        stopping.set()

    if headless:
        print("Running headless: the UI is available at the URL above.")
        try:
            while not stopping.is_set():
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass
        server.stop()
        server.state["engine"].close()
        return 0

    profile_dir = config.data_dir / "window-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # 1) native window (blocks until closed)
    if _try_pywebview(url, shutdown):
        server.stop()
        server.state["engine"].close()
        return 0

    # 2) chromeless browser window
    process = _try_app_mode(url, profile_dir)
    if process is not None:
        try:
            process.wait()
        except KeyboardInterrupt:
            process.terminate()
        server.stop()
        server.state["engine"].close()
        return 0

    # 3) default browser, and keep serving until interrupted
    opened = _try_default_browser(url)
    if not opened:
        print("Could not open a window automatically. Open the URL above in "
              "your browser.")
    print("Press Ctrl+C to stop the server.")
    try:
        while not stopping.is_set():
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.stop()
        server.state["engine"].close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
