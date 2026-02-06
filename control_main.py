"""
Vozlia Control Plane (FastAPI)
- Admin settings + email account admin endpoints
- Render proxy helpers for Portal UI (services, instances, logs preview, logs export)

Notes:
- Avoid shadowing `datetime` (Pydantic needs datetime type, not module).
- Render /v1/logs uses timestamp pagination (hasMore + nextStartTime/nextEndTime); we do NOT pass `limit` upstream.
"""

from __future__ import annotations

import os
import json
import re
import logging
import time
import random
import urllib.error
import socket
import datetime as dt
from datetime import datetime, timedelta
from typing import List, Optional, Iterator, Any, Dict

# Default prompts (UI wiring). Keep stable unless you intentionally change behavior.
DEFAULT_GMAIL_SUMMARY_LLM_PROMPT = (
    "You are Vozlia. Given email metadata (subject, sender, snippet, date), "
    "produce a VERY short spoken-style summary (1–3 sentences). "
    "Do NOT read email addresses or long codes out loud."
)

from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request, Body
from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, load_only
from sqlalchemy import text

from deps import get_db
from db import Base, engine
from core.security import encrypt_str
from models import EmailAccount
from services.user_service import get_or_create_primary_user
from services.backend_proxy import backend_get, backend_post, backend_delete
from services.config_wizard_service import WizardTurnIn, WizardTurnOut, run_wizard_turn
from services.settings_service import (
    get_agent_greeting,
    gmail_summary_enabled,
    get_selected_gmail_account_id,
    get_realtime_prompt_addendum,
    get_enabled_gmail_account_ids,
    set_setting,
    set_enabled_gmail_account_ids,
    # NEW
    get_skills_config,
    patch_skill_config,
    get_skills_priority_order,
    set_skills_priority_order,
    shortterm_memory_enabled,
    longterm_memory_enabled,
    get_memory_engagement_phrases,
)

from services.render_api import render_get_json, ms_to_rfc3339, rfc3339_to_ms, RenderAPIError

from models import EmailAccount
from admin_memory import build_memory_router




logger = logging.getLogger("vozlia.control")
DEBUG_RENDER_LOGS = os.getenv("VOZLIA_DEBUG_RENDER_LOGS", "0") == "1"
NY_TZ = ZoneInfo("America/New_York")


# =========================
# AUTH
# =========================

