# kb_guardrails.py
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Set, Tuple

from fastapi import Depends, APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, String, text
from sqlalchemy.orm import Session

from db import Base

log = logging.getLogger("vozlia.control.kb_guardrails")

# -------------------------
# Optional worker heartbeat table
# -------------------------

class KbWorkerHeartbeat(Base):
    __tablename__ = "kb_worker_heartbeat"

    # Worker instance id (hostname/pid or Render instance id)
    id = Column(String, primary_key=True)
    # Last time worker reported liveness (UTC)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


# -------------------------
# Types
# -------------------------

RequireAdminFn = Callable[..., bool]
GetDbDep = Callable[..., Any]

_EXPECTED_FILE_ROUTES: List[Tuple[str, str]] = [
    ("/admin/kb/files", "GET"),
    ("/admin/kb/files/upload-token", "POST"),
    ("/admin/kb/files/{file_id}", "GET"),
    ("/admin/kb/files/{file_id}", "DELETE"),
    ("/admin/kb/files/{file_id}/download-token", "GET"),
    ("/admin/kb/files/{file_id}/download", "GET"),
    ("/kb/upload", "POST"),
    ("/kb/download", "GET"),
]

_EXPECTED_INGEST_ROUTES: List[Tuple[str, str]] = [
    ("/admin/kb/files/{file_id}/ingest", "POST"),
    ("/admin/kb/files/{file_id}/ingest-status", "GET"),
    ("/admin/kb/ingest-jobs", "GET"),
]


def _route_map(app: Any) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for r in getattr(app, "router", None).routes:  # type: ignore[attr-defined]
        path = getattr(r, "path", "") or ""
        methods = set(getattr(r, "methods", None) or [])
        if not path:
            continue
        out.setdefault(path, set()).update(methods)
    return out


def _missing(routes: Dict[str, Set[str]], expected: List[Tuple[str, str]]) -> List[str]:
    missing = []
    for path, method in expected:
        ms = routes.get(path, set())
        if method not in ms:
            missing.append(f"{method} {path}")
    return missing


def _env_bool(name: str) -> bool:
    return (os.getenv(name, "") or "").strip() not in ("", "0", "false", "False")


def register_kb_guardrails_routes(
    *,
    app: Any,
    require_admin: RequireAdminFn,
    get_db: GetDbDep,
) -> None:
    router = APIRouter(prefix="/admin/kb", tags=["kb"])

    class KbHealthOut(BaseModel):
        ok: bool = True

        kb_files_enabled: bool = True
        kb_ingest_enabled: bool = False

        routes: Dict[str, Any] = Field(default_factory=dict)
        storage: Dict[str, Any] = Field(default_factory=dict)
        db: Dict[str, Any] = Field(default_factory=dict)
        worker: Dict[str, Any] = Field(default_factory=dict)

        now_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @router.get("/health", response_model=KbHealthOut)
    def kb_health(
        request: Request,
        admin_ok: bool = Depends(require_admin),
        db: Session = Depends(get_db),
    ) -> KbHealthOut:
        routes = _route_map(request.app)

        missing_file = _missing(routes, _EXPECTED_FILE_ROUTES)
        missing_ingest = _missing(routes, _EXPECTED_INGEST_ROUTES)

        # Storage config — booleans only (never return secrets)
        storage = {
            "bucket_set": bool((os.getenv("KB_S3_BUCKET", "") or "").strip()),
            "prefix_set": bool((os.getenv("KB_S3_PREFIX", "") or "").strip()),
            "endpoint_set": bool((os.getenv("KB_S3_ENDPOINT_URL", "") or "").strip()),
            "region_set": bool((os.getenv("KB_S3_REGION", "") or "").strip()),
            "access_key_set": bool((os.getenv("KB_S3_ACCESS_KEY_ID", "") or "").strip()),
            "secret_key_set": bool((os.getenv("KB_S3_SECRET_ACCESS_KEY", "") or "").strip()),
            "kb_token_secret_set": bool((os.getenv("KB_TOKEN_SECRET", "") or "").strip()),
        }

        # DB: table existence
        def regclass(name: str) -> bool:
            try:
                val = db.execute(text("SELECT to_regclass(:t)"), {"t": f"public.{name}"}).scalar()
                return val is not None
            except Exception:
                return False

        db_state = {
            "kb_files_table": regclass("kb_files"),
            "kb_ingest_jobs_table": regclass("kb_ingest_jobs"),
            "kb_chunks_table": regclass("kb_chunks"),
            "kb_worker_heartbeat_table": regclass("kb_worker_heartbeat"),
        }

        # Worker heartbeat (best-effort)
        worker_state: Dict[str, Any] = {"heartbeat_ok": None, "last_heartbeat_utc": None, "age_s": None}
        try:
            row = db.execute(text("SELECT MAX(updated_at) FROM kb_worker_heartbeat")).scalar()
            if row is not None:
                now = datetime.now(timezone.utc)
                # row may be naive depending on driver; normalize
                if getattr(row, "tzinfo", None) is None:
                    row = row.replace(tzinfo=timezone.utc)
                age_s = (now - row).total_seconds()
                max_age = int(os.getenv("KB_WORKER_HEARTBEAT_MAX_AGE_S", "60") or "60")
                worker_state = {
                    "heartbeat_ok": age_s <= max_age,
                    "last_heartbeat_utc": row.isoformat(),
                    "age_s": round(age_s, 2),
                    "max_age_s": max_age,
                }
        except Exception:
            # swallow; health must stay up
            pass

        files_enabled = os.getenv("KB_FILES_ENABLED", "1") == "1"
        ingest_enabled = os.getenv("KB_INGEST_ENABLED", "0") == "1"

        ok = True
        # If enabled, require presence
        if files_enabled and missing_file:
            ok = False
        if ingest_enabled and missing_ingest:
            ok = False

        return KbHealthOut(
            ok=ok,
            kb_files_enabled=files_enabled,
            kb_ingest_enabled=ingest_enabled,
            routes={
                "file_missing": missing_file,
                "ingest_missing": missing_ingest,
            },
            storage=storage,
            db=db_state,
            worker=worker_state,
        )

    app.include_router(router)


def kb_startup_selfcheck(app: Any) -> None:
    """
    Optional hard guardrail:
    - If KB_REQUIRE_FILE_ROUTES=1 and file routes are missing -> raise (fail deploy)
    - If KB_REQUIRE_INGEST_ROUTES=1 and ingest routes are missing -> raise (fail deploy)
    """
    routes = _route_map(app)

    missing_file = _missing(routes, _EXPECTED_FILE_ROUTES)
    missing_ingest = _missing(routes, _EXPECTED_INGEST_ROUTES)

    require_files = os.getenv("KB_REQUIRE_FILE_ROUTES", "0") == "1"
    require_ingest = os.getenv("KB_REQUIRE_INGEST_ROUTES", "0") == "1"

    if require_files and missing_file:
        raise RuntimeError(f"KB file routes missing: {missing_file}")
    if require_ingest and missing_ingest:
        raise RuntimeError(f"KB ingest routes missing: {missing_ingest}")

    if missing_file:
        log.error("KB file routes missing (will 404): %s", missing_file)
    if missing_ingest:
        log.warning("KB ingest routes missing: %s", missing_ingest)
