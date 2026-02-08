# services/backend_proxy.py
"""
Thin HTTP client for Vozlia backend.

Design goals:
- Keep control plane as the only surface exposed to the web UI.
- Forward admin auth via the existing X-Vozlia-Admin-Key header.
- Fail loudly but with actionable errors.
"""
from __future__ import annotations

import os
import logging
import time
from typing import Any, Optional, Dict

import httpx

from core.request_context import get_request_id
from core.logging import env_flag
class BackendProxyError(RuntimeError):
    """Raised when the backend returns a non-2xx response."""

    def __init__(self, *, status_code: int, method: str, url: str, detail: Any):
        super().__init__(f"Backend error {status_code} for {method} {url}: {detail}")
        self.status_code = status_code
        self.method = method
        self.url = url
        self.detail = detail


log = logging.getLogger("vozlia")

def _proxy_debug_enabled() -> bool:
    # Common debug gate (inherits VOZLIA_DEBUG if BACKEND_PROXY_DEBUG is unset).
    return env_flag("BACKEND_PROXY_DEBUG", "0", inherit_debug=True)


def _proxy_debug_prefixes() -> list[str]:
    raw = (os.getenv("BACKEND_PROXY_DEBUG_PREFIXES") or "/admin/dbquery,/admin/metrics,/admin/websearch").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def _backend_base_url() -> str:
    # Prefer explicit env var, but fall back to the standard Render URL for convenience.
    base = (
        os.getenv("VOZLIA_BACKEND_URL")
        or os.getenv("BACKEND_URL")
        or os.getenv("VOZLIA_BACKEND_BASE_URL")
        or os.getenv("BACKEND_BASE_URL")
        or "https://vozlia-backend.onrender.com"
    )
    return base.rstrip("/")


def backend_request(
    method: str,
    path: str,
    *,
    admin_key: str,
    json_body: Any | None = None,
    params: Dict[str, Any] | None = None,
    timeout_s: float = 30.0,
) -> Any:
    """
    Performs an authenticated request to the backend and returns JSON-decoded content.

    Raises:
        RuntimeError on non-2xx responses.
    """
    base = _backend_base_url()
    url = f"{base}{path if path.startswith('/') else '/' + path}"

    prefixes = _proxy_debug_prefixes()
    debug = _proxy_debug_enabled() and any(path.startswith(p) for p in prefixes)
    t0 = time.perf_counter()

    headers = {
        "X-Vozlia-Admin-Key": admin_key,
        "Accept": "application/json",
        # Correlation across Control -> Backend.
        "X-Vozlia-Request-Id": get_request_id(),
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    if debug:
        try:
            log.info(
                "BACKEND_PROXY_CALL method=%s path=%s url=%s params=%s has_body=%s",
                method.upper(),
                path,
                url,
                (sorted(list((params or {}).keys())) if isinstance(params, dict) else None),
                bool(json_body is not None),
            )
        except Exception:
            pass

    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.request(method.upper(), url, headers=headers, json=json_body, params=params)
    except Exception as e:
        raise RuntimeError(f"Backend request failed: {method} {url}: {e}") from e

    dt_ms = (time.perf_counter() - t0) * 1000.0
    if debug:
        try:
            log.info(
                "BACKEND_PROXY_RESP status=%s ms=%.1f ctype=%s",
                resp.status_code,
                dt_ms,
                (resp.headers.get('content-type') or ''),
            )
        except Exception:
            pass

    if resp.status_code < 200 or resp.status_code >= 300:
        # Try to include JSON error details if possible
        detail = None
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:1000]
        raise BackendProxyError(status_code=resp.status_code, method=method, url=url, detail=detail)

    # Some endpoints return empty bodies; normalize to {}.
    if not resp.content:
        return {}

    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        return resp.json()

    # Fallback: return text.
    return {"text": resp.text}


def backend_get(path: str, *, admin_key: str, params: Dict[str, Any] | None = None) -> Any:
    return backend_request("GET", path, admin_key=admin_key, params=params)


def backend_post(path: str, *, admin_key: str, json_body: Any | None = None) -> Any:
    return backend_request("POST", path, admin_key=admin_key, json_body=json_body)


def backend_delete(path: str, *, admin_key: str, params: Dict[str, Any] | None = None) -> Any:
    return backend_request("DELETE", path, admin_key=admin_key, params=params)
