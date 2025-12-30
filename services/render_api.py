# services/render_api.py
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

RENDER_API_BASE = "https://api.render.com"


class RenderAPIError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Render API error {status}: {body[:200]}")
        self.status = status
        self.body = body


def _auth_headers() -> Dict[str, str]:
    token = (os.getenv("RENDER_API_KEY") or "").strip()
    if not token:
        raise RuntimeError("RENDER_API_KEY not configured")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "vozlia-control-plane/1.0",
    }


def render_get_json(path: str, params: Optional[Dict[str, str]] = None, *, timeout_s: float = 15.0) -> Any:
    """
    Minimal Render API client using stdlib urllib (keeps deps small).
    Raises RenderAPIError for non-2xx responses.
    """
    if not path.startswith("/"):
        path = "/" + path

    qs = ""
    if params:
        # Remove empty params to avoid confusing Render API
        cleaned = {k: v for k, v in params.items() if v is not None and str(v) != ""}
        qs = "?" + urllib.parse.urlencode(cleaned, doseq=True)

    url = f"{RENDER_API_BASE}{path}{qs}"
    req = urllib.request.Request(url, headers=_auth_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return None
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RenderAPIError(getattr(e, "code", 500) or 500, body) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Render API network error: {e}") from None


def ms_to_rfc3339(ms: int) -> str:
    # Render API uses RFC3339 timestamps (with Z).
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def rfc3339_to_ms(ts: str) -> Optional[int]:
    if not ts:
        return None
    try:
        import datetime as _dt
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None
