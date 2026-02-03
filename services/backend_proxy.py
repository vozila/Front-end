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
from typing import Any, Optional, Dict

import httpx
from fastapi import HTTPException

log = logging.getLogger("vozlia")


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

    headers = {
        "X-Vozlia-Admin-Key": admin_key,
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.request(method.upper(), url, headers=headers, json=json_body, params=params)
    except Exception as e:
        log.exception("BACKEND_PROXY_REQUEST_FAILED method=%s url=%s", method, url)
        raise HTTPException(status_code=502, detail=f"Backend request failed: {method} {url}: {e}") from e

    if resp.status_code < 200 or resp.status_code >= 300:
        # Try to include JSON error details if possible
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:1000]

        # IMPORTANT:
        # The control plane proxies should *pass through* backend HTTP errors (422/401/etc),
        # not convert them into control-plane 500s.
        log.warning("BACKEND_PROXY_NON_2XX method=%s url=%s status=%s detail=%s", method, url, resp.status_code, str(detail)[:500])
        raise HTTPException(status_code=resp.status_code, detail=detail)

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
