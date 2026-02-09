# services/config_wizard_service.py
"""
Configuration Wizard service (owner/admin only).

This is a "tool-first" agent design:
- LLM proposes *structured* actions (JSON).
- Control plane validates and executes deterministic operations.
- UI stays minimalist; capabilities live behind the wizard.

This significantly reduces hallucinations vs. "freeform" LLM answers.

Update (DB Query support):
- Adds dbquery_run + dbquery_skill_create actions so the wizard can answer and/or
  save internal analytics questions (calls, customers, KB docs, schedules, etc.)
  using the backend /admin/dbquery/* endpoints.
"""
from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, Literal, Annotated, Tuple

from pydantic import BaseModel, Field, ValidationError

from services.user_service import get_or_create_primary_user
from services.settings_service import (
    get_skills_config,
    patch_skill_config,
    get_skills_priority_order,
    set_skills_priority_order,
)
from services.backend_proxy import backend_get, backend_post, BackendProxyError
from services.wizard_grounding import rewrite_dbquery_spec_for_kb, build_grounded_reply_and_citations
from core.logging import env_flag

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from openai import OpenAI


def _patch_openai_by_alias_none() -> None:
    """Workaround for an upstream SDK issue.

    Some versions of openai-python (and other httpx+pydantic SDKs) can end up passing
    by_alias=None into pydantic's model_dump(), which raises:

        TypeError: argument 'by_alias': 'NoneType' object cannot be converted to 'PyBool'

    This patch coerces by_alias=None -> False inside openai._compat helpers.
    Safe: no behavior change when by_alias is already a bool.
    """
    try:
        import openai._compat as _compat  # type: ignore
    except Exception:
        return

    # Idempotent: only patch once per process.
    try:
        if getattr(_compat, "_vozlia_by_alias_patch", False):
            return
    except Exception:
        pass

    try:
        orig_model_dump = getattr(_compat, "model_dump", None)
        if callable(orig_model_dump):
            def _model_dump_patched(model, *args, **kwargs):
                if kwargs.get("by_alias", False) is None:
                    kwargs["by_alias"] = False
                return orig_model_dump(model, *args, **kwargs)
            _compat.model_dump = _model_dump_patched  # type: ignore
    except Exception:
        # Don't fail the wizard due to a logging/compat patch.
        return

    # Some SDKs also expose model_json_schema; patch similarly if present.
    try:
        orig_schema = getattr(_compat, "model_json_schema", None)
        if callable(orig_schema):
            def _model_json_schema_patched(model, *args, **kwargs):
                if kwargs.get("by_alias", False) is None:
                    kwargs["by_alias"] = False
                return orig_schema(model, *args, **kwargs)
            _compat.model_json_schema = _model_json_schema_patched  # type: ignore
    except Exception:
        pass

    try:
        setattr(_compat, "_vozlia_by_alias_patch", True)
    except Exception:
        pass


# Apply at import time so the first wizard call is safe.
_patch_openai_by_alias_none()

log = logging.getLogger("vozlia")

WIZARD_DEBUG_LOGS = env_flag("WIZARD_DEBUG_LOGS", "0", inherit_debug=True)



# -----------------------------
# Models (input)
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


# -----------------------------
# Models (DBQuery DSL)
# -----------------------------

FilterOp = Literal[
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "contains",
    "icontains",
    "in",
    "between",
    "is_null",
    "not_null",
    "has_concept",
]

AggOp = Literal["count", "count_distinct", "sum", "avg", "min", "max"]

OrderDir = Literal["asc", "desc"]

TimePreset = Literal[
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_7_days",
    "last_30_days",
]


class DBFilter(BaseModel):
    field: str = Field(..., min_length=1, max_length=80)
    op: FilterOp
    value: Any | None = None
    values: list[Any] | None = None


class DBAggregation(BaseModel):
    op: AggOp
    field: str | None = None  # None => count(*)
    as_name: str | None = None


class DBOrderBy(BaseModel):
    field: str = Field(..., min_length=1, max_length=80)
    direction: OrderDir = "desc"


class DBTimeframe(BaseModel):
    preset: TimePreset | None = None
    start: datetime | None = None
    end: datetime | None = None
    timezone: str = "America/New_York"


class DBQuerySpec(BaseModel):
    entity: str = Field(..., min_length=1, max_length=80)
    select: list[str] | None = None
    filters: list[DBFilter] = Field(default_factory=list)
    timeframe: DBTimeframe | None = None
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[DBAggregation] | None = None
    order_by: list[DBOrderBy] = Field(default_factory=list)
    limit: int = Field(default=25, ge=1, le=200)


# -----------------------------
# Models (actions)
# -----------------------------

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


class ActionDBQueryRun(BaseModel):
    type: Literal["dbquery_run"] = "dbquery_run"
    spec: DBQuerySpec
    # If true, suggest saving as a skill (wizard asks user).
    suggest_skill: bool = False


class ActionDBQuerySkillCreate(BaseModel):
    type: Literal["dbquery_skill_create"] = "dbquery_skill_create"
    name: str = Field(..., min_length=1, max_length=80)
    entity: str = Field(..., min_length=1, max_length=80)
    spec: DBQuerySpec
    triggers: List[str] = Field(default_factory=list, max_length=20)


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
        ActionDBQueryRun,
        ActionDBQuerySkillCreate,
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
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    grounding: Dict[str, Any] = Field(default_factory=dict)
    # Fresh state snapshots for UI refresh
    websearch_skills: List[Dict[str, Any]] = Field(default_factory=list)
    websearch_schedules: List[Dict[str, Any]] = Field(default_factory=list)
    dbquery_skills: List[Dict[str, Any]] = Field(default_factory=list)
    dbquery_entities: Dict[str, Any] = Field(default_factory=dict)
    skills_config: Dict[str, Any] = Field(default_factory=dict)
    skills_priority_order: List[str] = Field(default_factory=list)


# -----------------------------
# Helpers
# -----------------------------

DEFAULT_TIMEZONE = "America/New_York"

