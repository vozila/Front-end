# services/config_wizard_service.py
"""
Configuration Wizard service (owner/admin only).

This is a "tool-first" agent design:
- LLM proposes *structured* actions (JSON).
- Control plane validates and executes deterministic operations.
- UI stays minimalist; capabilities live behind the wizard.

This significantly reduces hallucinations vs. "freeform" LLM answers.
"""
from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, Literal, Annotated

from pydantic import BaseModel, Field, ValidationError

from services.user_service import get_or_create_primary_user
from services.settings_service import (
    get_skills_config,
    patch_skill_config,
    get_skills_priority_order,
    set_skills_priority_order,
)
from services.backend_proxy import backend_get, backend_post

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from openai import OpenAI

log = logging.getLogger("vozlia")


# -----------------------------
# Models
# -----------------------------

class WizardTurnIn(BaseModel):
    # Latest user message
    message: str = Field(..., min_length=1, max_length=4000)
    # Optional short history: [{role:"user"|"assistant", content:"..."}]
    messages: Optional[List[Dict[str, str]]] = None
    # Client-provided defaults (optional)
    default_timezone: Optional[str] = None
    default_channel: Optional[Literal["email", "sms", "whatsapp", "phone"]] = None
    default_destination: Optional[str] = None
    # When true, the wizard will only *plan*, not execute.
    dry_run: bool = False


class ActionWebSearchRun(BaseModel):
    type: Literal["websearch_run"] = "websearch_run"
    query: str = Field(..., min_length=1, max_length=500)
    # If true, also propose making it into a dedicated skill (wizard will ask user).
    suggest_skill: bool = False


class ActionWebSearchSkillCreate(BaseModel):
    type: Literal["websearch_skill_create"] = "websearch_skill_create"
    name: str = Field(..., min_length=1, max_length=80)
    query: str = Field(..., min_length=1, max_length=500)
    triggers: List[str] = Field(default_factory=list, max_length=20)


class ActionWebSearchScheduleUpsert(BaseModel):
    type: Literal["websearch_schedule_upsert"] = "websearch_schedule_upsert"
    # Prefer id; name is allowed and will be resolved.
    web_search_skill_id: Optional[str] = None
    skill_name: Optional[str] = None
    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)
    timezone: str = Field(..., min_length=1, max_length=64)
    channel: Literal["email", "sms"] = "email"
    destination: str = Field(..., min_length=3, max_length=320)


class ActionSkillConfigPatch(BaseModel):
    type: Literal["skill_config_patch"] = "skill_config_patch"
    skill_id: str = Field(..., min_length=1, max_length=128)
    patch: Dict[str, Any]


class ActionSkillsPrioritySet(BaseModel):
    type: Literal["skills_priority_set"] = "skills_priority_set"
    # Full ordered list of skill ids/keys
    order: List[str] = Field(..., min_length=1)


class ActionNoop(BaseModel):
    type: Literal["noop"] = "noop"
    reason: Optional[str] = None


WizardAction = Annotated[
    Union[
        ActionWebSearchRun,
        ActionWebSearchSkillCreate,
        ActionWebSearchScheduleUpsert,
        ActionSkillConfigPatch,
        ActionSkillsPrioritySet,
        ActionNoop,
    ],
    Field(discriminator="type"),
]


class WizardPlan(BaseModel):
    reply: str = Field(..., min_length=1, max_length=3000)
    actions: List[WizardAction] = Field(default_factory=list)


class WizardTurnOut(BaseModel):
    reply: str
    actions_executed: List[Dict[str, Any]] = Field(default_factory=list)
    # Fresh state snapshots for UI refresh
    websearch_skills: List[Dict[str, Any]] = Field(default_factory=list)
    websearch_schedules: List[Dict[str, Any]] = Field(default_factory=list)
    skills_config: Dict[str, Any] = Field(default_factory=dict)
    skills_priority_order: List[str] = Field(default_factory=list)


# -----------------------------
# Helpers
# -----------------------------

DEFAULT_TIMEZONE = "America/New_York"

_SKILL_ALIASES: Dict[str, List[str]] = {
    "gmail_summary": ["gmail summary", "email summaries", "email summary", "emails summary"],
    "investment_reporting": ["investment report", "investment reporting", "stock report", "stocks report", "stock reporting"],
    "web_search": ["web search", "websearch", "internet search"],
}


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _resolve_skill_id_from_name(name: str, skills_config: Dict[str, Any]) -> Optional[str]:
    n = _normalize(name)
    # exact skill_id
    if n in skills_config:
        return n
    # alias match
    for skill_id, aliases in _SKILL_ALIASES.items():
        if n == skill_id or n in aliases:
            return skill_id if skill_id in skills_config else skill_id
    # fuzzy contains match on aliases
    for skill_id, aliases in _SKILL_ALIASES.items():
        for a in aliases:
            if a in n:
                return skill_id
    return None


