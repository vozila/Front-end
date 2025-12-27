import os
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from deps import get_db
from db import Base, engine
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

# =========================
# AUTH
# =========================

def require_admin_key(x_vozlia_admin_key: str = Header(default="", alias="X-Vozlia-Admin-Key")) -> bool:
    expected = os.getenv("ADMIN_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if not x_vozlia_admin_key or x_vozlia_admin_key != expected:
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
