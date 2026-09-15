"""Test harness: isolate on a temporary SQLite database."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="part_intel_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
# Keep tests deterministic: never hit live distributor APIs during tests.
os.environ["MOUSER_API_KEY"] = ""
os.environ["DIGIKEY_CLIENT_ID"] = ""
os.environ["DIGIKEY_CLIENT_SECRET"] = ""
os.environ["NEXAR_CLIENT_ID"] = ""
os.environ["NEXAR_CLIENT_SECRET"] = ""

import pytest  # noqa: E402

from app.db import init_db  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _db():
    init_db()
    yield