def _resolve_websearch_skill_id(plan_action: ActionWebSearchScheduleUpsert, websearch_skills: List[Dict[str, Any]]) -> Optional[str]:
    if plan_action.web_search_skill_id:
        return plan_action.web_search_skill_id
    if not plan_action.skill_name:
        return None
    target = _normalize(plan_action.skill_name)
    for s in websearch_skills:
        if _normalize(s.get("name", "")) == target:
            return s.get("id")
    # contains match
    for s in websearch_skills:
        if target and target in _normalize(s.get("name", "")):
            return s.get("id")
    return None


def _get_model_name() -> str:
    return (
        os.getenv("OPENAI_WIZARD_MODEL")
        or os.getenv("OPENAI_ROUTER_MODEL")
        or os.getenv("OPENAI_MODEL")
        or "gpt-4o-mini"
    )


def _safe_json_loads(s: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(s)
    except Exception:
        # Try to salvage by extracting a JSON object
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _tz_from_text(text: str, default_tz: str) -> str:
    t = text.lower()
    # Common shorthands
    if re.search(r"\b(est|edt|et|eastern)\b", t):
        return "America/New_York"
    # If user provided an IANA timezone-like token, trust it
    m = re.search(r"\b[A-Za-z]+\/[A-Za-z_]+\b", text)
    if m:
        return m.group(0)
    return default_tz


def _parse_time_hm(text: str) -> Optional[tuple[int, int]]:
    """
    Parses times like:
      - 23:10
      - 11:10 PM
      - 11:10PM
      - 11 PM (treated as 11:00 PM)
    """
    t = text.strip()
    m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\s*(am|pm)?\b", t, re.IGNORECASE)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        ap = (m.group(3) or "").lower()
        if ap:
            if hh == 12:
                hh = 0
            if ap == "pm":
                hh += 12
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm

    m2 = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t, re.IGNORECASE)
    if m2:
        hh = int(m2.group(1))
        ap = m2.group(2).lower()
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        if 0 <= hh <= 23:
            return hh, 0

    return None


def _extract_quoted(text: str) -> Optional[str]:
    m = re.search(r"'([^']+)'", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"\"([^\"]+)\"", text)
    if m:
        return m.group(1).strip()
    return None


def _fastpath_schedule_websearch(payload: WizardTurnIn, context: Dict[str, Any]) -> Optional[WizardPlan]:
    """
    Deterministic guardrail for the most common owner request:
    "schedule the existing '<skill>' skill to run every 11:10 PM EST every day"

    If we can parse skill name + time, we bypass the LLM and schedule directly.
    """
    msg = payload.message.strip()
    if not re.search(r"\b(schedule|run every|every day|daily)\b", msg, re.IGNORECASE):
        return None

    skill_name = _extract_quoted(msg)
    if not skill_name:
        return None

    hm = _parse_time_hm(msg)
    if not hm:
        return None

    tz = _tz_from_text(msg, payload.default_timezone or DEFAULT_TIMEZONE)
    channel = payload.default_channel or "email"
    dest = payload.default_destination or ""
    if not dest:
        # No destination: let the LLM ask a clarifying question.
        return None

    # Confirm the skill exists (websearch skills only in this fastpath)
    ws_skills = context.get("websearch_skills") or []
    ws_id = None
    for s in ws_skills:
        if _normalize(s.get("name", "")) == _normalize(skill_name):
            ws_id = s.get("id")
            break
    if not ws_id:
        return None

    hh, mm = hm
    reply = f"Scheduled '{skill_name}' to run daily at {hh:02d}:{mm:02d} ({tz}) and deliver via {channel} to {dest}."
    return WizardPlan(
        reply=reply,
        actions=[
            ActionWebSearchScheduleUpsert(
                web_search_skill_id=ws_id,
                hour=hh,
                minute=mm,
                timezone=tz,
                channel=("sms" if channel == "sms" else "email"),
                destination=dest,
            )
        ],
    )

def _build_context_snapshot(db, user, admin_key: str) -> Dict[str, Any]:
    skills_config = get_skills_config(db, user)
    skills_priority = get_skills_priority_order(db, user)

    # Websearch resources live in backend; fetch via backend admin endpoints.
    websearch_skills = backend_get("/admin/websearch/skills", admin_key=admin_key)
    websearch_schedules = backend_get("/admin/websearch/schedules", admin_key=admin_key)

    # Normalize to lists
    if not isinstance(websearch_skills, list):
        websearch_skills = []
    if not isinstance(websearch_schedules, list):
        websearch_schedules = []

    return {
        "now_utc": datetime.utcnow().isoformat() + "Z",
        "skills_config": skills_config,
        "skills_priority_order": skills_priority,
        "websearch_skills": websearch_skills,
        "websearch_schedules": websearch_schedules,
    }


