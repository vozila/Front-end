# services/settings_service.py
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from models import User, UserSetting

# -----------------------------
# Defaults (single source of truth for Control Plane UI)
# -----------------------------

DEFAULT_GMAIL_SUMMARY_LLM_PROMPT = (
    "You are Vozlia. Given email metadata (subject, sender, snippet, date), "
    "produce a VERY short spoken-style summary (1–3 sentences). "
    "Do NOT read email addresses or long codes out loud."
)

DEFAULTS: dict[str, dict[str, Any]] = {
    "agent_greeting": {"text": "Hello! How can I assist you today?"},
    # Back-compat flag (still used by Portal UI today)
    "gmail_summary_enabled": {"enabled": True},
    "gmail_account_id": {"account_id": "d8c8cd99-c9bc-4e8c-a560-d220782665a1"},
    "gmail_enabled_accounts": {"account_ids": []},
    "realtime_prompt_addendum": {
        "text": (
            "CALL OPENING RULE (FIRST UTTERANCE ONLY): "
            "Greet the caller and introduce yourself as Vozlia in one short sentence. "
            'Example: "Hello, you\'re speaking with Vozlia — how can I help today?" '
            "Do not repeat the brand intro after the first utterance."
        )
    },
    # NEW: modular per-skill configuration (future skills slot into this shape)
    "skills_config": {
        "skills": {
            "gmail_summary": {
                "enabled": True,
                "add_to_greeting": False,
                # Keep conservative default to preserve current behavior.
                "engagement_phrases": ["email summaries"],
                "llm_prompt": DEFAULT_GMAIL_SUMMARY_LLM_PROMPT,
            }
        }
    },
    # NEW: memory controls (migrating from env vars into DB-configurable toggles)
    "shortterm_memory_enabled": {"enabled": True},
    "longterm_memory_enabled": {"enabled": False},
    "memory_engagement_phrases": {"phrases": []},
    "skills_priority_order": {"order": ["gmail_summary", "memory", "sms", "calendar", "web_search", "weather", "investment_reporting"]},
}


def get_setting(db: Session, user: User, key: str) -> dict:
    row = db.query(UserSetting).filter(UserSetting.user_id == user.id, UserSetting.key == key).first()
    if row and isinstance(row.value, dict):
        return row.value
    return DEFAULTS.get(key, {})


def set_setting(db: Session, user: User, key: str, value: dict) -> None:
    row = db.query(UserSetting).filter(UserSetting.user_id == user.id, UserSetting.key == key).first()
    if row:
        row.value = value or {}
    else:
        row = UserSetting(user_id=user.id, key=key, value=value or {})
        db.add(row)
    db.commit()


# -----------------------------
# Existing settings (back-compat)
# -----------------------------
def get_agent_greeting(db: Session, user: User) -> str:
    v = get_setting(db, user, "agent_greeting")
    t = (v or {}).get("text")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return str(DEFAULTS["agent_greeting"]["text"])


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


def get_realtime_prompt_addendum(db: Session, user: User) -> str:
    v = get_setting(db, user, "realtime_prompt_addendum")
    t = (v or {}).get("text")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return str(DEFAULTS["realtime_prompt_addendum"]["text"])


def get_enabled_gmail_account_ids(db: Session, user: User) -> Optional[list[str]]:
    v = get_setting(db, user, "gmail_enabled_accounts")
    account_ids = (v or {}).get("account_ids")
    if account_ids is None:
        return None
    if isinstance(account_ids, list):
        cleaned = [str(x).strip() for x in account_ids if str(x).strip()]
        return cleaned
    return None


def set_enabled_gmail_account_ids(db: Session, user: User, account_ids: list[str]) -> None:
    cleaned = [str(x).strip() for x in (account_ids or []) if str(x).strip()]
    set_setting(db, user, "gmail_enabled_accounts", {"account_ids": cleaned})