# Cache the expensive backend inventory calls the wizard needs (skills/schedules/entities).
# This reduces control-plane log volume and avoids spamming the backend during UI polling.
WIZARD_CONTEXT_CACHE_TTL_S = int((os.getenv("WIZARD_CONTEXT_CACHE_TTL_S") or "10").strip() or "10")
_WIZARD_CONTEXT_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _env_flag(name: str, default: str = "0") -> bool:
    v = (os.getenv(name, default) or default).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

# Workflow flag:
# - When disabled (default), the wizard answers questions without asking "want to save as a skill?"
#   The portal UI can still offer a "Save as skill" button.
# - When enabled, the wizard may append a follow-up prompt to save/schedule.
OFFER_SAVE_AFTER_QUERY = False  # hard-disabled (use UI Save-as-Skill button)

# Feature flag:
# - When enabled, some quantitative questions will be routed to the backend metrics engine.
# - Default OFF to avoid breaking the wizard if the metrics endpoint is not deployed.
# Feature flag:
# - When enabled, some quantitative questions will be routed to the backend metrics engine.
# - Default ON for now; can be disabled if the backend metrics endpoint is not deployed.
WIZARD_METRICS_FASTPATH_ENABLED = _env_flag('WIZARD_METRICS_FASTPATH_ENABLED', '1')

# Tighten the "metrics" detector so KB/menu questions (e.g. “how many steak dishes on the menu?”)
# do not get misrouted to the metrics engine.
WIZARD_METRICS_FASTPATH_STRICT = _env_flag('WIZARD_METRICS_FASTPATH_STRICT', '1')

_METRIC_HINTS = ("how many", "number of", "how often", "times", "count", "unique", "most", "top", "least", "total")
_METRICS_TARGET_HINTS = ("caller", "call", "calls", "lead", "leads", "conversation", "conversations", "turn", "turns", "message", "messages", "sms", "voicemail", "email")
_KB_EXCLUDE_HINTS = ("menu", "dish", "dishes", "beverage", "drink", "drinks", "coffee", "smoothie", "steak", "salad", "sandwich")

def _looks_like_metric_question(message: str) -> bool:
    t = (message or "").strip().lower()
    if not t:
        return False

    if not any(h in t for h in _METRIC_HINTS):
        return False

    if WIZARD_METRICS_FASTPATH_STRICT:
        # If it's clearly a KB/menu question, do not send it through metrics.
        if any(h in t for h in _KB_EXCLUDE_HINTS):
            return False
        return any(h in t for h in _METRICS_TARGET_HINTS)

    # Legacy heuristic: any count-like phrase triggers metrics.
    return True


def _extract_explicit_concept_codes(message: str) -> List[str]:
    """Extract explicit concept codes when the user says 'concept ...'.

    We intentionally only trigger when the word 'concept' is present to avoid
    accidentally treating domains or other dotted strings as concept codes.
    """
    t = (message or "").strip()
    if not t or not re.search(r"\bconcept\b", t, flags=re.IGNORECASE):
        return []

    # Examples we want to capture: menu.steak, faq.delivery-hours
    candidates = re.findall(r"\b([a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+)\b", t, flags=re.IGNORECASE)

    out: List[str] = []
    seen: set[str] = set()
    for c in candidates:
        c_norm = c.strip(".").lower()
        # Defensive: avoid a couple common domains if they ever show up in text.
        if c_norm in ("render.com", "onrender.com"):
            continue
        if c_norm not in seen:
            seen.add(c_norm)
            out.append(c_norm)
    return out


def _looks_like_count_request(message: str) -> bool:
    t = (message or "").lower()
    return any(k in t for k in ("how many", "count", "number of", "return just the count", "just the count"))


_SKILL_ALIASES: Dict[str, List[str]] = {
    "gmail_summary": ["gmail summary", "email summaries", "email summary", "emails summary"],
    "investment_reporting": ["investment report", "investment reporting", "stock report", "stocks report", "stock reporting"],
    "web_search": ["web search", "websearch", "internet search"],
}

_STOPWORDS = {
    "a", "an", "the", "my", "me", "please", "give", "show", "run", "do", "get", "tell",
    "today", "todays", "this", "that", "for", "to", "of", "in", "on", "at", "and", "or",
    "is", "are", "was", "were",
}


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _norm_tokens(s: str) -> List[str]:
    t = _normalize(s)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    toks = [w for w in t.split(" ") if w and w not in _STOPWORDS]
    return toks



def _looks_like_save_prompt(text: str) -> bool:
    t = _normalize(text)
    # Keep this fuzzy; UI copy may change.
    return ("save this as a skill" in t) or ("save as a skill" in t) or ("save it as a skill" in t)


