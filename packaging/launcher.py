"""Schemata launcher — a small native (tkinter) start window.

Shows what the app is, how to use it, then "Start" boots the local
intelligence server (no console window) and opens the browser. Supports
--port, --no-browser and --quit-after-seconds for automated smoke tests.
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import font as tkfont

from app.config import DATA_DIR

DEFAULT_PORT = 8750
HOST = "127.0.0.1"

_LOG_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Schemata" / "launcher.log"


def _log(msg: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:  # noqa: BLE001 - logging must never crash the launcher
        pass


BG = "#0d1117"
PANEL = "#121a24"
LINE = "#1f2a39"
TEXT = "#dce3ec"
MUTED = "#5b6b80"
BRASS = "#d9a441"
OK = "#5cb85c"

HOW_TO = (
    "How it works:\n"
    "• Search a part number e.g. STM32F407VGT6 — lifecycle status, distributor\n"
    "  stock & pricing, and engineering risk are gathered from live APIs when\n"
    "  keys are set (see the .env file) or from the built-in demo catalog.\n"
    "• Open a part for the full dossier: life-cycle timeline, price/stock\n"
    "  history charts, risk breakdown and replacement candidates.\n"
    "• Upload any BOM (.csv / .xlsx / .txt) for a line-by-line procurement\n"
    "  risk report, then export JSON / CSV / HTML.\n"
    "• All data is cached locally; nothing leaves your machine except direct\n"
    "  look-ups to Mouser / DigiKey when keys are configured."
)


class Launcher:
    def __init__(self, port: int, open_browser: bool, quit_after: float | None, auto_start: bool = False):
        self.port = port
        self.open_browser = open_browser
        self.quit_after = quit_after
        self.auto_start = auto_start
        self.server = None
        self.online = False
        self._starting = False
        self.url = f"http://{HOST}:{port}"

        self.root = tk.Tk()
        self.root.title("Schemata — Component Intelligence Platform")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        if Path(__file__).resolve().parent.joinpath("icon.ico").exists():
            self.root.iconbitmap(str(Path(__file__).resolve().parent / "icon.ico"))
        self._fonts()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        if self.auto_start:
            self.root.after(400, self._toggle)

    def _fonts(self) -> None:
        self.f_title = tkfont.Font(family="Segoe UI", size=26, weight="bold")
        self.f_sub = tkfont.Font(family="Segoe UI", size=10)
        self.f_body = tkfont.Font(family="Segoe UI", size=10)
        self.f_mono = tkfont.Font(family="Consolas", size=10)

    def _build(self) -> None:
        pad = tk.Frame(self.root, bg=PANEL, padx=28, pady=20)
        pad.pack(fill="both", expand=True)

        tk.Label(pad, text="Schemata", bg=PANEL, fg=BRASS, font=self.f_title).pack(anchor="w")
        tk.Label(pad, text="Component intelligence platform · local server", bg=PANEL, fg=MUTED, font=self.f_sub).pack(
            anchor="w", pady=(0, 14)
        )

        info = f"  URL        {self.url}\n  Port       {self.port}\n  Data dir   {DATA_DIR}"
        tk.Label(pad, text=info, bg=PANEL, fg=TEXT, font=self.f_mono, justify="left", anchor="w").pack(
            fill="x", pady=(0, 12)
        )

        tk.Label(pad, text=HOW_TO, bg=PANEL, fg=TEXT, font=self.f_body, justify="left", anchor="w").pack(
            fill="x", pady=(0, 16)
        )

        self.status = tk.StringVar(value="Idle — press Start to launch the server.")
        tk.Label(pad, textvariable=self.status, bg=PANEL, fg=MUTED, font=self.f_body, anchor="w").pack(
            fill="x", pady=(0, 12)
        )

        buttons = tk.Frame(pad, bg=PANEL)
        buttons.pack(fill="x")
        self.start_btn = tk.Button(
            buttons,
            text="Start Schemata",
            command=self._toggle,
            bg=BRASS,
            fg="#11151c",
            activebackground="#e8b95c",
            activeforeground="#11151c",
            relief="flat",
            font=self.f_body,
            padx=18,
            pady=8,
            cursor="hand2",
            bd=0,
        )
        self.start_btn.pack(side="left")
        tk.Button(
            buttons,
            text="Quit",
            command=self.quit,
            bg=PANEL,
            fg=TEXT,
            activebackground=LINE,
            activeforeground=TEXT,
            relief="flat",
            font=self.f_body,
            padx=18,
            pady=8,
            cursor="hand2",
            bd=0,
            highlightthickness=1,
            highlightbackground=LINE,
        ).pack(side="right")

    def _toggle(self) -> None:
        if self._starting:
            return
        running = self.server is not None and self.server.started and not self.server.should_exit
        if running:
            self.server.should_exit = True
            self.start_btn.configure(state="disabled", text="Stopping…")
            self.status.set("Stopping server…")
        else:
            self._start()

    def _start(self) -> None:
        if self._starting:
            return
        self._starting = True
        self.start_btn.configure(state="disabled", text="Starting…")
        self.status.set("Starting server… (first run may seed the demo catalog)")
        threading.Thread(target=self._serve, daemon=True).start()
        self.root.after(200, self._poll)
        if self.quit_after:
            self.root.after(int(self.quit_after * 1000), self.quit)

    def _serve(self) -> None:
        _log(f"[{os.getpid()}] server thread starting on port {self.port}")
        try:
            from app.ensure_demo import ensure_demo_seed
            from app.main import app as dash_app

            ensure_demo_seed()
            import uvicorn

            config = uvicorn.Config(dash_app, host=HOST, port=self.port, log_level="warning")
            self.server = uvicorn.Server(config)
            self.server.run()
        except Exception:  # noqa: BLE001 - surface startup failures to the log
            traceback.print_exc()
            _log(f"[{os.getpid()}] SERVER ERROR: {traceback.format_exc()}")

    def _poll(self) -> None:
        srv = self.server
        if srv is not None and srv.started:
            if srv.should_exit:  # teardown in progress
                self._starting = False
                self.online = False
                self.status.set("Server stopped.")
                self.start_btn.configure(state="normal", text="Start Schemata")
                self.server = None
            else:
                self.online = True
                self._starting = False
                self.status.set(f"Running at {self.url} — server stays up until you stop it.")
                self.start_btn.configure(state="normal", text="Stop server")
                if self.open_browser:
                    try:
                        webbrowser.open(self.url)
                    except Exception:  # noqa: BLE001 - browser absence is non-fatal
                        pass
            return
        self.root.after(200, self._poll)

    def quit(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        self.root.destroy()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int, max_tries: int = 50) -> int:
    for cand in range(preferred, preferred + max_tries):
        if _port_free(cand):
            return cand
    raise RuntimeError(f"No free port found near {preferred}")


def _serve_headless(port: int) -> None:
    """No-window mode used by smoke tests and user automation."""
    _log(f"[{os.getpid()}] headless server on port {port}")
    try:
        from app.ensure_demo import ensure_demo_seed
        from app.main import app as dash_app

        ensure_demo_seed()
        import uvicorn

        config = uvicorn.Config(dash_app, host=HOST, port=port, log_level="warning")
        uvicorn.Server(config).run()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        _log(f"[{os.getpid()}] HEADLESS SERVER ERROR: {traceback.format_exc()}")
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description="Schemata launcher")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--quit-after-seconds", type=float, default=None, help="auto-quit after N seconds (smoke tests)")
    ap.add_argument("--auto-start", action="store_true", help="start the server without clicking (smoke tests)")
    ap.add_argument(
        "--serve-only", action="store_true", help="run the server with no window (smoke tests / automation)"
    )
    args = ap.parse_args()
    port = _pick_port(args.port)
    if args.serve_only:
        _serve_headless(port)
        return
    Launcher(port, not args.no_browser, args.quit_after_seconds, args.auto_start).root.mainloop()


if __name__ == "__main__":
    main()