# -----------------------------
# NEW: Skill config (modular)
# -----------------------------
def get_skills_config(db: Session, user: User) -> dict[str, dict]:
    v = get_setting(db, user, "skills_config")
    skills = (v or {}).get("skills")
    if isinstance(skills, dict):
        out: dict[str, dict] = {}
        for k, cfg in skills.items():
            if isinstance(k, str) and isinstance(cfg, dict):
                out[k] = cfg
        return out
    # fall back to defaults
    return dict(DEFAULTS["skills_config"]["skills"])


def patch_skill_config(db: Session, user: User, skill_id: str, patch: dict) -> dict[str, dict]:
    """
    Merge and persist a per-skill config patch.

    Notes:
    - Accept both snake_case (backend) and camelCase (portal UI) keys.
    - Parse string fields into lists where appropriate (engagement prompt, tickers).
    - Fail-open: unknown keys are ignored.
    """
    current = get_skills_config(db, user)
    base = dict(current.get(skill_id) or DEFAULTS["skills_config"]["skills"].get(skill_id, {}))

    # Normalize alternate keys coming from the portal
    normalized = dict(patch or {})

    # engagementPrompt / engagement_prompt -> engagement_phrases
    if "engagement_phrases" not in normalized:
        alt = normalized.get("engagementPrompt") or normalized.get("engagement_prompt")
        if isinstance(alt, str):
            # allow newline-separated or comma-separated
            phrases = []
            for line in alt.replace(",", "\n").splitlines():
                s = line.strip()
                if s:
                    phrases.append(s)
            normalized["engagement_phrases"] = phrases

    # llmPrompt -> llm_prompt
    if "llm_prompt" not in normalized and isinstance(normalized.get("llmPrompt"), str):
        normalized["llm_prompt"] = normalized["llmPrompt"]

    # tickers: allow list or comma-separated string
    if "tickers" in normalized and isinstance(normalized["tickers"], str):
        raw = normalized["tickers"]
        tickers = []
        for part in raw.split(","):
            t = part.strip().upper()
            if t:
                tickers.append(t)
        normalized["tickers"] = tickers
        normalized.setdefault("tickers_raw", raw)

    # merge allowed keys
    for k in (
        "enabled",
        "add_to_greeting",
        "auto_execute_after_greeting",
        "engagement_phrases",
        "llm_prompt",
        "tickers",
        "tickers_raw",
    ):
        if k in normalized:
            base[k] = normalized[k]

    current[skill_id] = base
    set_setting(db, user, "skills_config", {"skills": current})
    return current




def get_skills_priority_order(db: Session, user: User) -> list[str]:
    v = get_setting(db, user, "skills_priority_order")
    order = (v or {}).get("order")
    if isinstance(order, list):
        cleaned: list[str] = []
        for s in order:
            if isinstance(s, str) and s.strip():
                cleaned.append(s.strip())
        if cleaned:
            return cleaned
    d = DEFAULTS.get("skills_priority_order", {}).get("order")
    if isinstance(d, list):
        return [s for s in d if isinstance(s, str) and s.strip()]
    return []


def set_skills_priority_order(db: Session, user: User, order: list[str]) -> None:
    cleaned: list[str] = []
    for s in (order or []):
        if isinstance(s, str) and s.strip():
            cleaned.append(s.strip())
    set_setting(db, user, "skills_priority_order", {"order": cleaned})

# -----------------------------
# NEW: Memory config (migrating env vars)
# -----------------------------
def shortterm_memory_enabled(db: Session, user: User) -> bool:
    v = get_setting(db, user, "shortterm_memory_enabled")
    enabled = (v or {}).get("enabled")
    return bool(True if enabled is None else enabled)


def longterm_memory_enabled(db: Session, user: User) -> bool:
    v = get_setting(db, user, "longterm_memory_enabled")
    enabled = (v or {}).get("enabled")
    return bool(False if enabled is None else enabled)


def get_memory_engagement_phrases(db: Session, user: User) -> list[str]:
    v = get_setting(db, user, "memory_engagement_phrases")
    phrases = (v or {}).get("phrases")
    if isinstance(phrases, list):
        cleaned = [str(x).strip() for x in phrases if str(x).strip()]
        return cleaned
    return list(DEFAULTS["memory_engagement_phrases"]["phrases"])