def require_admin_key(
    x_vozlia_admin_key: str = Header(default="", alias="X-Vozlia-Admin-Key"),
    x_admin_key: str = Header(default="", alias="x-admin-key"),
    x_vozlia_trace: str = Header(default="", alias="X-Vozlia-Trace"),
) -> bool:
    expected = (os.getenv("ADMIN_API_KEY", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")

    provided = (x_vozlia_admin_key or "").strip() or (x_admin_key or "").strip()
    if (not provided) or (provided != expected):
        if DEBUG_RENDER_LOGS:
            logger.warning("ADMIN_AUTH_FAIL trace=%s", (x_vozlia_trace or "").strip() or None)
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


# =========================
# APP
# =========================

app = FastAPI(title="Vozlia Control")

# -------------------------
# CORS (needed for browser -> Control Plane uploads via /kb/upload)
# -------------------------
CONTROL_CORS_ORIGIN_REGEX = os.getenv("CONTROL_CORS_ORIGIN_REGEX", "").strip()
if (CONTROL_CORS_ORIGIN_REGEX.startswith('"') and CONTROL_CORS_ORIGIN_REGEX.endswith('"')) or (
    CONTROL_CORS_ORIGIN_REGEX.startswith("'") and CONTROL_CORS_ORIGIN_REGEX.endswith("'")
):
    CONTROL_CORS_ORIGIN_REGEX = CONTROL_CORS_ORIGIN_REGEX[1:-1].strip()

if CONTROL_CORS_ORIGIN_REGEX:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=CONTROL_CORS_ORIGIN_REGEX,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Vozlia-Trace"],
        max_age=600,
    )

# -------------------------
# KB Phase 1: File management routes (list/upload-token/upload/download/delete)
# -------------------------
if os.getenv("KB_FILES_ENABLED", "1") == "1":
    try:
        from admin_kb_files import build_kb_router
        app.include_router(build_kb_router(require_admin_key))
        logger.info("KB file routes registered")
    except Exception:
        # Fail-open: keep control plane online, but make it very obvious uploads will 404
        logger.exception("KB file routes failed to register; upload/list endpoints disabled")

# -------------------------
# KB Phase 2: ingestion routes (enqueue/status/jobs)
# -------------------------
if os.getenv("KB_INGEST_ENABLED", "0") == "1":
    try:
        from kb_ingest import register_kb_ingest_routes
        register_kb_ingest_routes(app, require_admin=require_admin_key, get_db=get_db)
        logger.info("KB ingest routes registered")
    except Exception:
        # Fail-open: Control Plane should still boot even if ingest is misconfigured
        logger.exception("KB ingest routes failed to register; ingestion endpoints disabled")

# Admin Memory (long-term memory table + delete) for WebUI debugging
app.include_router(build_memory_router(require_admin_key))


# -------------------------
# KB Phase 3: Q&A/query routes (retrieve chunks + optional LLM answer)
# -------------------------
if os.getenv("KB_QA_ENABLED", "1") == "1":
    try:
        from kb_query import register_kb_query_routes
        register_kb_query_routes(app, require_admin=require_admin_key, get_db=get_db)
        logger.info("KB query routes registered")
    except Exception:
        # Fail-open: keep control plane online, but make it obvious KB Q&A will 404/503
        logger.exception("KB query routes failed to register; KB Q&A endpoint disabled")

# -------------------------
# KB Guardrails: /admin/kb/health + optional startup selfcheck
# -------------------------

_KB_EXPECTED_FILE_ROUTES = [
    ("/admin/kb/files", "GET"),
    ("/admin/kb/files/upload-token", "POST"),
    ("/admin/kb/files/{file_id}", "GET"),
    ("/admin/kb/files/{file_id}", "DELETE"),
    ("/admin/kb/files/{file_id}/download-token", "GET"),
    ("/admin/kb/files/{file_id}/download", "GET"),
    ("/kb/upload", "POST"),
    ("/kb/download", "GET"),
]

_KB_EXPECTED_INGEST_ROUTES = [
    ("/admin/kb/files/{file_id}/ingest", "POST"),
    ("/admin/kb/files/{file_id}/ingest-status", "GET"),
    ("/admin/kb/ingest-jobs", "GET"),
]


def _kb_route_map() -> Dict[str, set]:
    out: Dict[str, set] = {}
    for r in app.router.routes:
        path = getattr(r, "path", "") or ""
        if not path:
            continue
        methods = set(getattr(r, "methods", None) or [])
        out.setdefault(path, set()).update(methods)
    return out


def _kb_missing(expected: List[tuple]) -> List[str]:
    routes = _kb_route_map()
    missing: List[str] = []
    for path, method in expected:
        ms = routes.get(path, set())
        if method not in ms:
            missing.append(f"{method} {path}")
    return missing


def _kb_env_is_1(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip() == "1"


class KbHealthOut(BaseModel):
    ok: bool = True

    kb_files_enabled: bool = True
    kb_ingest_enabled: bool = False

    routes: Dict[str, Any] = Field(default_factory=dict)
    storage: Dict[str, Any] = Field(default_factory=dict)
    db: Dict[str, Any] = Field(default_factory=dict)
    worker: Dict[str, Any] = Field(default_factory=dict)

    now_utc: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())


@app.get("/admin/kb/health", response_model=KbHealthOut)
def kb_health(
    _admin_ok: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
) -> KbHealthOut:
    files_enabled = os.getenv("KB_FILES_ENABLED", "1") == "1"
    ingest_enabled = os.getenv("KB_INGEST_ENABLED", "0") == "1"

    missing_file = _kb_missing(_KB_EXPECTED_FILE_ROUTES) if files_enabled else []
    missing_ingest = _kb_missing(_KB_EXPECTED_INGEST_ROUTES) if ingest_enabled else []

    # Storage config booleans (never return actual secrets)
    storage = {
        "bucket_set": bool((os.getenv("KB_S3_BUCKET", "") or "").strip()),
        "prefix_set": bool((os.getenv("KB_S3_PREFIX", "") or "").strip()),
        "endpoint_set": bool((os.getenv("KB_S3_ENDPOINT_URL", "") or "").strip()),
        "region_set": bool((os.getenv("KB_S3_REGION", "") or "").strip()),
        "access_key_set": bool((os.getenv("KB_S3_ACCESS_KEY_ID", "") or "").strip()),
        "secret_key_set": bool((os.getenv("KB_S3_SECRET_ACCESS_KEY", "") or "").strip()),
        "kb_token_secret_set": bool((os.getenv("KB_TOKEN_SECRET", "") or "").strip()),
    }
    storage_required_ok = (
        storage["bucket_set"]
        and storage["prefix_set"]
        and storage["access_key_set"]
        and storage["secret_key_set"]
        and storage["kb_token_secret_set"]
    )

    # DB table existence
    def table_exists(name: str) -> bool:
        try:
            val = db.execute(text("SELECT to_regclass(:t)"), {"t": f"public.{name}"}).scalar()
            return val is not None
        except Exception:
            return False

    db_state = {
        "kb_files_table": table_exists("kb_files"),
        "kb_ingest_jobs_table": table_exists("kb_ingest_jobs"),
        "kb_chunks_table": table_exists("kb_chunks"),
        "kb_worker_heartbeat_table": table_exists("kb_worker_heartbeat"),
    }

    # Worker heartbeat (best-effort)
    worker_state: Dict[str, Any] = {"heartbeat_ok": None, "last_heartbeat_utc": None, "age_s": None}
    if db_state["kb_worker_heartbeat_table"]:
        try:
            row = db.execute(text("SELECT MAX(updated_at) FROM kb_worker_heartbeat")).scalar()
            if row is not None:
                now = dt.datetime.now(dt.timezone.utc)
                if getattr(row, "tzinfo", None) is None:
                    row = row.replace(tzinfo=dt.timezone.utc)
                age_s = (now - row).total_seconds()
                max_age = int(os.getenv("KB_WORKER_HEARTBEAT_MAX_AGE_S", "60") or "60")
                worker_state = {
                    "heartbeat_ok": age_s <= max_age,
                    "last_heartbeat_utc": row.isoformat(),
                    "age_s": round(age_s, 2),
                    "max_age_s": max_age,
                }
        except Exception:
            pass

    ok = True
    if files_enabled and missing_file:
        ok = False
    if ingest_enabled and missing_ingest:
        ok = False

    # Config sanity: if KB files are enabled, storage + token secret should be set
    if files_enabled and not storage_required_ok:
        ok = False

    # If ingest is enabled, the job/chunk tables should exist
    if ingest_enabled and (not db_state["kb_ingest_jobs_table"] or not db_state["kb_chunks_table"]):
        ok = False

    # Optionally require ingest worker heartbeat when ingest is enabled
    if ingest_enabled and _kb_env_is_1("KB_REQUIRE_INGEST_WORKER", "0"):
        if worker_state.get("heartbeat_ok") is not True:
            ok = False

    return KbHealthOut(
        ok=ok,
        kb_files_enabled=files_enabled,
        kb_ingest_enabled=ingest_enabled,
        routes={"file_missing": missing_file, "ingest_missing": missing_ingest},
        storage={**storage, "required_ok": storage_required_ok},
        db=db_state,
        worker=worker_state,
    )


def _kb_startup_selfcheck() -> None:
    """Optional hard guardrail: fail deploy if expected KB routes are missing."""
    files_enabled = os.getenv("KB_FILES_ENABLED", "1") == "1"
    ingest_enabled = os.getenv("KB_INGEST_ENABLED", "0") == "1"

    missing_file = _kb_missing(_KB_EXPECTED_FILE_ROUTES) if files_enabled else []
    missing_ingest = _kb_missing(_KB_EXPECTED_INGEST_ROUTES) if ingest_enabled else []

    require_files = _kb_env_is_1("KB_REQUIRE_FILE_ROUTES", "0")
    require_ingest = _kb_env_is_1("KB_REQUIRE_INGEST_ROUTES", "0")

    if missing_file:
        logger.error("KB file routes missing (uploads/list will 404): %s", missing_file)
    if missing_ingest and ingest_enabled:
        logger.warning("KB ingest routes missing: %s", missing_ingest)

    if require_files and missing_file:
        raise RuntimeError(f"KB file routes missing: {missing_file}")
    if require_ingest and missing_ingest:
        raise RuntimeError(f"KB ingest routes missing: {missing_ingest}")


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace = request.headers.get("x-vozlia-trace") or request.headers.get("X-Vozlia-Trace")
    t0 = time.monotonic()
    try:
        resp = await call_next(request)
    except Exception:
        if DEBUG_RENDER_LOGS and request.url.path.startswith("/admin/render"):
            logger.exception("RENDER_ADMIN_REQUEST_EXCEPTION trace=%s path=%s", trace, request.url.path)
        raise
    dt_ms = (time.monotonic() - t0) * 1000.0
    if trace:
        resp.headers["X-Vozlia-Trace"] = trace
    if DEBUG_RENDER_LOGS and request.url.path.startswith("/admin/render"):
        logger.info(
            "RENDER_ADMIN %s %s status=%s ms=%.1f trace=%s",
            request.method,
            request.url.path,
            getattr(resp, "status_code", None),
            dt_ms,
            trace,
        )
    return resp


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)

    # Optional: fail deploy if expected KB routes are missing
    try:
        _kb_startup_selfcheck()
    except Exception:
        logger.exception("KB startup selfcheck failed")
        if _kb_env_is_1("KB_REQUIRE_FILE_ROUTES", "0") or _kb_env_is_1("KB_REQUIRE_INGEST_ROUTES", "0"):
            raise


