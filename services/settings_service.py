# services/settings_service.py
from __future__ import annotations

from typing import Optional
from sqlalchemy.orm import Session

from models import User, UserSetting

DEFAULTS = {
    "agent_greeting": {"text": "Hello! How can I assist you today?"},
    "gmail_summary_enabled": {"enabled": True},
    "gmail_account_id": {"account_id": "d8c8cd99-c9bc-4e8c-a560-d220782665a1"},
    "realtime_prompt_addendum": {
        "text": (
            "CALL OPENING RULE (FIRST UTTERANCE ONLY): "
            "Greet the caller and introduce yourself as Vozlia in one short sentence. "
            "Example: \"Hello, you're speaking with Vozlia — how can I help today?\" "
            "Do not repeat the brand intro after the first utterance."
        )
    },
}

def get_realtime_prompt_addendum(db: Session, user: User) -> str:
    v = get_setting(db, user, "realtime_prompt_addendum")
    txt = (v or {}).get("text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    return DEFAULTS["realtime_prompt_addendum"]["text"]


def get_setting(db: Session, user: User, key: str) -> dict:
    row = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.key == key)
        .first()
    )
    if row and isinstance(row.value, dict):
        return row.value
    return DEFAULTS.get(key, {})

def set_setting(db: Session, user: User, key: str, value: dict) -> dict:
    row = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.key == key)
        .first()
    )
    if row:
        row.value = value
    else:
        row = UserSetting(user_id=user.id, key=key, value=value)
        db.add(row)

    db.commit()
    db.refresh(row)
    return row.value

def get_agent_greeting(db: Session, user: User) -> str:
    v = get_setting(db, user, "agent_greeting")
    txt = (v or {}).get("text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    return DEFAULTS["agent_greeting"]["text"]

def gmail_summary_enabled(db: Session, user: User) -> bool:
    v = get_setting(db, user, "gmail_summary_enabled")
    enabled = (v or {}).get("enabled")
    return bool(True if enabled is None else enabled)

def get_selected_gmail_account_id(db: Session, user: User) -> Optional[str]:
    v = get_setting(db, user, "gmail_account_id")
    account_id = (v or {}).get("account_id")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None