def _plan_with_llm(payload: WizardTurnIn, context: Dict[str, Any]) -> WizardPlan:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        # Hard fail: wizard needs LLM planning
        return WizardPlan(
            reply="Wizard is not configured: OPENAI_API_KEY is missing on the control plane.",
            actions=[ActionNoop(reason="missing_openai_key")],
        )

    model = _get_model_name()
    client = OpenAI(api_key=api_key)

    # Keep the context compact; the LLM needs *available identifiers* more than raw configs.
    skills_list = []
    for sid, cfg in (context.get("skills_config") or {}).items():
        label = cfg.get("label") or sid
        skills_list.append({"skill_id": sid, "label": label, "enabled": bool(cfg.get("enabled", True))})

    ws_skills_list = [
        {"id": s.get("id"), "name": s.get("name"), "query": s.get("query"), "enabled": s.get("enabled", True)}
        for s in (context.get("websearch_skills") or [])
    ]

    ws_sched_list = [
        {
            "id": r.get("id"),
            "web_search_skill_id": r.get("web_search_skill_id"),
            "enabled": r.get("enabled", True),
            "cadence": r.get("cadence"),
            "time_of_day": r.get("time_of_day"),
            "timezone": r.get("timezone"),
            "channel": r.get("channel"),
            "destination": r.get("destination"),
            "next_run_at": r.get("next_run_at"),
        }
        for r in (context.get("websearch_schedules") or [])
    ]

    default_tz = payload.default_timezone or DEFAULT_TIMEZONE
    default_channel = payload.default_channel or "email"
    default_dest = payload.default_destination or ""

    system = f"""
You are the Vozlia Configuration Wizard for the *business owner portal*.

Your job:
- Help the owner accomplish goals by performing actions inside Vozlia (not explaining how to do it in other products like Alexa).
- When possible, perform the change directly by outputting a structured action plan.
- If information is missing, ask a single clarifying question.

Critical rules:
- Never give instructions for unrelated third-party products (Alexa, Google Home, etc.) unless the user explicitly asked about them.
- Only refer to capabilities that exist via the allowed actions below.
- Output MUST be a single JSON object (no markdown, no extra text).
- If the user asks for a schedule, use 24-hour time (hour 0-23, minute 0-59). Default timezone is "{default_tz}".
- If channel/destination are missing, you may use defaults: channel="{default_channel}", destination="{default_dest}" (if destination is non-empty). Otherwise ask the user.

Allowed actions (choose 0+):
1) websearch_run:
   {{ "type":"websearch_run", "query":"...", "suggest_skill": false }}

2) websearch_skill_create:
   {{ "type":"websearch_skill_create", "name":"...", "query":"...", "triggers":["..."] }}

3) websearch_schedule_upsert:
   {{ "type":"websearch_schedule_upsert", "web_search_skill_id":"<uuid>" OR "skill_name":"<name>",
      "hour": 23, "minute": 10, "timezone":"America/New_York",
      "channel":"email"|"sms", "destination":"user@example.com" }}

4) skill_config_patch:
   {{ "type":"skill_config_patch", "skill_id":"gmail_summary|investment_reporting|...", "patch":{{ ... }} }}

5) skills_priority_set:
   {{ "type":"skills_priority_set", "order":["skill_id_1","skill_id_2", ...] }}

6) noop:
   {{ "type":"noop", "reason":"..." }}

Known built-in skill aliases:
- gmail_summary: "email summaries", "gmail summary"
- investment_reporting: "investment report", "stock report"

Context: current Vozlia state (read-only):
- skills: {json.dumps(skills_list)[:8000]}
- websearch_skills: {json.dumps(ws_skills_list)[:8000]}
- websearch_schedules: {json.dumps(ws_sched_list)[:8000]}

Return JSON shape:
{{
  "reply": "<what you will do / what you need>",
  "actions": [ ...allowed actions... ]
}}
""".strip()

    messages = []
    messages.append({"role": "system", "content": system})

    # Optional short history
    if payload.messages:
        for m in payload.messages[-12:]:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content[:2000]})

    messages.append({"role": "user", "content": payload.message[:4000]})

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore
            temperature=0,
            max_tokens=700,
        )
    except Exception as e:
        log.exception("CONFIG_WIZARD_LLM_CALL_FAIL")
        return WizardPlan(
            reply=f"Wizard LLM call failed: {e}",
            actions=[ActionNoop(reason="llm_call_failed")],
        )

    content = (resp.choices[0].message.content or "").strip()
    data = _safe_json_loads(content)
    if not data:
        return WizardPlan(
            reply="I couldn’t produce a valid action plan. Please rephrase your request.",
            actions=[ActionNoop(reason="invalid_json")],
        )

    try:
        return WizardPlan.model_validate(data)
    except ValidationError:
        log.warning("CONFIG_WIZARD_PLAN_VALIDATION_FAIL data=%s", data)
        return WizardPlan(
            reply="I understood your request, but the action plan didn’t match the expected format. Please try again.",
            actions=[ActionNoop(reason="plan_schema_invalid")],
        )