@app.get("/health")
def health() -> str:
    return "OK"


# =========================
# SETTINGS
# =========================

# SETTINGS
# =========================

class SkillConfig(BaseModel):
    enabled: bool = True
    add_to_greeting: bool = False
    auto_execute_after_greeting: bool = False
    engagement_phrases: List[str] = Field(default_factory=list)
    llm_prompt: str = ""

    # Optional per-skill fields (used by some skills, e.g. investment_reporting)
    tickers: List[str] = Field(default_factory=list)
    tickers_raw: Optional[str] = None


class AdminSettingsOut(BaseModel):
    # Legacy / current
    agent_greeting: str
    gmail_summary_enabled: bool
    gmail_account_id: Optional[str] = None
    gmail_enabled_account_ids: Optional[List[str]] = None
    realtime_prompt_addendum: str

    # New (modular)
    skills_config: Dict[str, SkillConfig] = Field(default_factory=dict)

    # Memory (migrated from env vars)
    shortterm_memory_enabled: bool = True
    longterm_memory_enabled: bool = False
    memory_engagement_phrases: List[str] = Field(default_factory=list)


class AdminSettingsPatch(BaseModel):
    # Legacy / current
    skills_priority_order: Optional[List[str]] = None
    agent_greeting: str | None = Field(default=None, min_length=1, max_length=500)
    gmail_summary_enabled: bool | None = None
    gmail_account_id: str | None = None
    gmail_enabled_account_ids: List[str] | None = None
    realtime_prompt_addendum: str | None = Field(default=None, min_length=1, max_length=4000)

    # New (modular)
    skills_config: Dict[str, SkillConfig] | None = None

    # Memory
    shortterm_memory_enabled: bool | None = None
    longterm_memory_enabled: bool | None = None
    memory_engagement_phrases: List[str] | None = None


# Pydantic v2 + postponed annotations: ensure models are fully built before use
try:
    SkillConfig.model_rebuild()
except Exception:
    pass
try:
    AdminSettingsOut.model_rebuild()
except Exception:
    pass
try:
    AdminSettingsPatch.model_rebuild()
except Exception:
    pass


@app.get("/admin/settings", response_model=AdminSettingsOut, dependencies=[Depends(require_admin_key)])
def get_settings(db: Session = Depends(get_db)):
    user = get_or_create_primary_user(db)
    skills = get_skills_config(db, user)

    # Back-compat + defaults: ensure gmail_summary config is fully populated and enabled mirrors legacy toggle.
    gmail_enabled = gmail_summary_enabled(db, user)

    gmail_defaults = {
        "enabled": bool(gmail_enabled),
        "add_to_greeting": False,
        "engagement_phrases": ["email summaries"],
        "llm_prompt": DEFAULT_GMAIL_SUMMARY_LLM_PROMPT,
    }
    existing = skills.get("gmail_summary") if isinstance(skills, dict) else None
    if isinstance(existing, dict):
        merged = {**gmail_defaults, **existing}
    else:
        merged = dict(gmail_defaults)

    merged["enabled"] = bool(gmail_enabled)
    skills["gmail_summary"] = merged

    return AdminSettingsOut(
        agent_greeting=get_agent_greeting(db, user),
        gmail_summary_enabled=gmail_enabled,
        gmail_account_id=get_selected_gmail_account_id(db, user),
        gmail_enabled_account_ids=get_enabled_gmail_account_ids(db, user),
        realtime_prompt_addendum=get_realtime_prompt_addendum(db, user),
        skills_config=skills,
        shortterm_memory_enabled=shortterm_memory_enabled(db, user),
        longterm_memory_enabled=longterm_memory_enabled(db, user),
        memory_engagement_phrases=get_memory_engagement_phrases(db, user),
    )


@app.patch("/admin/settings", response_model=AdminSettingsOut, dependencies=[Depends(require_admin_key)])
def patch_settings(payload: AdminSettingsPatch, db: Session = Depends(get_db)):
    user = get_or_create_primary_user(db)
    data = payload.model_dump(exclude_none=True)

    if "agent_greeting" in data:
        set_setting(db, user, "agent_greeting", {"text": data["agent_greeting"].strip()})

    if "gmail_summary_enabled" in data:
        # legacy toggle
        set_setting(db, user, "gmail_summary_enabled", {"enabled": bool(data["gmail_summary_enabled"])})
        # also mirror into skills_config.gmail_summary.enabled for future unification
        patch_skill_config(db, user, "gmail_summary", {"enabled": bool(data["gmail_summary_enabled"])})

    if "gmail_account_id" in data:
        set_setting(db, user, "gmail_account_id", {"account_id": data["gmail_account_id"].strip()})

    if "gmail_enabled_account_ids" in data:
        set_enabled_gmail_account_ids(db, user, data["gmail_enabled_account_ids"])

    if "realtime_prompt_addendum" in data:
        set_setting(db, user, "realtime_prompt_addendum", {"text": data["realtime_prompt_addendum"].strip()})

    # New modular skill config
    if "skills_config" in data and isinstance(data["skills_config"], dict):
        for sid, cfg in data["skills_config"].items():
            if not isinstance(sid, str) or not isinstance(cfg, dict):
                continue
            patch_skill_config(db, user, sid, cfg)

    # Memory toggles
    if "shortterm_memory_enabled" in data:
        set_setting(db, user, "shortterm_memory_enabled", {"enabled": bool(data["shortterm_memory_enabled"])})

    if "longterm_memory_enabled" in data:
        set_setting(db, user, "longterm_memory_enabled", {"enabled": bool(data["longterm_memory_enabled"])})

    if "memory_engagement_phrases" in data:
        phrases = data.get("memory_engagement_phrases") or []
        cleaned = [str(x).strip() for x in (phrases if isinstance(phrases, list) else []) if str(x).strip()]
        set_setting(db, user, "memory_engagement_phrases", {"phrases": cleaned})

    return get_settings(db)


# =========================



# =========================
# EMAIL ACCOUNTS (ADMIN)
# =========================

class EmailAccountOut(BaseModel):
    id: str
    user_id: str

    provider_type: str
    oauth_provider: Optional[str] = None

    email_address: str
    display_name: Optional[str] = None

    is_primary: bool
    is_active: bool

    # Optional connection metadata (non-secret)
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_ssl: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_ssl: Optional[bool] = None
    username: Optional[str] = None

    created_at: datetime
    updated_at: datetime


