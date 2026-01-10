# kb_tokens.py
"""
Signed token utilities for KB uploads/downloads.

Why:
- The browser must never receive the Control Plane admin key.
- For large file uploads/downloads, we want the browser to talk directly to the Control Plane
  (not through Vercel serverless proxies).
- A short-lived signed token allows this safely.

Token format:
  base64url(json_payload) + "." + base64url(hmac_sha256(payload_bytes, secret))

Payload must include:
  - op: "upload" | "download"
  - exp: unix epoch seconds
  - tenant_id: str
Additional fields are allowed (e.g., file_id, filename, kind, content_type).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict


class KBTokenError(Exception):
    pass


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("utf-8"))


def _get_secret() -> str:
    # Prefer a dedicated secret; fall back to ADMIN_API_KEY for convenience.
    secret = (os.getenv("KB_TOKEN_SECRET") or "").strip()
    if secret:
        return secret
    secret = (os.getenv("ADMIN_API_KEY") or "").strip()
    if not secret:
        raise KBTokenError("KB_TOKEN_SECRET (or ADMIN_API_KEY fallback) is not configured")
    return secret


def mint_token(payload: Dict[str, Any], *, ttl_seconds: int = 15 * 60, secret: str | None = None) -> str:
    """
    Returns a signed token string.

    ttl_seconds is added to current time if payload does not include "exp".
    """
    secret = secret or _get_secret()
    now = int(time.time())
    exp = int(payload.get("exp") or (now + int(ttl_seconds)))
    body = dict(payload)
    body["exp"] = exp

    payload_bytes = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def verify_token(token: str, *, secret: str | None = None) -> Dict[str, Any]:
    """
    Verifies signature + exp, returns decoded payload dict.

    Raises KBTokenError on any failure.
    """
    secret = secret or _get_secret()
    if not token or "." not in token:
        raise KBTokenError("Invalid token format")

    part_payload, part_sig = token.split(".", 1)
    payload_bytes = _b64url_decode(part_payload)
    sig_bytes = _b64url_decode(part_sig)

    expected = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig_bytes):
        raise KBTokenError("Invalid token signature")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        raise KBTokenError("Invalid token payload") from e

    exp = int(payload.get("exp") or 0)
    now = int(time.time())
    if exp <= now:
        raise KBTokenError("Token expired")

    return payload
