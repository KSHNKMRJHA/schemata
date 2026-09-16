"""
BOM-IQ -- a local, offline-first BOM analysis and part-intelligence engine.

The package is deliberately built on the Python standard library only. Optional
third-party accelerators (openpyxl, xlrd, rapidfuzz, pywebview) are detected at
runtime and used when present; every code path has a stdlib fallback so the
application is fully functional on a bare Python install.

Public entry points
-------------------
``bomiq.engine.Engine``        -- the analysis engine (programmatic API)
``bomiq.cli.main``             -- command line interface
``bomiq.desktop.main``         -- desktop application launcher
``bomiq.server.app.serve``     -- local HTTP API + UI
"""

from .version import __version__, APP_NAME, APP_TITLE

__all__ = ["__version__", "APP_NAME", "APP_TITLE"]