class EmailAccountPatch(BaseModel):
    is_active: Optional[bool] = None
    is_primary: Optional[bool] = None
    display_name: Optional[str] = Field(default=None, max_length=200)


class GmailUpsertRequest(BaseModel):
    email_address: str = Field(..., min_length=3, max_length=320)
    display_name: Optional[str] = Field(default=None, max_length=200)
    oauth_access_token: str = Field(..., min_length=10)
    oauth_refresh_token: Optional[str] = Field(default=None)
    expires_in: Optional[int] = Field(default=None, ge=0)


def _to_email_out(a: EmailAccount) -> EmailAccountOut:
    return EmailAccountOut(
        id=str(a.id),
        user_id=str(a.user_id),
        provider_type=a.provider_type,
        oauth_provider=a.oauth_provider,
        email_address=a.email_address,
        display_name=a.display_name,
        is_primary=bool(a.is_primary),
        is_active=bool(a.is_active),
        imap_host=a.imap_host,
        imap_port=a.imap_port,
        imap_ssl=a.imap_ssl,
        smtp_host=a.smtp_host,
        smtp_port=a.smtp_port,
        smtp_ssl=a.smtp_ssl,
        username=a.username,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )



# -----------------------------------------------------------------------------
# EmailAccount schema drift guard (admin endpoints)
# -----------------------------------------------------------------------------
# In production we've seen DB/schema drift where sensitive token columns get renamed
# (e.g. `access_token_enc` vs `oauth_access_token`). The Admin Portal listing/toggles
# should NOT depend on token columns at all.
#
# To keep /admin/email-accounts and PATCH toggles working even if token columns are
# missing, we only SELECT the non-secret fields used by EmailAccountOut.
#
# NOTE: We also use a scoped refresh (attribute_names=...) because SQLAlchemy expires
# instances on commit by default (expire_on_commit=True). A full refresh would re-select
# missing columns and re-trigger a 500.
_EMAIL_ACCOUNT_PUBLIC_FIELDS = (
    "id",
    "user_id",
    "provider_type",
    "oauth_provider",
    "email_address",
    "display_name",
    "is_primary",
    "is_active",
    "imap_host",
    "imap_port",
    "imap_ssl",
    "smtp_host",
    "smtp_port",
    "smtp_ssl",
    "username",
    "created_at",
    "updated_at",
)

# Precompute existing mapped attributes (defensive across code versions)
_EMAIL_ACCOUNT_PUBLIC_ATTR_NAMES = [n for n in _EMAIL_ACCOUNT_PUBLIC_FIELDS if hasattr(EmailAccount, n)]


def _email_account_public_query(db: Session):
    q = db.query(EmailAccount)
    cols = []
    for name in _EMAIL_ACCOUNT_PUBLIC_FIELDS:
        attr = getattr(EmailAccount, name, None)
        if attr is not None:
            cols.append(attr)
    if cols:
        q = q.options(load_only(*cols))
    return q


def _refresh_email_account_public(db: Session, a: EmailAccount) -> None:
    # SQLAlchemy 2.x supports attribute_names to scope the refresh query.
    try:
        db.refresh(a, attribute_names=_EMAIL_ACCOUNT_PUBLIC_ATTR_NAMES)
    except Exception:
        # Last resort (should be avoided if schema drift exists)
        db.refresh(a)

