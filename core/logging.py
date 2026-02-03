"""VOZLIA FILE PURPOSE
Purpose: Centralized logging setup for Vozlia backend services (Render/Uvicorn friendly).
Hot path: yes (log calls everywhere), but constant-time.
Public interfaces: logger (app logger), LEVEL (effective log level).
Reads/Writes: stdout only.
Feature flags: LOG_LEVEL, LOG_FORMAT.
Failure mode: never raises; falls back to safe defaults.
Last touched: 2026-02-03 (add request_id context filter for better debugging)
"""

# core/logging.py
from __future__ import annotations

import logging
import os
import sys

from core.request_context import get_request_id


def _level() -> int:
    name = (os.getenv("LOG_LEVEL") or "INFO").upper().strip()
    return getattr(logging, name, logging.INFO)


def _fmt() -> str:
    # Keep stable default; include request_id for correlation.
    return (os.getenv("LOG_FORMAT") or "%(levelname)s:%(name)s:rid=%(request_id)s:%(message)s").strip()


LEVEL = _level()
FMT = _fmt()


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Ensure attribute exists so formatter never crashes.
        try:
            record.request_id = get_request_id()
        except Exception:
            record.request_id = "-"
        return True


# Root logger: make sure something prints to stdout
root = logging.getLogger()
root.setLevel(LEVEL)

# Ensure at least one stdout handler exists (Render captures stdout)
handler = None
if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(LEVEL)
    handler.setFormatter(logging.Formatter(FMT))
    root.addHandler(handler)

# Attach request_id filter to all handlers (safe no-op if formatter doesn't use it).
for h in root.handlers:
    try:
        h.addFilter(_RequestIdFilter())
        # If we created the handler above, ensure formatter uses our configured format.
        if h is handler:
            h.setFormatter(logging.Formatter(FMT))
    except Exception:
        pass

# Vozlia app logger
logger = logging.getLogger("vozlia")
logger.setLevel(LEVEL)
logger.propagate = True
