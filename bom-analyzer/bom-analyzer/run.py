#!/usr/bin/env python3
"""
Launch BOM-IQ as a desktop app.

    python run.py                # open the desktop window
    python run.py --headless     # just serve; open the printed URL yourself
    python run.py BOM.xlsx       # start with a file loaded
    python run.py --cli ...      # anything after --cli goes to the CLI

This is the file the packaged executable wraps, and the one to double-click
when running from source.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the app importable when run from a checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] == "--cli":
        from bomiq.cli import main as cli_main

        return cli_main(argv[1:])

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    headless = "--headless" in argv
    argv = [a for a in argv if a != "--headless"]

    port = 0
    if "--port" in argv:
        index = argv.index("--port")
        if index + 1 < len(argv):
            try:
                port = int(argv[index + 1])
            except ValueError:
                print(f"Ignoring invalid port {argv[index + 1]!r}")
            del argv[index:index + 2]

    open_file = next((a for a in argv if not a.startswith("-")), None)

    from bomiq.desktop import main as desktop_main

    return desktop_main(port=port, open_file=open_file, headless=headless)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