@app.get("/admin/email-accounts", response_model=List[EmailAccountOut], dependencies=[Depends(require_admin_key)])
def list_email_accounts(
    include_inactive: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    user = get_or_create_primary_user(db)
    q = _email_account_public_query(db).filter(EmailAccount.user_id == user.id)
    if not include_inactive:
        q = q.filter(EmailAccount.is_active == True)  # noqa: E712
    rows = q.order_by(EmailAccount.created_at.desc()).all()
    return [_to_email_out(r) for r in rows]


@app.post(
    "/admin/email-accounts/gmail/upsert",
    response_model=EmailAccountOut,
    dependencies=[Depends(require_admin_key)],
)
def upsert_gmail_account(payload: GmailUpsertRequest, db: Session = Depends(get_db)):
    """Create or update a Gmail/Google OAuth EmailAccount for the primary user."""
    user = get_or_create_primary_user(db)

    email_address = payload.email_address.strip().lower()
    if not email_address:
        raise HTTPException(status_code=400, detail="email_address is required")

    a = (
        _email_account_public_query(db)
        .filter(
            EmailAccount.user_id == user.id,
            EmailAccount.provider_type == "gmail",
            EmailAccount.oauth_provider == "google",
            EmailAccount.email_address == email_address,
        )
        .first()
    )

    if not a:
        a = EmailAccount(
            user_id=user.id,
            provider_type="gmail",
            oauth_provider="google",
            email_address=email_address,
            display_name=payload.display_name or email_address,
            is_primary=False,
            is_active=True,
        )
        db.add(a)
        db.flush()

    # Always update access token
    a.oauth_access_token = encrypt_str(payload.oauth_access_token)

    # Update refresh token only if provided; otherwise preserve existing
    if payload.oauth_refresh_token:
        a.oauth_refresh_token = encrypt_str(payload.oauth_refresh_token)

    if payload.expires_in is not None:
        a.oauth_expires_at = datetime.utcnow() + timedelta(seconds=int(payload.expires_in))

    if payload.display_name:
        a.display_name = payload.display_name

    a.is_active = True
    a.updated_at = datetime.utcnow()

    # Ensure there's a primary
    any_primary = (
        db.query(EmailAccount)
        .filter(EmailAccount.user_id == user.id, EmailAccount.is_primary == True)  # noqa: E712
        .count()
        > 0
    )
    if not any_primary:
        a.is_primary = True

    db.commit()
    _refresh_email_account_public(db, a)
    return _to_email_out(a)


@app.patch("/admin/email-accounts/{account_id}", response_model=EmailAccountOut, dependencies=[Depends(require_admin_key)])
def patch_email_account(account_id: str, payload: EmailAccountPatch, db: Session = Depends(get_db)):
    user = get_or_create_primary_user(db)
    a = _email_account_public_query(db).filter(EmailAccount.id == account_id, EmailAccount.user_id == user.id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Email account not found")

    data = payload.model_dump(exclude_none=True)

    if "display_name" in data:
        a.display_name = (data["display_name"] or "").strip() or None

    if "is_active" in data:
        a.is_active = bool(data["is_active"])

    if data.get("is_primary") is True:
        db.query(EmailAccount).filter(
            EmailAccount.user_id == user.id,
            EmailAccount.id != a.id,
        ).update({"is_primary": False})
        a.is_primary = True

    db.commit()
    _refresh_email_account_public(db, a)
    return _to_email_out(a)


@app.delete("/admin/email-accounts/{account_id}", dependencies=[Depends(require_admin_key)])
def delete_email_account(
    account_id: str,
    hard: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """Disconnect an email account."""
    user = get_or_create_primary_user(db)
    a = _email_account_public_query(db).filter(EmailAccount.id == account_id, EmailAccount.user_id == user.id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Email account not found")

    if hard:
        db.delete(a)
        db.commit()
        return {"status": "deleted", "hard": True}

    a.is_active = False
    a.is_primary = False
    a.oauth_access_token = None
    a.oauth_refresh_token = None
    a.oauth_expires_at = None
    a.password_enc = None
    db.commit()
    return {"status": "disconnected", "hard": False}


# =========================
# RENDER LOGS (Portal helper)
# =========================

class RenderServiceOut(BaseModel):
    id: str
    name: str | None = None
    type: str | None = None
    owner_id: str | None = None
    region: str | None = None


class RenderInstanceOut(BaseModel):
    id: str
    service_id: str | None = None
    status: str | None = None
    started_at: str | None = None


class RenderLogRow(BaseModel):
    ts: str | None = None            # display timestamp (we emit Eastern)
    level: str | None = None
    msg: str | None = None
    raw: str                         # required by portal UI


class RenderLogsOut(BaseModel):
    service_id: str
    instance_id: str | None = None
    start_ms: int
    end_ms: int
    rows: List[RenderLogRow]
    has_more: bool = False
    next_start_ms: int | None = None
    next_end_ms: int | None = None


def _render_owner_id() -> str | None:
    return (os.getenv("RENDER_OWNER_ID") or "").strip() or None


def _iso_to_est(iso_ts: str) -> str:
    try:
        dtu = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        local = dtu.astimezone(NY_TZ)
        return local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return iso_ts


def _extract_level_from_labels(labels: Any) -> str | None:
    # Render provides labels like [{name:"level", value:"INFO"}, ...]
    if not isinstance(labels, list):
        return None
    for lab in labels:
        if isinstance(lab, dict) and str(lab.get("name", "")).lower() == "level":
            v = lab.get("value")
            return str(v) if v is not None else None
    return None


def _render_get_json_with_backoff(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: float | None = None,
    max_retries: int = 5,
    base_sleep_s: float = 0.6,
):
    """Call Render API with exponential backoff on 429/5xx and transient network errors.

    Retries are bounded and add jitter to avoid thundering herds.
    """
    attempt = 0
    while True:
        try:
            if timeout_s is None:
                return render_get_json(path, params=params)
            return render_get_json(path, params=params, timeout_s=timeout_s)

        except RenderAPIError as e:
            status = getattr(e, "status", None)
            body = getattr(e, "body", None)
            retryable = (status == 429) or (isinstance(status, int) and 500 <= status <= 599)
            if (not retryable) or attempt >= max_retries:
                raise

            sleep_s = base_sleep_s * (2**attempt) + random.uniform(0.0, 0.25)
            if DEBUG_RENDER_LOGS:
                logger.warning(
                    "RENDER_BACKOFF status=%s attempt=%s sleep_s=%.2f body=%s",
                    status,
                    attempt + 1,
                    sleep_s,
                    body,
                )
            time.sleep(sleep_s)
            attempt += 1
            continue

        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            # Treat transient transport errors as retryable (common under upstream throttling).
            if attempt >= max_retries:
                raise
            sleep_s = base_sleep_s * (2**attempt) + random.uniform(0.0, 0.25)
            if DEBUG_RENDER_LOGS:
                logger.warning(
                    "RENDER_BACKOFF transport_error attempt=%s sleep_s=%.2f err=%s",
                    attempt + 1,
                    sleep_s,
                    str(e),
                )
            time.sleep(sleep_s)
            attempt += 1
            continue
@app.get(
    "/admin/render/services",
    response_model=List[RenderServiceOut],
    dependencies=[Depends(require_admin_key)],
)
def admin_render_list_services(limit: int = Query(default=100, ge=1, le=200)):
    # Render max limit is 100; clamp any UI requests above that.
    if limit > 100:
        if DEBUG_RENDER_LOGS:
            logger.info("RENDER_LIST_SERVICES clamp_limit requested=%s clamped=100", limit)
        limit = 100

    def _parse_services(data: Any) -> List[RenderServiceOut]:
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("services") or []
        else:
            items = []

        out: List[RenderServiceOut] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            svc = it.get("service") if isinstance(it.get("service"), dict) else it
            sid = str(svc.get("id") or "")
            if not sid:
                continue
            out.append(
                RenderServiceOut(
                    id=sid,
                    name=svc.get("name"),
                    type=svc.get("type"),
                    owner_id=svc.get("ownerId") or svc.get("owner_id"),
                    region=svc.get("region"),
                )
            )
        return out

    params: dict[str, Any] = {"limit": str(limit)}
    owner_id = (_render_owner_id() or "").strip()
    if owner_id:
        params["ownerId"] = owner_id

    try:
        data = _render_get_json_with_backoff("/v1/services", params=params)
        return _parse_services(data)
    except RenderAPIError as e:
        logger.error(
            "RENDER_LIST_SERVICES upstream_error status=%s owner_id=%s body=%s",
            getattr(e, "status", None),
            owner_id or None,
            getattr(e, "body", None),
        )
        # If ownerId filter is misconfigured, retry once without it.
        if owner_id and getattr(e, "status", None) == 400:
            data2 = _render_get_json_with_backoff("/v1/services", params={"limit": str(limit)})
            return _parse_services(data2)
        raise HTTPException(status_code=getattr(e, "status", 502), detail=getattr(e, "body", "upstream_error"))
    except Exception as ex:
        logger.exception("RENDER_LIST_SERVICES exception")
        raise HTTPException(status_code=500, detail=str(ex))


@app.get(
    "/admin/render/services/{service_id}/instances",
    response_model=List[RenderInstanceOut],
    dependencies=[Depends(require_admin_key)],
)
def admin_render_list_instances(service_id: str):
    try:
        data = _render_get_json_with_backoff(f"/v1/services/{service_id}/instances", params={"limit": "50"})
    except RenderAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    items = data if isinstance(data, list) else ((data or {}).get("instances") if isinstance(data, dict) else [])
    items = items or []

    out: List[RenderInstanceOut] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "")
        if not iid:
            continue
        out.append(
            RenderInstanceOut(
                id=iid,
                service_id=it.get("serviceId") or it.get("service_id") or service_id,
                status=it.get("status"),
                started_at=it.get("startedAt") or it.get("started_at"),
            )
        )
    return out


@app.get("/admin/render/logs", response_model=RenderLogsOut, dependencies=[Depends(require_admin_key)])
def admin_render_logs(
    service_id: str = Query(...),
    instance_id: str | None = Query(default=None),
    start_ms: int = Query(..., ge=0),
    end_ms: int = Query(..., ge=0),
    q: str | None = Query(default=None),
    limit: int = Query(default=300, ge=1, le=2000),
):
    """
    Logs preview for Portal.

    IMPORTANT:
    - Do NOT pass `limit` to Render /v1/logs (timestamp pagination).
    - Treat empty instance_id/q as None (portal sends empty strings).
    - Always return 200 with rows:[] if no logs.
    - Emit timestamps in America/New_York.
    """
    if end_ms <= start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be > start_ms")

    owner_id = _render_owner_id()
    if not owner_id:
        raise HTTPException(status_code=500, detail="RENDER_OWNER_ID not configured")

    instance_id = (instance_id or "").strip() or None
    q = (q or "").strip() or None

    params: dict[str, Any] = {
        "ownerId": owner_id,
        "startTime": ms_to_rfc3339(start_ms),
        "endTime": ms_to_rfc3339(end_ms),
        "direction": "backward",
        "resource": [service_id],
    }
    if instance_id:
        params["instance"] = [instance_id]
    if q:
        params["text"] = [q]

    try:
        data = _render_get_json_with_backoff("/v1/logs", params=params)
    except RenderAPIError as e:
        # Fail-soft for preview: if Render is flaky, show empty rows instead of breaking UI.
        body = getattr(e, "body", "") or ""
        status = getattr(e, "status", 502)
        # Fail-soft on rate limit for preview so the table still renders.
        if status == 429:
            msg = "Render rate limit exceeded. Please wait a few seconds and try again."
            if DEBUG_RENDER_LOGS:
                logger.warning("RENDER_LOGS preview_rate_limited status=429 body=%s", body)
            return RenderLogsOut(
                service_id=service_id,
                instance_id=instance_id,
                start_ms=start_ms,
                end_ms=end_ms,
                rows=[RenderLogRow(ts=None, level="ERROR", msg=msg, raw=msg)],
                has_more=False,
            )
        if status >= 500:
            if DEBUG_RENDER_LOGS:
                logger.warning("RENDER_LOGS preview_failsoft status=%s body=%s", status, body)
            return RenderLogsOut(service_id=service_id, instance_id=instance_id, start_ms=start_ms, end_ms=end_ms, rows=[])
        raise HTTPException(status_code=status, detail=body or "upstream_error")
    except Exception as e:
        # Fail-soft for preview: keep UI functional even if Render/network is flaky.
        msg = f"Render logs preview error: {e}"
        if DEBUG_RENDER_LOGS:
            logger.exception("RENDER_LOGS preview_exception")
        return RenderLogsOut(
            service_id=service_id,
            instance_id=instance_id,
            start_ms=start_ms,
            end_ms=end_ms,
            rows=[RenderLogRow(ts=None, level="ERROR", msg=msg, raw=msg)],
            has_more=False,
        )

    logs = []
    has_more = False
    next_start = None
    next_end = None

    if isinstance(data, dict):
        logs = data.get("logs") or []
        has_more = bool(data.get("hasMore") or False)
        next_start = data.get("nextStartTime")
        next_end = data.get("nextEndTime")

    rows: List[RenderLogRow] = []
    for entry in (logs or [])[:limit]:
        if not isinstance(entry, dict):
            raw = str(entry)
            rows.append(RenderLogRow(ts=None, level=None, msg=raw, raw=raw))
            continue

        msg = str(entry.get("message") or "")
        ts_utc = str(entry.get("timestamp") or "")
        lvl = _extract_level_from_labels(entry.get("labels"))

        rows.append(
            RenderLogRow(
                ts=_iso_to_est(ts_utc) if ts_utc else None,
                level=lvl,
                msg=msg or None,
                raw=msg,
            )
        )

    return RenderLogsOut(
        service_id=service_id,
        instance_id=instance_id,
        start_ms=start_ms,
        end_ms=end_ms,
        rows=rows,
        has_more=has_more,
        next_start_ms=rfc3339_to_ms(next_start) if next_start else None,
        next_end_ms=rfc3339_to_ms(next_end) if next_end else None,
    )

@app.get("/admin/render/logs/export", dependencies=[Depends(require_admin_key)])
def admin_render_logs_export(
    service_id: str = Query(...),
    instance_id: Optional[str] = Query(default=None),
    start_ms: int = Query(..., ge=0),
    end_ms: int = Query(..., ge=0),
    q: Optional[str] = Query(default=None),
    format: str = Query(default="csv", description="csv|text|ndjson"),
):
    """
    Export logs.

    - format=csv: CSV with columns: ts, level, msg, raw
    - format=text: plain text lines: "<ts> <level> <msg>"
    - format=ndjson: JSON per line: {"ts":"...","level":"...","msg":"...","raw":"..."}

    We page using Render's hasMore + nextStartTime/nextEndTime.
    We do NOT send `limit` upstream (Render /v1/logs is timestamp-paginated).
    """
    if end_ms <= start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be > start_ms")

    owner_id = _render_owner_id()
    if not owner_id:
        raise HTTPException(status_code=500, detail="RENDER_OWNER_ID not configured")

    instance_id = (instance_id or "").strip() or None
    q = (q or "").strip() or None
    fmt = (format or "csv").strip().lower()
    if fmt not in ("csv", "text", "ndjson"):
        raise HTTPException(status_code=400, detail="format must be csv, text, or ndjson")

    safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", service_id)[:80]
    filename = (
        f"render-logs_{safe_name}_{int((end_ms-start_ms)/60000)}min_"
        f"{datetime.utcnow().isoformat(timespec='seconds')}Z."
        f"{'csv' if fmt == 'csv' else ('ndjson' if fmt == 'ndjson' else 'log')}"
    )

    def _csv_escape(v: Any) -> str:
        s = "" if v is None else str(v)
        if any(c in s for c in [",", '"', "\n", "\r"]):
            return '"' + s.replace('"', '""') + '"'
        return s

    def _iter_export() -> Iterator[bytes]:
        if fmt == "csv":
            yield b"ts,level,msg,raw\n"

        params_base: dict[str, Any] = {
            "ownerId": owner_id,
            "direction": "backward",
            "resource": [service_id],
        }
        if instance_id:
            params_base["instance"] = [instance_id]
        if q:
            params_base["text"] = [q]

        cur_start = start_ms
        cur_end = end_ms
        safety_pages = 0

        while True:
            safety_pages += 1
            if safety_pages > 30:
                msg = "[export truncated: too many pages]"
                if fmt == "csv":
                    yield (",".join([_csv_escape(None), _csv_escape("WARN"), _csv_escape(msg), _csv_escape(msg)]) + "\n").encode("utf-8")
                elif fmt == "ndjson":
                    yield (json.dumps({"ts": None, "level": "WARN", "msg": msg, "raw": msg}) + "\n").encode("utf-8")
                else:
                    yield (msg + "\n").encode("utf-8")
                break

            params = dict(params_base)
            params["startTime"] = ms_to_rfc3339(cur_start)
            params["endTime"] = ms_to_rfc3339(cur_end)

            try:
                data = _render_get_json_with_backoff("/v1/logs", params=params, timeout_s=30.0)
            except RenderAPIError as e:
                err = f"[export error] Render API error {getattr(e, 'status', None)}: {getattr(e, 'body', '')}"
                if fmt == "csv":
                    yield (",".join([_csv_escape(None), _csv_escape("ERROR"), _csv_escape(err), _csv_escape(err)]) + "\n").encode("utf-8")
                elif fmt == "ndjson":
                    yield (json.dumps({"ts": None, "level": "ERROR", "msg": err, "raw": err}) + "\n").encode("utf-8")
                else:
                    yield (err + "\n").encode("utf-8")
                break
            except Exception as e:
                err = f"[export error] {e}"
                if fmt == "csv":
                    yield (",".join([_csv_escape(None), _csv_escape("ERROR"), _csv_escape(err), _csv_escape(err)]) + "\n").encode("utf-8")
                elif fmt == "ndjson":
                    yield (json.dumps({"ts": None, "level": "ERROR", "msg": err, "raw": err}) + "\n").encode("utf-8")
                else:
                    yield (err + "\n").encode("utf-8")
                break

            logs = []
            has_more = False
            next_start = None
            next_end = None

            if isinstance(data, dict):
                logs = data.get("logs") or []
                has_more = bool(data.get("hasMore") or False)
                next_start = data.get("nextStartTime")
                next_end = data.get("nextEndTime")
            elif isinstance(data, list):
                logs = data

            for entry in logs or []:
                if isinstance(entry, dict):
                    msg = str(entry.get("message") or "")
                    ts_utc = str(entry.get("timestamp") or "")
                    lvl = _extract_level_from_labels(entry.get("labels"))
                    ts_local = _iso_to_est(ts_utc) if ts_utc else None

                    if fmt == "csv":
                        line = ",".join([_csv_escape(ts_local), _csv_escape(lvl), _csv_escape(msg), _csv_escape(msg)]) + "\n"
                        yield line.encode("utf-8")
                    elif fmt == "ndjson":
                        yield (json.dumps({"ts": ts_local, "level": lvl, "msg": msg, "raw": msg}) + "\n").encode("utf-8")
                    else:
                        yield f"{ts_local or ''} {lvl or ''} {msg}\n".encode("utf-8")
                else:
                    raw = str(entry)
                    if fmt == "csv":
                        line = ",".join([_csv_escape(None), _csv_escape(None), _csv_escape(raw), _csv_escape(raw)]) + "\n"
                        yield line.encode("utf-8")
                    elif fmt == "ndjson":
                        yield (json.dumps({"ts": None, "level": None, "msg": raw, "raw": raw}) + "\n").encode("utf-8")
                    else:
                        yield f"{raw}\n".encode("utf-8")

            if not has_more or not next_start or not next_end:
                break

            ns = rfc3339_to_ms(next_start)
            ne = rfc3339_to_ms(next_end)
            if (not ns) or (not ne) or ne <= ns:
                break
            cur_start, cur_end = ns, ne

    media_type = "text/csv" if fmt == "csv" else ("application/x-ndjson" if fmt == "ndjson" else "text/plain")
    return StreamingResponse(
        _iter_export(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
# ============================================================
# Backend proxy endpoints (notify + websearch)
# ============================================================
# These keep the Web UI isolated from the backend network surface.
# The Web UI calls the control plane; the control plane calls the backend.

def _backend_admin_key() -> str:
    # We intentionally reuse ADMIN_API_KEY to avoid redundant env vars.
    # If you want a distinct key, set VOZLIA_BACKEND_ADMIN_KEY and update backend_proxy.py accordingly.
    return (os.getenv("ADMIN_API_KEY") or "").strip()

def _proxy_call(fn):
    """Run a backend proxy call and preserve backend status codes."""
    try:
        return fn()
    except BackendProxyError as e:
        logger.warning(
            "BACKEND_PROXY_NON_2XX status=%s method=%s url=%s detail=%s",
            e.status_code,
            e.method,
            e.url,
            repr(e.detail),
        )
        content = e.detail if isinstance(e.detail, (dict, list)) else {"detail": str(e.detail)}
        return JSONResponse(status_code=e.status_code, content=content)



@app.post("/notify/email", dependencies=[Depends(require_admin_key)])
def proxy_notify_email(payload: dict):
    return _proxy_call(lambda: backend_post("/notify/email", admin_key=_backend_admin_key(), json_body=payload))


@app.post("/notify/sms", dependencies=[Depends(require_admin_key)])
def proxy_notify_sms(payload: dict):
    return _proxy_call(lambda: backend_post("/notify/sms", admin_key=_backend_admin_key(), json_body=payload))


@app.post("/notify/whatsapp", dependencies=[Depends(require_admin_key)])
def proxy_notify_whatsapp(payload: dict):
    return _proxy_call(lambda: backend_post("/notify/whatsapp", admin_key=_backend_admin_key(), json_body=payload))


@app.post("/notify/call", dependencies=[Depends(require_admin_key)])
def proxy_notify_call(payload: dict):
    return _proxy_call(lambda: backend_post("/notify/call", admin_key=_backend_admin_key(), json_body=payload))


@app.get("/admin/websearch/skills", dependencies=[Depends(require_admin_key)])
def proxy_websearch_skills_list():
    return _proxy_call(lambda: backend_get("/admin/websearch/skills", admin_key=_backend_admin_key()))


@app.post("/admin/websearch/skills", dependencies=[Depends(require_admin_key)])
def proxy_websearch_skills_upsert(payload: dict):
    return _proxy_call(lambda: backend_post("/admin/websearch/skills", admin_key=_backend_admin_key(), json_body=payload))


@app.delete("/admin/websearch/skills/{skill_id}", dependencies=[Depends(require_admin_key)])
def proxy_websearch_skills_delete(skill_id: str):
    return _proxy_call(lambda: backend_delete(f"/admin/websearch/skills/{skill_id}", admin_key=_backend_admin_key()))


@app.post("/admin/websearch/search", dependencies=[Depends(require_admin_key)])
def proxy_websearch_search(payload: dict):
    return _proxy_call(lambda: backend_post("/admin/websearch/search", admin_key=_backend_admin_key(), json_body=payload))


@app.get("/admin/websearch/schedules", dependencies=[Depends(require_admin_key)])
def proxy_websearch_schedules_list():
    return _proxy_call(lambda: backend_get("/admin/websearch/schedules", admin_key=_backend_admin_key()))


@app.post("/admin/websearch/schedules", dependencies=[Depends(require_admin_key)])
def proxy_websearch_schedules_upsert(payload: dict):
    return _proxy_call(lambda: backend_post("/admin/websearch/schedules", admin_key=_backend_admin_key(), json_body=payload))


@app.delete("/admin/websearch/schedules/{schedule_id}", dependencies=[Depends(require_admin_key)])
def proxy_websearch_schedules_delete(schedule_id: str):
    return _proxy_call(lambda: backend_delete(f"/admin/websearch/schedules/{schedule_id}", admin_key=_backend_admin_key()))




# ============================================================
# Proxy: Concepts (backend is source-of-truth)
# ============================================================

@app.get("/admin/concepts/definitions", dependencies=[Depends(require_admin_key)])
def proxy_concepts_definitions_get():
    return _proxy_call(lambda: backend_get("/admin/concepts/definitions", admin_key=_backend_admin_key()))

@app.post("/admin/concepts/definitions", dependencies=[Depends(require_admin_key)])
def proxy_concepts_definitions_post(payload: dict = Body(...)):
    return _proxy_call(lambda: backend_post("/admin/concepts/definitions", admin_key=_backend_admin_key(), json_body=payload))

@app.get("/admin/concepts/assignments", dependencies=[Depends(require_admin_key)])
def proxy_concepts_assignments_get():
    return _proxy_call(lambda: backend_get("/admin/concepts/assignments", admin_key=_backend_admin_key()))

@app.post("/admin/concepts/assignments", dependencies=[Depends(require_admin_key)])
def proxy_concepts_assignments_post(payload: dict = Body(...)):
    return _proxy_call(lambda: backend_post("/admin/concepts/assignments", admin_key=_backend_admin_key(), json_body=payload))


# ============================================================
# Proxy: DBQuery (backend is source-of-truth)
# ============================================================

@app.get("/admin/dbquery/entities", dependencies=[Depends(require_admin_key)])
def proxy_dbquery_entities_get():
    return _proxy_call(lambda: backend_get("/admin/dbquery/entities", admin_key=_backend_admin_key()))

@app.post("/admin/dbquery/run", dependencies=[Depends(require_admin_key)])
def proxy_dbquery_run(payload: dict = Body(...)):
    return _proxy_call(lambda: backend_post("/admin/dbquery/run", admin_key=_backend_admin_key(), json_body=payload))

@app.get("/admin/dbquery/skills", dependencies=[Depends(require_admin_key)])
def proxy_dbquery_skills_get():
    return _proxy_call(lambda: backend_get("/admin/dbquery/skills", admin_key=_backend_admin_key()))

@app.post("/admin/dbquery/skills", dependencies=[Depends(require_admin_key)])
def proxy_dbquery_skills_post(payload: dict = Body(...)):
    return _proxy_call(lambda: backend_post("/admin/dbquery/skills", admin_key=_backend_admin_key(), json_body=payload))

@app.delete("/admin/dbquery/skills/{skill_id}", dependencies=[Depends(require_admin_key)])
def proxy_dbquery_skills_delete(skill_id: str):
    return _proxy_call(lambda: backend_delete(f"/admin/dbquery/skills/{skill_id}", admin_key=_backend_admin_key()))

@app.get("/admin/dbquery/schedules", dependencies=[Depends(require_admin_key)])
def proxy_dbquery_schedules_get():
    return _proxy_call(lambda: backend_get("/admin/dbquery/schedules", admin_key=_backend_admin_key()))

@app.post("/admin/dbquery/schedules", dependencies=[Depends(require_admin_key)])
def proxy_dbquery_schedules_post(payload: dict = Body(...)):
    return _proxy_call(lambda: backend_post("/admin/dbquery/schedules", admin_key=_backend_admin_key(), json_body=payload))

# ============================================================
# Configuration Wizard endpoint (ChatGPT-style console)
# ============================================================

@app.post("/admin/wizard/turn", response_model=WizardTurnOut, dependencies=[Depends(require_admin_key)])
def admin_wizard_turn(payload: WizardTurnIn, db: Session = Depends(get_db)):
    """
    Chat-driven configuration endpoint.

    The LLM proposes structured actions; the control plane validates and executes them.
    """
    return run_wizard_turn(db, payload, admin_key=_backend_admin_key())


# -----------------------------
# Regression diagnostics
# -----------------------------

@app.get("/admin/diag/regression")
def admin_diag_regression(admin_key: str = Depends(require_admin_key)):
    """Lightweight health checks intended to catch WebUI regressions quickly.

    Returns 200 with ok=true when all checks pass. Returns 200 with ok=false if any check fails.
    (We intentionally do not raise here so the WebUI can render details.)
    """
    checks = []

    def _check(name: str, fn):
        t0 = time.time()
        try:
            out = fn()
            ms = (time.time() - t0) * 1000.0

            # Many of our admin endpoints return {ok: true}. Some return lists.
            ok = True
            detail = None
            if isinstance(out, dict) and "ok" in out:
                ok = bool(out.get("ok"))
                detail = None if ok else out
            elif out is None:
                ok = False
                detail = "no_response"

            checks.append({"name": name, "ok": ok, "ms": ms, "status": 200, "detail": detail})
        except Exception as e:
            ms = (time.time() - t0) * 1000.0
            checks.append({"name": name, "ok": False, "ms": ms, "status": None, "detail": str(e)})

    # Backend proxy checks (these power wizard + skills lists)
    _check("backend: /admin/websearch/skills", lambda: backend_get("/admin/websearch/skills", admin_key=admin_key))
    _check("backend: /admin/websearch/schedules", lambda: backend_get("/admin/websearch/schedules", admin_key=admin_key))
    _check("backend: /admin/dbquery/skills", lambda: backend_get("/admin/dbquery/skills", admin_key=admin_key))
    _check("backend: /admin/dbquery/schedules", lambda: backend_get("/admin/dbquery/schedules", admin_key=admin_key))
    _check("backend: /admin/dbquery/entities", lambda: backend_get("/admin/dbquery/entities", admin_key=admin_key))

    # Render API checks (power Render Logs panel)
    def _render_services_check():
        client = get_render_api()
        services = client.list_services()
        # Some accounts return dicts; normalize to list
        if isinstance(services, dict) and "services" in services:
            services = services["services"]
        return {"ok": bool(services), "count": len(services) if isinstance(services, list) else None}

    _check("render: list services", _render_services_check)

    ok = all(c.get("ok") for c in checks)
    return {"ok": ok, "ts": datetime.utcnow().isoformat() + "Z", "checks": checks}
