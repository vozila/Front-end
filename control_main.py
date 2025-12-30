import os
import json
import re
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from deps import get_db
from db import Base, engine
from core.security import encrypt_str
from models import EmailAccount
from services.user_service import get_or_create_primary_user
from services.settings_service import (
    get_agent_greeting,
    gmail_summary_enabled,
    get_selected_gmail_account_id,
    get_realtime_prompt_addendum,
    get_enabled_gmail_account_ids,
    set_setting,
    set_enabled_gmail_account_ids,
)
from services.render_api import render_get_json, ms_to_rfc3339, rfc3339_to_ms, RenderAPIError


# =========================
# AUTH
# =========================

from fastapi import Header, HTTPException

def require_admin_key(
    x_vozlia_admin_key: str = Header(default="", alias="X-Vozlia-Admin-Key"),
    x_admin_key: str = Header(default="", alias="x-admin-key"),
) -> bool:
    expected = (os.getenv("ADMIN_API_KEY", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")

    provided = (x_vozlia_admin_key or "").strip() or (x_admin_key or "").strip()
    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return True



# =========================
# APP
# =========================

app = FastAPI(title="Vozlia Control")

@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/health")
def health() -> str:
    return "OK"


# =========================
# SETTINGS
# =========================

class AdminSettingsOut(BaseModel):
    agent_greeting: str
    gmail_summary_enabled: bool
    gmail_account_id: Optional[str] = None
    gmail_enabled_account_ids: Optional[List[str]] = None
    realtime_prompt_addendum: str


class AdminSettingsPatch(BaseModel):
    agent_greeting: str | None = Field(default=None, min_length=1, max_length=500)
    gmail_summary_enabled: bool | None = None
    gmail_account_id: str | None = None
    gmail_enabled_account_ids: List[str] | None = None
    realtime_prompt_addendum: str | None = Field(default=None, min_length=1, max_length=4000)


@app.get("/admin/settings", response_model=AdminSettingsOut, dependencies=[Depends(require_admin_key)])
def get_settings(db: Session = Depends(get_db)):
    user = get_or_create_primary_user(db)
    return AdminSettingsOut(
        agent_greeting=get_agent_greeting(db, user),
        gmail_summary_enabled=gmail_summary_enabled(db, user),
        gmail_account_id=get_selected_gmail_account_id(db, user),
        gmail_enabled_account_ids=get_enabled_gmail_account_ids(db, user),
        realtime_prompt_addendum=get_realtime_prompt_addendum(db, user),
    )


@app.patch("/admin/settings", response_model=AdminSettingsOut, dependencies=[Depends(require_admin_key)])
def patch_settings(payload: AdminSettingsPatch, db: Session = Depends(get_db)):
    user = get_or_create_primary_user(db)
    data = payload.model_dump(exclude_none=True)

    if "agent_greeting" in data:
        set_setting(db, user, "agent_greeting", {"text": data["agent_greeting"].strip()})

    if "gmail_summary_enabled" in data:
        set_setting(db, user, "gmail_summary_enabled", {"enabled": bool(data["gmail_summary_enabled"])})

    if "gmail_account_id" in data:
        set_setting(db, user, "gmail_account_id", {"account_id": data["gmail_account_id"].strip()})

    if "gmail_enabled_account_ids" in data:
        set_enabled_gmail_account_ids(db, user, data["gmail_enabled_account_ids"])

    if "realtime_prompt_addendum" in data:
        set_setting(db, user, "realtime_prompt_addendum", {"text": data["realtime_prompt_addendum"].strip()})

    return get_settings(db)


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


@app.get("/admin/email-accounts", response_model=List[EmailAccountOut], dependencies=[Depends(require_admin_key)])
def list_email_accounts(
    include_inactive: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    user = get_or_create_primary_user(db)
    q = db.query(EmailAccount).filter(EmailAccount.user_id == user.id)
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
    """Create or update a Gmail/Google OAuth EmailAccount for the primary user.

    Notes:
    - Access/refresh tokens are encrypted at rest using ENCRYPTION_KEY.
    - If refresh_token is not provided (Google may omit it on subsequent consents),
      the existing refresh token (if any) is preserved.
    """
    user = get_or_create_primary_user(db)

    email_address = payload.email_address.strip().lower()
    if not email_address:
        raise HTTPException(status_code=400, detail="email_address is required")

    a = (
        db.query(EmailAccount)
        .filter(
            EmailAccount.user_id == user.id,
            EmailAccount.provider_type == "gmail",
            EmailAccount.oauth_provider == "google",
            EmailAccount.email_address == email_address,
        )
        .first()
    )

    created = False
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
        created = True

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
    db.refresh(a)
    return _to_email_out(a)


@app.patch("/admin/email-accounts/{account_id}", response_model=EmailAccountOut, dependencies=[Depends(require_admin_key)])
def patch_email_account(account_id: str, payload: EmailAccountPatch, db: Session = Depends(get_db)):
    user = get_or_create_primary_user(db)
    a = db.query(EmailAccount).filter(EmailAccount.id == account_id, EmailAccount.user_id == user.id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Email account not found")

    data = payload.model_dump(exclude_none=True)

    if "display_name" in data:
        a.display_name = (data["display_name"] or "").strip() or None

    if "is_active" in data:
        a.is_active = bool(data["is_active"])

    if data.get("is_primary") is True:
        # Demote others
        db.query(EmailAccount).filter(
            EmailAccount.user_id == user.id,
            EmailAccount.id != a.id,
        ).update({"is_primary": False})
        a.is_primary = True

    db.commit()
    db.refresh(a)
    return _to_email_out(a)


@app.delete("/admin/email-accounts/{account_id}", dependencies=[Depends(require_admin_key)])
def delete_email_account(
    account_id: str,
    hard: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """Disconnect an email account.

    - soft (default): set inactive and wipe stored credentials/tokens
    - hard=true: delete row entirely
    """
    user = get_or_create_primary_user(db)
    a = db.query(EmailAccount).filter(EmailAccount.id == account_id, EmailAccount.user_id == user.id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Email account not found")

    if hard:
        db.delete(a)
        db.commit()
        return {"status": "deleted", "hard": True}

    # soft disconnect
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
    ts: str | None = None
    level: str | None = None
    message: str
    raw: dict | str | None = None

class RenderLogsOut(BaseModel):
    rows: List[RenderLogRow]
    has_more: bool = False
    next_start_ms: int | None = None
    next_end_ms: int | None = None


def _render_owner_id() -> str | None:
    # Optional: if set, we filter list-services and constrain log queries
    return (os.getenv("RENDER_OWNER_ID") or "").strip() or None


def _normalize_log_row(item) -> RenderLogRow:
    # Render logs typically return dict objects; keep robust.
    if isinstance(item, str):
        msg = item.strip()
        return RenderLogRow(message=msg, raw=item)

    if not isinstance(item, dict):
        return RenderLogRow(message=str(item), raw={"value": str(item)})

    ts = item.get("timestamp") or item.get("time") or item.get("ts")
    msg = item.get("message") or item.get("text") or item.get("line") or json.dumps(item, ensure_ascii=False)
    level = item.get("level") or item.get("severity")

    # If level missing, attempt quick inference from message prefix.
    if not level and isinstance(msg, str):
        m = re.search(r"\b(INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)\b", msg)
        if m:
            level = m.group(1)

    return RenderLogRow(ts=ts, level=level, message=str(msg), raw=item)


@app.get("/admin/render/services", response_model=List[RenderServiceOut], dependencies=[Depends(require_admin_key)])
def admin_render_list_services(
    limit: int = Query(default=100, ge=1, le=200),
):
    """
    Lists Render services for the configured Render API key.
    Optionally filtered to RENDER_OWNER_ID if configured.
    """
    params = {"limit": str(limit)}
    owner_id = _render_owner_id()
    if owner_id:
        params["ownerId"] = owner_id

    try:
        data = render_get_json("/v1/services", params=params)
    except RenderAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Render returns either:
    #  - [ { "cursor": "...", "service": { ... } }, ... ]
    #  - [ { ...service fields... }, ... ]
    #  - { "services": [ ... ] }
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



@app.get("/admin/render/services/{service_id}/instances", response_model=List[RenderInstanceOut], dependencies=[Depends(require_admin_key)])
def admin_render_list_instances(service_id: str):
    """
    Lists instances for a given Render service.
    """
    try:
        data = render_get_json(f"/v1/services/{service_id}/instances", params={"limit": "50"})
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
        out.append(
            RenderInstanceOut(
                id=str(it.get("id") or ""),
                service_id=it.get("serviceId") or it.get("service_id") or service_id,
                status=it.get("status"),
                started_at=it.get("startedAt") or it.get("started_at"),
            )
        )
    out = [x for x in out if x.id]
    return out


@app.get("/admin/render/logs", response_model=RenderLogsOut, dependencies=[Depends(require_admin_key)])
def admin_render_logs(
    service_id: str = Query(..., description="Render service id (srv-...)"),
    instance_id: Optional[str] = Query(default=None, description="Render instance id (optional)"),
    start_ms: int = Query(..., ge=0),
    end_ms: int = Query(..., ge=0),
    q: Optional[str] = Query(default=None, description="Optional text filter"),
    limit: int = Query(default=200, ge=1, le=500),
    next_start_ms: Optional[int] = Query(default=None),
    next_end_ms: Optional[int] = Query(default=None),
):
    """
    Preview logs for a service (and optional instance) between start_ms and end_ms.
    Supports paging via next_start_ms/next_end_ms (returned from previous response).
    """
    if end_ms <= start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be > start_ms")

    # For paging, override start/end with next values if provided
    if next_start_ms and next_end_ms and next_end_ms > next_start_ms:
        start_ms = next_start_ms
        end_ms = next_end_ms

    params = {
        "limit": str(limit),
        "startTime": ms_to_rfc3339(start_ms),
        "endTime": ms_to_rfc3339(end_ms),
        # Render expects resource filters; use single service id.
        "resource": service_id,
    }

    owner_id = _render_owner_id()
    if owner_id:
        params["ownerId"] = owner_id

    if instance_id:
        params["instance"] = instance_id

    # Some Render API implementations support server-side text query; keep as best-effort.
    if q:
        params["text"] = q

    try:
        data = render_get_json("/v1/logs", params=params)
    except RenderAPIError as e:
        raise HTTPException(status_code=e.status, detail=e.body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    logs = []
    has_more = False
    next_start = None
    next_end = None

    if isinstance(data, dict):
        logs = data.get("logs") or data.get("entries") or data.get("items") or []
        has_more = bool(data.get("hasMore") or data.get("has_more") or False)
        next_start = data.get("nextStartTime") or data.get("next_start_time")
        next_end = data.get("nextEndTime") or data.get("next_end_time")
    elif isinstance(data, list):
        logs = data

    rows = [_normalize_log_row(x) for x in (logs or [])]

    # If server-side filtering isn't supported, apply client-side filter.
    if q and rows:
        qn = q.lower()
        rows = [r for r in rows if qn in (r.message or "").lower()]

    out = RenderLogsOut(
        rows=rows,
        has_more=has_more,
        next_start_ms=rfc3339_to_ms(next_start) if next_start else None,
        next_end_ms=rfc3339_to_ms(next_end) if next_end else None,
    )
    return out


@app.get("/admin/render/logs/export", dependencies=[Depends(require_admin_key)])
def admin_render_logs_export(
    service_id: str = Query(...),
    instance_id: Optional[str] = Query(default=None),
    start_ms: int = Query(..., ge=0),
    end_ms: int = Query(..., ge=0),
    q: Optional[str] = Query(default=None),
):
    """
    Export logs as plain text (one line per record).
    This does a bounded paged fetch using Render's hasMore + nextStartTime/nextEndTime.
    """
    if end_ms <= start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be > start_ms")

    filename = f"render_logs_{service_id}_{start_ms}_{end_ms}.log"

    def _iter_text():
        nonlocal start_ms, end_ms
        params_base = {
            "limit": "500",
            "resource": service_id,
        }
        owner_id = _render_owner_id()
        if owner_id:
            params_base["ownerId"] = owner_id
        if instance_id:
            params_base["instance"] = instance_id
        if q:
            params_base["text"] = q

        cur_start = start_ms
        cur_end = end_ms
        safety_pages = 0

        while True:
            safety_pages += 1
            if safety_pages > 20:
                # hard cap to prevent runaway exports
                yield "\n[export truncated: too many pages]\n"
                break

            params = dict(params_base)
            params["startTime"] = ms_to_rfc3339(cur_start)
            params["endTime"] = ms_to_rfc3339(cur_end)

            try:
                data = render_get_json("/v1/logs", params=params, timeout_s=30.0)
            except Exception as e:
                yield f"\n[export error] {e}\n"
                break

            logs = []
            has_more = False
            next_start = None
            next_end = None
            if isinstance(data, dict):
                logs = data.get("logs") or data.get("entries") or data.get("items") or []
                has_more = bool(data.get("hasMore") or data.get("has_more") or False)
                next_start = data.get("nextStartTime") or data.get("next_start_time")
                next_end = data.get("nextEndTime") or data.get("next_end_time")
            elif isinstance(data, list):
                logs = data

            for item in logs or []:
                row = _normalize_log_row(item)
                ts = row.ts or ""
                lvl = row.level or ""
                msg = row.message or ""
                yield f"{ts} {lvl} {msg}\n"

            if not has_more or not next_start or not next_end:
                break

            ns = rfc3339_to_ms(next_start)
            ne = rfc3339_to_ms(next_end)
            if not ns or not ne or ne <= ns:
                break

            cur_start, cur_end = ns, ne

    return StreamingResponse(
        _iter_text(),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