def _is_affirmative(text: str) -> bool:
    t = _normalize(text)
    return t in ("y", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please do", "do it", "save it", "save")


def _is_negative(text: str) -> bool:
    t = _normalize(text)
    return t in ("n", "no", "nope", "nah", "not now", "no thanks", "no thank you", "dont", "don't", "do not", "don't save", "dont save", "skip")

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




_AGG_EXPR_RE = re.compile(r"^(count_distinct|count|sum|avg|min|max)\(\s*(\*|[A-Za-z0-9_]+)?\s*\)$", re.IGNORECASE)


def _coerce_time_preset(preset: Any) -> Any:
    if not isinstance(preset, str):
        return preset
    t = _normalize(preset).replace("-", "_").replace(" ", "_")
    # Common variants
    mapping = {
        "thisweek": "this_week",
        "this_week": "this_week",
        "current_week": "this_week",
        "lastweek": "last_week",
        "last_week": "last_week",
        "thismonth": "this_month",
        "this_month": "this_month",
        "last7days": "last_7_days",
        "last_7_days": "last_7_days",
        "last30days": "last_30_days",
        "last_30_days": "last_30_days",
        "yesterday": "yesterday",
        "today": "today",
    }
    return mapping.get(t, t)


def _coerce_filters(raw: Any) -> list[dict]:
    if raw is None:
        return []
    out: list[dict] = []

    # Dict form: {"kind":"turn"} => eq filters
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k and v is not None:
                out.append({"field": str(k), "op": "eq", "value": v})
        return out

    if not isinstance(raw, list):
        return []

    for item in raw:
        if item is None:
            continue

        # String shortcuts: "kind=turn"
        if isinstance(item, str):
            s = item.strip()
            m = re.match(r"^([A-Za-z0-9_\.]+)\s*(=|:|!=|>=|<=|>|<)\s*(.+)$", s)
            if m:
                field = m.group(1)
                op_sym = m.group(2)
                val = m.group(3).strip().strip('"').strip("'")
                sym2op = {
                    "=": "eq",
                    ":": "eq",
                    "!=": "ne",
                    ">": "gt",
                    "<": "lt",
                    ">=": "gte",
                    "<=": "lte",
                }
                out.append({"field": field, "op": sym2op.get(op_sym, "eq"), "value": val})
            continue

        if not isinstance(item, dict):
            continue

        # Already in correct-ish shape
        if "field" in item and "op" in item:
            d = dict(item)
            # normalize a few common alias keys
            if "as" in d and "as_name" not in d:
                d["as_name"] = d.pop("as")
            out.append(d)
            continue

        # Single-pair dict: {"kind":"turn"}
        if len(item.keys()) == 1:
            k = next(iter(item.keys()))
            v = item.get(k)
            out.append({"field": str(k), "op": "eq", "value": v})
            continue

        # Multi-key dict without explicit op: treat each key as eq
        for k, v in item.items():
            if k and v is not None:
                out.append({"field": str(k), "op": "eq", "value": v})

    return out


def _parse_agg_expr(expr: Any) -> tuple[str, str | None] | None:
    if not isinstance(expr, str):
        return None
    s = expr.strip()
    m = _AGG_EXPR_RE.match(s)
    if not m:
        return None
    op = m.group(1).lower()
    field = (m.group(2) or "").strip() or None
    if field == "*" or field == "":
        field = None
    return op, field


def _coerce_aggs(raw: Any) -> list[dict] | None:
    if raw is None:
        return None

    # Dict form: {"calls":"count_distinct(call_sid)"}
    if isinstance(raw, dict):
        raw = [raw]

    if not isinstance(raw, list):
        return None

    out: list[dict] = []
    for item in raw:
        if item is None:
            continue

        if isinstance(item, str):
            parsed = _parse_agg_expr(item)
            if parsed:
                op, field = parsed
                out.append({"op": op, "field": field, "as_name": None})
            continue

        if not isinstance(item, dict):
            continue

        # Proper-ish format already
        if "op" in item:
            d = dict(item)
            # normalize common aliases
            if "as" in d and "as_name" not in d:
                d["as_name"] = d.pop("as")
            if "name" in d and "as_name" not in d:
                d["as_name"] = d.pop("name")
            out.append(d)
            continue

        # Single-key dict: {"calls":"count_distinct(call_sid)"}
        if len(item.keys()) == 1:
            alias = next(iter(item.keys()))
            expr = item.get(alias)
            parsed = _parse_agg_expr(expr)
            if parsed:
                op, field = parsed
                out.append({"op": op, "field": field, "as_name": str(alias)})
            continue

        # Multi-key dict: {"calls":"count_distinct(call_sid)","unique_callers":"count_distinct(caller_id)"}
        for alias, expr in item.items():
            parsed = _parse_agg_expr(expr)
            if parsed:
                op, field = parsed
                out.append({"op": op, "field": field, "as_name": str(alias)})

    return out or None


def _coerce_dbquery_spec_dict(spec: Any, *, default_tz: str) -> dict:
    if not isinstance(spec, dict):
        return {}
    s = dict(spec)

    # timeframe normalization
    tf = s.get("timeframe")
    if isinstance(tf, dict):
        tf2 = dict(tf)
        if "timezone" not in tf2 or not tf2.get("timezone"):
            tf2["timezone"] = default_tz
        if "preset" in tf2:
            tf2["preset"] = _coerce_time_preset(tf2.get("preset"))
        s["timeframe"] = tf2

    # filters + aggregations normalization
    s["filters"] = _coerce_filters(s.get("filters"))
    aggs = _coerce_aggs(s.get("aggregations"))
    if aggs is not None:
        s["aggregations"] = aggs

    # limit normalization
    try:
        if "limit" in s:
            s["limit"] = int(s["limit"])
    except Exception:
        s["limit"] = 25

    return s


def _coerce_plan_dict(data: Any, *, default_tz: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    out = dict(data)
    actions = out.get("actions")
    if actions is None:
        out["actions"] = []
        return out
    if not isinstance(actions, list):
        out["actions"] = []
        return out

    fixed_actions: list[Any] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        ad = dict(a)
        t = ad.get("type")
        if t in ("dbquery_run", "dbquery_skill_create"):
            spec = ad.get("spec")
            ad["spec"] = _coerce_dbquery_spec_dict(spec, default_tz=default_tz)
            # If an entity key exists at the action level, propagate into spec
            ent = ad.get("entity")
            if isinstance(ent, str) and ent.strip() and isinstance(ad.get("spec"), dict):
                ad["spec"]["entity"] = ent.strip()
        fixed_actions.append(ad)

    out["actions"] = fixed_actions
    return out


def _tz_from_text(text: str, default_tz: str) -> str:
    t = (text or "").lower()
    # Common shorthands
    if re.search(r"\b(est|edt|et|eastern)\b", t):
        return "America/New_York"
    if re.search(r"\b(cst|cdt|ct|central)\b", t):
        return "America/Chicago"
    if re.search(r"\b(pst|pdt|pt|pacific)\b", t):
        return "America/Los_Angeles"
    # If user provided an IANA timezone-like token, trust it
    m = re.search(r"\b[A-Za-z]+\/[A-Za-z_]+\b", text or "")
    if m:
        return m.group(0)
    return default_tz


def _parse_time_hm(text: str) -> Optional[Tuple[int, int]]:
    """Parses times like:
      - 23:10
      - 11:10 PM
      - 11:10PM
      - 11 PM (treated as 11:00 PM)
    """
    t = (text or "").strip()
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
    m = re.search(r"'([^']+)'", text or "")
    if m:
        return m.group(1).strip()
    m = re.search(r"\"([^\"]+)\"", text or "")
    if m:
        return m.group(1).strip()
    return None


def _infer_time_preset(text: str) -> Optional[TimePreset]:
    t = _normalize(text)
    if "today" in t:
        return "today"
    if "yesterday" in t:
        return "yesterday"
    if "this week" in t:
        return "this_week"
    if "last week" in t:
        return "last_week"
    if "this month" in t:
        return "this_month"
    if "last 7 days" in t or "past 7 days" in t:
        return "last_7_days"
    if "last 30 days" in t or "past 30 days" in t:
        return "last_30_days"
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


def _fastpath_calls_count(payload: WizardTurnIn) -> Optional[WizardPlan]:
    """
    Deterministic analytics fast-path for common owner questions:
      - "how many calls did we receive today/this week/yesterday?"
      - "how many customers called today?"

    Uses entity=caller_memory_events (tenant-scoped) with kind="turn".

    This bypasses the LLM for reliability, but still uses the backend DBQuery engine.
    """
    msg = (payload.message or "").strip()
    if not msg:
        return None

    t = _normalize(msg)

    # Must look like a count question.
    if not (("how many" in t) or ("number of" in t) or ("count" in t)):
        return None
    if "call" not in t and "caller" not in t and "customer" not in t:
        return None

    preset = _infer_time_preset(t) or "this_week"
    tz = _tz_from_text(t, payload.default_timezone or DEFAULT_TIMEZONE)

    # Decide metric: calls vs unique callers.
    metric_unique = any(w in t for w in ["customer", "customers", "caller", "callers", "unique"])
    if metric_unique:
        agg = DBAggregation(op="count_distinct", field="caller_id", as_name="unique_callers")
        noun = "unique callers"
    else:
        # Calls: count distinct call_sid (only when present).
        agg = DBAggregation(op="count_distinct", field="call_sid", as_name="calls")
        noun = "calls"

    spec = DBQuerySpec(
        entity="caller_memory_events",
        timeframe=DBTimeframe(preset=preset, timezone=tz),
        filters=[
            DBFilter(field="kind", op="eq", value="turn"),
            DBFilter(field="call_sid", op="not_null"),
        ],
        aggregations=[agg],
        limit=25,
    )

    reply = f"Okay — I'll check your {noun} for {preset.replace('_', ' ')} ({tz})."
    return WizardPlan(
        reply=reply,
        actions=[ActionDBQueryRun(spec=spec, suggest_skill=OFFER_SAVE_AFTER_QUERY)],
    )




def _humanize_preset(preset: str) -> str:
    p = (preset or "").strip()
    mapping = {
        "today": "Today",
        "yesterday": "Yesterday",
        "this_week": "This Week",
        "last_week": "Last Week",
        "this_month": "This Month",
        "last_7_days": "Last 7 Days",
        "last_30_days": "Last 30 Days",
    }
    return mapping.get(p, p.replace("_", " ").title() if p else "Recent")


def _fastpath_save_followup(payload: WizardTurnIn, ctx: Dict[str, Any]) -> Optional[WizardPlan]:
    """
    Handles the common follow-up after the wizard asks:
      "Want me to save this as a Skill ... ?"

    Without this, a short user reply like "no" can trigger an LLM plan that
    may be schema-invalid, leading to the "action plan didn’t match" error.

    Behavior:
      - If user says NO: acknowledge and do not save. If it looks like we never actually
        returned the underlying answer (e.g., backend call failed), we re-run the last
        question deterministically when possible.
      - If user says YES: save a default skill immediately (MVP) and offer scheduling next.
    """
    if not payload.messages:
        return None

    user_msg = (payload.message or "").strip()
    if not user_msg:
        return None

    if not (_is_affirmative(user_msg) or _is_negative(user_msg)):
        return None

    # Find the most recent assistant message that contained the "save as a Skill" prompt.
    idx_save: int | None = None
    save_text = ""
    msgs = payload.messages
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i] or {}
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and _looks_like_save_prompt(m.get("content") or ""):
            idx_save = i
            save_text = m.get("content") or ""
            break
    if idx_save is None:
        return None

    # Find the original user question that preceded the save prompt.
    orig_q: str | None = None
    for j in range(idx_save - 1, -1, -1):
        m = msgs[j] or {}
        if m.get("role") == "user" and isinstance(m.get("content"), str) and (m.get("content") or "").strip():
            orig_q = (m.get("content") or "").strip()
            break

    # Heuristic: if the assistant message already contained an answer (numbers / "here's what I found"),
    # then a "no" should just decline saving, without re-running.
    save_norm = _normalize(save_text)
    assistant_looked_like_answer = bool(re.search(r"\bhere['’]?s\b.*\bfound\b", save_norm)) or bool(re.search(r"\d", save_text))

    if _is_negative(user_msg):
        # If the answer seemed missing, attempt to re-run deterministically from the previous question.
        if (not assistant_looked_like_answer) and orig_q:
            # Try DB fastpaths first (calls/customers), else fall back to websearch.
            tmp = WizardTurnIn(
                message=orig_q,
                default_timezone=payload.default_timezone,
                default_channel=payload.default_channel,
                default_destination=payload.default_destination,
                dry_run=payload.dry_run,
            )
            p = _fastpath_calls_count(tmp)
            if p and p.actions and isinstance(p.actions[0], ActionDBQueryRun):
                spec = p.actions[0].spec
                return WizardPlan(
                    reply="Okay — here you go (not saving as a Skill).",
                    actions=[
                        ActionDBQueryRun(spec=spec, suggest_skill=False),
                        ActionNoop(reason="declined_save_as_skill"),
                    ],
                )
            # Best-effort fallback: treat as a websearch question.
            return WizardPlan(
                reply="Okay — here you go (not saving as a Skill).",
                actions=[
                    ActionWebSearchRun(query=orig_q, suggest_skill=False),
                    ActionNoop(reason="declined_save_as_skill"),
                ],
            )

        return WizardPlan(
            reply="Okay — I won’t save this as a Skill. Anything else you’d like to set up?",
            actions=[ActionNoop(reason="declined_save_as_skill")],
        )

    # YES path (MVP): save a default skill immediately.
    if not orig_q:
        return WizardPlan(
            reply="Okay — what would you like to name this Skill?",
            actions=[ActionNoop(reason="need_skill_name")],
        )

    # Prefer DB fastpath-derived spec when applicable.
    tmp = WizardTurnIn(
        message=orig_q,
        default_timezone=payload.default_timezone,
        default_channel=payload.default_channel,
        default_destination=payload.default_destination,
        dry_run=payload.dry_run,
    )
    p = _fastpath_calls_count(tmp)
    if p and p.actions and isinstance(p.actions[0], ActionDBQueryRun):
        # Derive a stable default name/triggers.
        preset = "this_week"
        tz = payload.default_timezone or DEFAULT_TIMEZONE
        try:
            if WIZARD_DEBUG_LOGS:
                log.info("WIZARD_METRICS_FASTPATH_TAKE tz=%s", tz)
        except Exception:
            pass
        try:
            if p.actions[0].spec.timeframe and p.actions[0].spec.timeframe.preset:
                preset = str(p.actions[0].spec.timeframe.preset)
            if p.actions[0].spec.timeframe and p.actions[0].spec.timeframe.timezone:
                tz = str(p.actions[0].spec.timeframe.timezone)
        except Exception:
            pass

        metric_unique = any(w in _normalize(orig_q) for w in ["customer", "customers", "caller", "callers", "unique"])
        base_name = f"{'Unique Callers' if metric_unique else 'Calls'} {_humanize_preset(preset)}"
        name = base_name.strip()
        triggers = [orig_q]
        # Add a short trigger variant
        if metric_unique:
            triggers.append(f"callers {preset.replace('_', ' ')}")
        else:
            triggers.append(f"calls {preset.replace('_', ' ')}")
        # De-dupe
        seen = set()
        triggers2 = []
        for t in triggers:
            t2 = (t or '').strip()
            if not t2:
                continue
            key = _normalize(t2)
            if key in seen:
                continue
            seen.add(key)
            triggers2.append(t2)

        return WizardPlan(
            reply=f"Saved. I created a Skill called '{name}'. Want to schedule it (time + timezone + delivery destination)?",
            actions=[
                ActionDBQuerySkillCreate(
                    name=name,
                    entity="caller_memory_events",
                    spec=p.actions[0].spec,
                    triggers=triggers2[:20],
                )
            ],
        )

    # Fallback: websearch skill
    default_name = f"WebSearch: {orig_q[:48].strip()}" if len(orig_q) > 10 else f"WebSearch: {orig_q.strip()}"
    return WizardPlan(
        reply=f"Saved. I created a Skill called '{default_name}'. Want to schedule it (time + timezone + delivery destination)?",
        actions=[ActionWebSearchSkillCreate(name=default_name, query=orig_q, triggers=[orig_q])],
    )


def _build_context_snapshot(db, user, admin_key: str, *, force_refresh: bool = False) -> Dict[str, Any]:
    """Build (or reuse) the wizard context snapshot.

    This snapshot is used to:
    - provide the LLM planner with current skill/schedule inventories
    - resolve IDs in follow-up actions
    - refresh the UI after a mutating action (skill create/schedule upsert)

    To reduce log volume + backend churn, we cache this snapshot briefly.
    """
    # Cache key is per-user (single-tenant today, but keep the key explicit).
    user_id = getattr(user, "id", None) or "primary"
    cache_key = f"wizard_ctx:{user_id}"
    now = time.time()

    if not force_refresh and WIZARD_CONTEXT_CACHE_TTL_S > 0:
        hit = _WIZARD_CONTEXT_CACHE.get(cache_key)
        if hit:
            ts, snap = hit
            if (now - ts) < WIZARD_CONTEXT_CACHE_TTL_S and isinstance(snap, dict):
                # Keep now_utc fresh even when the rest of the snapshot is cached.
                out = dict(snap)
                out["now_utc"] = datetime.utcnow().isoformat() + "Z"
                return out

    skills_config = get_skills_config(db, user)
    skills_priority = get_skills_priority_order(db, user)

    # Websearch resources live in backend; fetch via backend admin endpoints.
    try:
        websearch_skills = backend_get("/admin/websearch/skills", admin_key=admin_key)
    except Exception:
        websearch_skills = []
    try:
        websearch_schedules = backend_get("/admin/websearch/schedules", admin_key=admin_key)
    except Exception:
        websearch_schedules = []

    # DBQuery resources live in backend; fetch via backend admin endpoints.
    try:
        dbquery_skills = backend_get("/admin/dbquery/skills", admin_key=admin_key)
    except Exception:
        dbquery_skills = []
    try:
        ents = backend_get("/admin/dbquery/entities", admin_key=admin_key)
        # backend returns {"entities": {...}}
        dbquery_entities = (ents or {}).get("entities") if isinstance(ents, dict) else {}
    except Exception:
        dbquery_entities = {}

    # Normalize to lists
    if not isinstance(websearch_skills, list):
        websearch_skills = []
    if not isinstance(websearch_schedules, list):
        websearch_schedules = []
    if not isinstance(dbquery_skills, list):
        dbquery_skills = []

    snap = {
        "now_utc": datetime.utcnow().isoformat() + "Z",
        "skills_config": skills_config,
        "skills_priority_order": skills_priority,
        "websearch_skills": websearch_skills,
        "websearch_schedules": websearch_schedules,
        "dbquery_skills": dbquery_skills,
        "dbquery_entities": dbquery_entities,
    }

    if WIZARD_CONTEXT_CACHE_TTL_S > 0:
        _WIZARD_CONTEXT_CACHE[cache_key] = (now, snap)

    return snap


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
        skills_list.append({"skill_id": sid, "label": label, "enabled": bool(cfg.get("enabled", True)), "type": cfg.get("type")})

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

    dq_skills_list = [
        {"id": s.get("id"), "skill_key": s.get("skill_key"), "name": s.get("name"), "entity": s.get("entity"), "enabled": s.get("enabled", True)}
        for s in (context.get("dbquery_skills") or [])
    ]

    # Entity metadata can be large; pass only fields (already filtered by backend)
    dq_entities = context.get("dbquery_entities") or {}

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
- If the user asks for a schedule, use 24-hour time (hour 0-23, minute 0-59). Default timezone is \"{default_tz}\".
- If channel/destination are missing, you may use defaults: channel=\"{default_channel}\", destination=\"{default_dest}\" (if destination is non-empty). Otherwise ask the user.

Allowed actions (choose 0+):
1) websearch_run:
   {{ \"type\":\"websearch_run\", \"query\":\"...\", \"suggest_skill\": false }}

2) websearch_skill_create:
   {{ \"type\":\"websearch_skill_create\", \"name\":\"...\", \"query\":\"...\", \"triggers\":[\"...\"] }}

3) websearch_schedule_upsert:
   {{ \"type\":\"websearch_schedule_upsert\", \"web_search_skill_id\":\"<uuid>\" OR \"skill_name\":\"<name>\",
      \"hour\": 23, \"minute\": 10, \"timezone\":\"America/New_York\",
      \"channel\":\"email\"|\"sms\", \"destination\":\"user@example.com\" }}

4) dbquery_run:
   Use this when the owner asks about Vozlia's internal data (calls, customers, skills, schedules, KB docs, etc.).
   "suggest_skill" should usually be false. Set it to true ONLY if the user explicitly asks to save/create a skill.
   {{ \"type\":\"dbquery_run\", \"spec\": {{ \"entity\":\"caller_memory_events\", \"filters\":[...], \"timeframe\":{{\"preset\":\"today\", \"timezone\":\"America/New_York\"}}, \"aggregations\":[...] }}, \"suggest_skill\": true }}

5) dbquery_skill_create:
   Use this only when the owner explicitly requests creating/saving a DB-backed skill.
   {{ \"type\":\"dbquery_skill_create\", \"name\":\"...\", \"entity\":\"caller_memory_events\", \"spec\": {{ ...same as dbquery_run... }}, \"triggers\":[\"...\"] }}

6) skill_config_patch:
   {{ \"type\":\"skill_config_patch\", \"skill_id\":\"gmail_summary|investment_reporting|...\", \"patch\":{{ ... }} }}

7) skills_priority_set:
   {{ \"type\":\"skills_priority_set\", \"order\":[\"skill_id_1\",\"skill_id_2\", ...] }}

8) noop:
   {{ \"type\":\"noop\", \"reason\":\"...\" }}

DBQuery spec guidance (STRICT JSON SHAPE):
- Always choose an entity from the provided dbquery_entities context.
- Only reference fields listed for that entity.
- filters MUST be a list of objects like:
  [{{"field":"kind","op":"eq","value":"turn"}}, {{"field":"call_sid","op":"not_null"}}]
- timeframe is OPTIONAL: include it for time-bounded event tables. For kb_chunks/kb_files, OMIT timeframe (created_at is ingestion time).
- For kb_chunks, search content via {{"field":"text","op":"icontains","value":"<keyword>"}} and do NOT use kind for content labels.
- aggregations MUST be a list of objects like:
  [{{"op":"count_distinct","field":"call_sid","as_name":"calls"}}]

Example (calls this week):
{{ "type":"dbquery_run",
  "spec":{{
    "entity":"caller_memory_events",
    "timeframe":{{"preset":"this_week","timezone":"America/New_York"}},
    "filters":[{{"field":"kind","op":"eq","value":"turn"}},{{"field":"call_sid","op":"not_null"}}],
    "aggregations":[{{"op":"count_distinct","field":"call_sid","as_name":"calls"}}],
    "limit":25
  }},
  "suggest_skill": false
}}

Known built-in skill aliases:
- gmail_summary: \"email summaries\", \"gmail summary\"
- investment_reporting: \"investment report\", \"stock report\"

Context: current Vozlia state (read-only):
- skills: {json.dumps(skills_list)[:8000]}
- websearch_skills: {json.dumps(ws_skills_list)[:8000]}
- websearch_schedules: {json.dumps(ws_sched_list)[:8000]}
- dbquery_skills: {json.dumps(dq_skills_list)[:8000]}
- dbquery_entities: {json.dumps(dq_entities)[:8000]}

Return JSON shape:
{{
  \"reply\": \"<what you will do / what you need>\",
  \"actions\": [ ...allowed actions... ]
}}
""".strip()

    messages: List[Dict[str, str]] = []
    messages.append({"role": "system", "content": system})

    # Optional short history
    if payload.messages:
        for m in payload.messages[-12:]:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content[:2000]})

    messages.append({"role": "user", "content": payload.message[:4000]})

    # Prefer JSON-mode if supported by the model/client; fall back gracefully.
    req = {
        "model": model,
        "messages": messages,  # type: ignore
        "temperature": 0,
        "max_tokens": 850,
    }
    if os.getenv("WIZARD_LLM_JSON_MODE", "1") == "1":
        req["response_format"] = {"type": "json_object"}

    try:
        try:
            resp = client.chat.completions.create(**req)
        except TypeError:
            # Older client/model combos may not accept response_format
            req.pop("response_format", None)
            resp = client.chat.completions.create(**req)
    except Exception as e:
        log.exception("CONFIG_WIZARD_LLM_CALL_FAIL")
        return WizardPlan(
            reply=f"Wizard LLM call failed: {e}",
            actions=[ActionNoop(reason="llm_call_failed")],
        )

    content = (resp.choices[0].message.content or "").strip()
    data = _safe_json_loads(content)
    if not data:
        if os.getenv("WIZARD_LOG_RAW_PLAN", "0") == "1":
            log.warning("CONFIG_WIZARD_INVALID_JSON raw=%s", content[:4000])
        else:
            log.warning("CONFIG_WIZARD_INVALID_JSON (raw logging disabled) chars=%s", len(content))
        return WizardPlan(
            reply="I couldn’t produce a valid action plan. Please rephrase your request.",
            actions=[ActionNoop(reason="invalid_json")],
        )

    try:
        return WizardPlan.model_validate(data)
    except ValidationError as e:
        # Attempt a deterministic "shape repair" for common dbquery spec mistakes
        # (e.g., filters=[{"kind":"turn"}] or aggregations=[{"calls":"count_distinct(call_sid)"}]).
        try:
            fixed = _coerce_plan_dict(data, default_tz=default_tz)
            if fixed and fixed != data:
                try:
                    return WizardPlan.model_validate(fixed)
                except ValidationError:
                    pass
        except Exception:
            pass

        if os.getenv("WIZARD_LOG_RAW_PLAN", "0") == "1":
            log.warning("CONFIG_WIZARD_PLAN_VALIDATION_FAIL data=%s err=%s raw=%s", data, str(e), content[:4000])
        else:
            log.warning("CONFIG_WIZARD_PLAN_VALIDATION_FAIL data=%s err=%s", data, str(e))
        return WizardPlan(
            reply="I understood your request, but the action plan didn’t match the expected format. Please try again.",
            actions=[ActionNoop(reason="plan_schema_invalid")],
        )


# -----------------------------
# Public API
# -----------------------------

def run_wizard_turn(db, payload: WizardTurnIn, *, admin_key: str) -> WizardTurnOut:
    """Main entry point used by /admin/wizard/turn."""
    # Minimal breadcrumb so Render logs show the exact branch taken per wizard call.
    # Keep message truncated to avoid leaking large payloads into logs.
    try:
        _msg_snip = ((payload.message or "").replace("\n", " ")[:240]).strip()
    except Exception:
        _msg_snip = ""
    if WIZARD_DEBUG_LOGS:
        log.info("WIZARD_TURN_IN msg=%r dry_run=%s has_history=%s", _msg_snip, bool(payload.dry_run), bool(payload.messages))

    user = get_or_create_primary_user(db)

    # Load context once (skills & websearch/dbquery inventory). This is also used for id resolution.
    ctx = _build_context_snapshot(db, user, admin_key=admin_key)

    # Fill defaults from DB where possible.
    if not payload.default_destination:
        # Best effort: owner's email in DB becomes default recipient.
        if getattr(user, "email", None):
            payload.default_destination = user.email
    if not payload.default_timezone:
        payload.default_timezone = DEFAULT_TIMEZONE


    # Deterministic metrics fast-path: for quantitative questions, bypass LLM planning and call backend metrics engine.
    # This keeps portal troubleshooting aligned with voice metrics behavior (shared engine) and prevents numeric hallucinations.
    # NOTE: This path is optional and MUST NOT break the wizard if the backend metrics endpoint isn't deployed.
    if (
        WIZARD_METRICS_FASTPATH_ENABLED
        and _looks_like_metric_question(payload.message)
        and not (('create' in payload.message.lower()) and ('skill' in payload.message.lower()))
    ):
        tz = payload.default_timezone or DEFAULT_TIMEZONE
        try:
            if WIZARD_DEBUG_LOGS:
                log.info("WIZARD_METRICS_FASTPATH_TAKE tz=%s", tz)
        except Exception:
            pass
        try:
            out = backend_post(
                '/admin/metrics/run',
                admin_key=admin_key,
                json_body={'question': payload.message, 'timezone': tz},
            )
        except BackendProxyError as e:
            # Metrics is a convenience; fall back to normal planning on any upstream error.
            log.warning(
                'WIZARD_METRICS_FASTPATH_FAILED status=%s url=%s detail=%s',
                getattr(e, 'status_code', None),
                getattr(e, 'url', None),
                getattr(e, 'detail', None),
            )
        else:
            reply = out.get('spoken_summary') if isinstance(out, dict) else None
            if not reply:
                reply = 'I can’t compute that metric yet from the current database.'
            return WizardTurnOut(
                reply=reply,
                actions_executed=[{'type': 'metrics_run', 'question': payload.message, 'result': out}],
                websearch_skills=ctx.get('websearch_skills', []),
                websearch_schedules=ctx.get('websearch_schedules', []),
                dbquery_skills=ctx.get('dbquery_skills', []),
                dbquery_entities=ctx.get('dbquery_entities', {}),
            )
    
    # --- Explicit concept fastpath -------------------------------------------------
    # If the user explicitly provides a concept code (e.g. "Use concept menu.steak"),
    # bypass LLM planning and run a deterministic dbquery using has_concept.
    concept_codes = _extract_explicit_concept_codes(payload.message)
    if concept_codes:
        concept_code = concept_codes[0]

        try:
            if WIZARD_DEBUG_LOGS:
                log.info("WIZARD_CONCEPT_FASTPATH_TAKE concept=%s", concept_code)
        except Exception:
            pass

        # Prefer an existing dbquery skill's entity if it already references this concept.
        entity = "kb_chunks"
        for s in ctx.get("dbquery_skills", []) or []:
            spec0 = (s or {}).get("spec") or {}
            filters0 = spec0.get("filters") or []
            if any((f or {}).get("op") == "has_concept" and str((f or {}).get("value") or "").lower() == concept_code for f in filters0):
                entity = spec0.get("entity") or entity
                break

        is_count = _looks_like_count_request(payload.message)
        spec: Dict[str, Any] = {
            "entity": entity,
            "filters": [{"field": "id", "op": "has_concept", "value": concept_code}],
        }

        if is_count:
            spec["aggregations"] = [{"op": "count_distinct", "field": "id", "as_name": "count"}]
            spec["limit"] = 1
        else:
            if entity == "kb_chunks":
                spec["select"] = ["id", "file_id", "chunk_index", "text"]
                spec["order_by"] = [{"field": "chunk_index", "direction": "asc"}]
            spec["limit"] = 50

        try:
            result = backend_post("/admin/dbquery/run", admin_key=admin_key, json_body={"spec": spec}, timeout_s=20)
        except BackendProxyError as e:
            log.warning(
                "WIZARD_CONCEPT_FASTPATH_FAILED status=%s url=%s detail=%s",
                getattr(e, "status_code", None),
                getattr(e, "url", None),
                getattr(e, "detail", None),
            )
        else:
            reply = result.get("spoken_summary") if isinstance(result, dict) else None
            if not reply:
                reply = "Done."
            return WizardTurnOut(
                reply=reply,
                actions_executed=[{"type": "dbquery_run", "spec": spec, "result": result}],
                websearch_skills=ctx.get("websearch_skills", []),
                websearch_schedules=ctx.get("websearch_schedules", []),
                dbquery_skills=ctx.get("dbquery_skills", []),
                dbquery_entities=ctx.get("dbquery_entities", {}),
            )


    plan = (
        _fastpath_save_followup(payload, ctx)
        or _fastpath_schedule_websearch(payload, ctx)
        or _plan_with_llm(payload, ctx)
    )

    try:
        _types = []
        for a in (plan.actions or []):
            _types.append(getattr(a, "type", type(a).__name__))
        if WIZARD_DEBUG_LOGS:
            log.info("WIZARD_PLAN_OK actions=%s", _types)
    except Exception:
        pass

    executed: List[Dict[str, Any]] = []
    if payload.dry_run:
        executed.append({"type": "dry_run", "note": "No changes executed."})
    else:
        for action in plan.actions:
            try:
                if isinstance(action, ActionWebSearchRun):
                    out = backend_post("/admin/websearch/search", admin_key=admin_key, json_body={"query": action.query})
                    executed.append({"type": action.type, "query": action.query, "result": out})
                    if WIZARD_DEBUG_LOGS:
                        try:
                            n_items = None
                            if isinstance(out, dict):
                                items = out.get('items')
                                if isinstance(items, list):
                                    n_items = len(items)
                            log.info('WIZARD_ACTION_RESULT type=websearch_run items=%s', n_items)
                        except Exception:
                            pass

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

                elif isinstance(action, ActionDBQueryRun):
                    spec_dict = action.spec.model_dump()
                    spec2, meta = rewrite_dbquery_spec_for_kb(spec_dict, payload.message)
                    if WIZARD_DEBUG_LOGS and meta.get('changed'):
                        log.info('WIZARD_DBQUERY_REWRITE notes=%s', meta.get('notes'))
                    out = backend_post(
                        "/admin/dbquery/run",
                        admin_key=admin_key,
                        json_body={"spec": spec2},
                    )
                    executed.append({"type": action.type, "spec": spec2, "result": out, "rewrite": meta if meta.get('changed') else None})
                    if WIZARD_DEBUG_LOGS:
                        try:
                            log.info(
                                'WIZARD_ACTION_RESULT type=dbquery_run ok=%s entity=%s count=%s rows=%s',
                                (out.get('ok') if isinstance(out, dict) else None),
                                (out.get('entity') if isinstance(out, dict) else None),
                                (out.get('count') if isinstance(out, dict) else None),
                                (len(out.get('rows') or []) if isinstance(out, dict) and isinstance(out.get('rows'), list) else None),
                            )
                        except Exception:
                            pass

                elif isinstance(action, ActionDBQuerySkillCreate):
                    # Ensure entity consistency
                    spec = action.spec.model_dump()
                    spec["entity"] = action.entity
                    out = backend_post(
                        "/admin/dbquery/skills",
                        admin_key=admin_key,
                        json_body={"name": action.name, "entity": action.entity, "spec": spec, "triggers": action.triggers},
                    )
                    executed.append({"type": action.type, "created": out})

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
                log.exception("CONFIG_WIZARD_ACTION_EXEC_FAIL type=%s", getattr(action, "type", "action"))
                executed.append({"type": getattr(action, "type", "action"), "error": str(e)})

    # Build a grounded reply (with citations) from executed tool outputs.
    citations: List[Dict[str, Any]] = []
    grounding: Dict[str, Any] = {}
    try:
        grounded_reply, citations, grounding = build_grounded_reply_and_citations(payload.message, executed)
        if isinstance(grounded_reply, str) and grounded_reply.strip():
            plan.reply = grounded_reply.strip()
        if WIZARD_DEBUG_LOGS:
            log.info(
                'WIZARD_GROUNDING mode=%s refused=%s evidence=%s citations=%s',
                grounding.get('mode'), grounding.get('refused'), grounding.get('evidence_count'), grounding.get('citations_count')
            )
    except Exception:
        pass

    # If enabled, the wizard may offer a follow-up to save the last query as a skill.
    # Default is OFF to avoid interrupting normal Q&A; the UI can use the 'Save as Skill' button instead.
    if OFFER_SAVE_AFTER_QUERY:
        try:
            suggested = any(
                (getattr(a, "type", "") in ("websearch_run", "dbquery_run") and bool(getattr(a, "suggest_skill", False)))
                for a in plan.actions
            )
            if suggested and ("save" not in plan.reply.lower()) and ("skill" not in plan.reply.lower()):
                plan.reply = (plan.reply.rstrip() + "\n\nWant me to save this as a Skill you can trigger by name (and optionally schedule)?").strip()
        except Exception:
            pass


    # Only acknowledge 'declined save' if the autosave-offer workflow is enabled.
    if OFFER_SAVE_AFTER_QUERY:
        try:
            declined = any(
                (getattr(a, "type", "") == "noop" and str(getattr(a, "reason", "") or "") == "declined_save_as_skill")
                for a in plan.actions
            )
            if declined and ("won't save" not in plan.reply.lower()) and ("won’t save" not in plan.reply.lower()):
                plan.reply = (plan.reply.rstrip() + "\n\nOkay — I won’t save this as a Skill.").strip()
        except Exception:
            pass

    # Return fresh state snapshots for the UI
    # Only refresh the expensive context snapshot after mutating actions.
    _MUTATING_ACTIONS = {
        "websearch_skill_create",
        "websearch_schedule_upsert",
        "dbquery_skill_create",
        "skill_config_patch",
        "skills_priority_set",
    }
    needs_refresh = any((e.get("type") in _MUTATING_ACTIONS) for e in (executed or []))
    ctx2 = (
        _build_context_snapshot(db, user, admin_key=admin_key, force_refresh=True)
        if needs_refresh
        else ctx
    )
    if WIZARD_DEBUG_LOGS:
        try:
            log.info(
                'WIZARD_TURN_OUT reply_len=%s actions_executed=%s',
                (len((plan.reply or '')) if isinstance(plan.reply, str) else None),
                ([e.get('type') for e in executed] if isinstance(executed, list) else None),
            )
        except Exception:
            pass
    return WizardTurnOut(
        reply=plan.reply,
        actions_executed=executed,
        citations=citations,
        grounding=grounding,
        websearch_skills=ctx2.get("websearch_skills") or [],
        websearch_schedules=ctx2.get("websearch_schedules") or [],
        dbquery_skills=ctx2.get("dbquery_skills") or [],
        dbquery_entities=ctx2.get("dbquery_entities") or {},
        skills_config=ctx2.get("skills_config") or {},
        skills_priority_order=ctx2.get("skills_priority_order") or [],
    )