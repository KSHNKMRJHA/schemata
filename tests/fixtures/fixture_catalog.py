"""Re-export of the demo catalog from the app package (kept for tests).

The canonical module lives at app.sources.demo_catalog so popular toolchains
(Nuitka/PyInstaller) ship it with the frozen binary.
"""

from app.sources.demo_catalog import (  # noqa: F401
    CATALOG,
    DemoOffer,
    DemoPart,
    all_mpns,
    get,
)
