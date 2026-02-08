"""VOZLIA FILE PURPOSE
Purpose: Centralized logging setup for Vozlia services (Render/Uvicorn friendly) with request-id correlation.
Hot path: yes (log calls everywhere), but constant-time (no DB/network).
Public interfaces: logger, LEVEL, is_debug(), env_flag().
Reads/Writes: stdout only.
Feature flags: LOG_LEVEL, LOG_FORMAT, UVICORN_ACCESS_LOG, VOZLIA_DEBUG.
Failure mode: never raises; falls back to safe defaults.
Last touched: 2026-02-08 (VOZLIA_DEBUG gate helpers + optional uvicorn access-log suppression)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from core.request_context import get_request_id


def _truthy(v: Optional[str]) -> bool:
    s = (v or "").strip().lower()
    return s in ("1", "true", "yes", "on")


def is_debug() -> bool:
    """Common debug gate used across Vozlia repos.

    Contract:
    - If VOZLIA_DEBUG is truthy, debug breadcrumbs are enabled by default.
    - Hot-path heavy logs MUST remain behind their own specific flags (e.g., REALTIME_LOG_*).
    """
    return _truthy(os.getenv("VOZLIA_DEBUG"))


def env_flag(name: str, default: str = "0", *, inherit_debug: bool = False) -> bool:
    """Parse an env var as a boolean.

    If inherit_debug=True and the env var is not set, VOZLIA_DEBUG is used as the default.
    This lets you flip one switch (VOZLIA_DEBUG=1) and get consistent observability,
    while still allowing per-flag overrides.
    """
    raw = os.getenv(name)
    if raw is None and inherit_debug:
        raw = os.getenv("VOZLIA_DEBUG")
    if raw is None:
        raw = default
    return _truthy(raw)


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


# Optional: suppress extremely noisy Uvicorn access logs (Render log readability).
# Enable by setting UVICORN_ACCESS_LOG=0 (or false/off). Default keeps access logs on.
try:
    _access_on = (os.getenv("UVICORN_ACCESS_LOG", "1") or "1").strip().lower()
    if _access_on in ("0", "false", "no", "off"):
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
except Exception:
    pass


# Vozlia app logger
logger = logging.getLogger("vozlia")
logger.setLevel(LEVEL)
logger.propagate = True
