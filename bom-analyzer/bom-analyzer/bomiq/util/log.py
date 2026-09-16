"""Logging setup with rotating file output and secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(?i)(client[_-]?secret|api[_-]?key|access[_-]?token|"
               r"refresh[_-]?token|authorization|password|bearer)"
               r"(['\"]?\s*[:=]\s*['\"]?)([^\s'\",&}]+)"),
    re.compile(r"(?i)Bearer\s+([A-Za-z0-9\-._~+/=]{12,})"),
]

_configured = False


class RedactingFilter(logging.Filter):
    """Strip anything that looks like a credential out of log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def redact(text: str) -> str:
    """Replace credential-looking substrings with ``***``."""
    if not text:
        return text
    out = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
        else:
            out = pattern.sub("Bearer ***", out)
    return out


def setup(log_dir: Path | None = None, level: str | int = "INFO",
          console: bool = True) -> logging.Logger:
    """Configure the ``bomiq`` logger. Safe to call repeatedly."""
    global _configured
    logger = logging.getLogger("bomiq")
    if _configured:
        return logger

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = RedactingFilter()

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "bomiq.log", maxBytes=4_000_000, backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            logger.addHandler(file_handler)
        except OSError:  # pragma: no cover - read-only install dir
            logger.warning("Could not open log file in %s", log_dir)

    _configured = True
    return logger


def get(name: str) -> logging.Logger:
    """Return a child logger, configuring the root lazily if needed."""
    if not _configured:
        setup(console=os.environ.get("BOMIQ_QUIET") != "1")
    return logging.getLogger(f"bomiq.{name}")
