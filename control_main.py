import os
from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from services.user_service import get_or_create_primary_user


from deps import get_db
from db import Base, engine
from services.user_service import get_or_create_primary_user
from services.settings_service import (
    get_agent_greeting,
    gmail_summary_enabled,
    get_selected_gmail_account_id,
    get_realtime_prompt_addendum,
    set_setting,
)

def require_admin_key(x_vozlia_admin_key: str | None = Header(default=None)):
    expected = os.getenv("ADMIN_API_KEY")
    if not expected or x_vozlia_admin_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

app = FastAPI(title="Vozlia Control")

@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)

@app.get("/health")
def health():
    return {"ok": True}

class AdminSettingsOut(BaseModel):
    agent_greeting: str
    gmail_summary_enabled: bool
    gmail_account_id: str | None = None
    realtime_prompt_addendum: str

class AdminSettingsPatch(BaseModel):
    agent_greeting: str | None = Field(default=None, min_length=1, max_length=500)
    gmail_summary_enabled: bool | None = None
    gmail_account_id: str | None = None
    realtime_prompt_addendum: str | None = Field(default=None, min_length=1, max_length=4000)

@app.get("/admin/settings", response_model=AdminSettingsOut, dependencies=[Depends(require_admin_key)])
def get_settings(db: Session = Depends(get_db)):
    user = get_or_create_primary_user(db)
    return AdminSettingsOut(
        agent_greeting=get_agent_greeting(db, user),
        gmail_summary_enabled=gmail_summary_enabled(db, user),
        gmail_account_id=get_selected_gmail_account_id(db, user),
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

    if "realtime_prompt_addendum" in data:
        set_setting(db, user, "realtime_prompt_addendum", {"text": data["realtime_prompt_addendum"].strip()})

    return get_settings(db)
