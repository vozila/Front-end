"""VOZLIA FILE PURPOSE
Purpose: Per-request context (request_id/trace_id) propagated via contextvars for logging correlation.
Hot path: yes (HTTP middleware), but constant-time and tiny allocations only.
Public interfaces: set_request_id, reset_request_id, get_request_id.
Reads/Writes: none (in-memory context vars only).
Feature flags: none (always on; safe default "-").
Failure mode: never raises; defaults to "-".
Last touched: 2026-02-03 (add request_id context for debugging deterministic DB issues)
"""

from __future__ import annotations

from contextvars import ContextVar, Token

# Default "-" keeps log format stable even outside HTTP request contexts.
_request_id_var: ContextVar[str] = ContextVar("vozlia_request_id", default="-")


def set_request_id(request_id: str) -> Token:
    """Set request id for current context; returns token for reset."""
    rid = (request_id or "").strip() or "-"
    return _request_id_var.set(rid)


def reset_request_id(token: Token) -> None:
    """Reset request id to previous value using token from set_request_id()."""
    try:
        _request_id_var.reset(token)
    except Exception:
        # Safe-by-default: never raise during cleanup.
        return


def get_request_id() -> str:
    """Get current request id for this context."""
    try:
        return _request_id_var.get()
    except Exception:
        return "-"