# -----------------------------
# Public API
# -----------------------------

def run_wizard_turn(db, payload: WizardTurnIn, *, admin_key: str) -> WizardTurnOut:
    """
    Main entry point used by /admin/wizard/turn.
    """
    user = get_or_create_primary_user(db)

    # Load context once (skills & websearch inventory). This is also used for id resolution.
    ctx = _build_context_snapshot(db, user, admin_key=admin_key)

    # Fill defaults from DB where possible.
    if not payload.default_destination:
        # Best effort: owner's email in DB becomes default recipient.
        if getattr(user, "email", None):
            payload.default_destination = user.email
    if not payload.default_timezone:
        payload.default_timezone = DEFAULT_TIMEZONE

    plan = _fastpath_schedule_websearch(payload, ctx) or _plan_with_llm(payload, ctx)

    executed: List[Dict[str, Any]] = []
    if payload.dry_run:
        executed.append({"type": "dry_run", "note": "No changes executed."})
    else:
        for action in plan.actions:
            try:
                if isinstance(action, ActionWebSearchRun):
                    # Perform websearch using backend endpoint
                    out = backend_post("/admin/websearch/search", admin_key=admin_key, json_body={"query": action.query})
                    executed.append({"type": action.type, "query": action.query, "result": out})
                elif isinstance(action, ActionWebSearchSkillCreate):
                    out = backend_post(
                        "/admin/websearch/skills",
                        admin_key=admin_key,
                        json_body={"name": action.name, "query": action.query, "triggers": action.triggers},
                    )
                    executed.append({"type": action.type, "created": out})
                elif isinstance(action, ActionWebSearchScheduleUpsert):
                    ws_id = _resolve_websearch_skill_id(action, ctx.get("websearch_skills") or [])
                    if not ws_id:
                        executed.append({"type": action.type, "error": "websearch_skill_not_found", "skill_name": action.skill_name})
                        continue
                    sched_payload = {
                        "web_search_skill_id": ws_id,
                        "hour": action.hour,
                        "minute": action.minute,
                        "timezone": action.timezone,
                        "channel": action.channel,
                        "destination": action.destination,
                    }
                    out = backend_post("/admin/websearch/schedules", admin_key=admin_key, json_body=sched_payload)
                    executed.append({"type": action.type, "scheduled": out})
                elif isinstance(action, ActionSkillConfigPatch):
                    patch_skill_config(db, user, action.skill_id, action.patch)
                    executed.append({"type": action.type, "skill_id": action.skill_id, "patch": action.patch})
                elif isinstance(action, ActionSkillsPrioritySet):
                    set_skills_priority_order(db, user, action.order)
                    executed.append({"type": action.type, "order": action.order})
                elif isinstance(action, ActionNoop):
                    executed.append({"type": action.type, "reason": action.reason})
                else:
                    executed.append({"type": "unknown_action"})
            except Exception as e:
                executed.append({"type": getattr(action, "type", "action"), "error": str(e)})

    # If we executed a websearch_run, prefer returning the backend-produced answer.
    try:
        for ex in executed:
            if ex.get('type') == 'websearch_run':
                res = ex.get('result') or {}
                if isinstance(res, dict) and isinstance(res.get('answer'), str) and res.get('answer').strip():
                    # If the plan only did websearch, return the answer directly.
                    if len(plan.actions) == 1 and getattr(plan.actions[0], 'type', None) == 'websearch_run':
                        plan.reply = res['answer'].strip()
                    else:
                        plan.reply = (plan.reply.rstrip() + "\n\n" + res['answer'].strip()).strip()
                    break
    except Exception:
        pass
    # Return fresh state snapshots for the UI
    ctx2 = _build_context_snapshot(db, user, admin_key=admin_key)
    return WizardTurnOut(
        reply=plan.reply,
        actions_executed=executed,
        websearch_skills=ctx2.get("websearch_skills") or [],
        websearch_schedules=ctx2.get("websearch_schedules") or [],
        skills_config=ctx2.get("skills_config") or {},
        skills_priority_order=ctx2.get("skills_priority_order") or [],
    )
