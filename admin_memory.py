# admin_memory.py
"""Admin Memory endpoints for the Vozlia Control Plane.

Purpose:
- Expose long-term memory rows (caller_memory_events) to the Admin WebUI for debugging.
- Provide simple server-side search + pagination.
- Allow permanent deletion of individual rows.
- Provide a DB-backed "turn feed" suitable for a ChatGPT-style debug console (scoped by tenant_id + call_sid).

Security:
- These routes MUST be protected by the control-plane admin key dependency passed in
  from control_main.py (X-Vozlia-Admin-Key).

Notes:
- This is *debug/admin* functionality. Keep the query logic simple and predictable.
- Search is a basic substring match (ILIKE). This is NOT vector search.
- Turn feed endpoints are read-only and return only kind='turn' events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Text, cast, func, or_
from sqlalchemy.orm import Session

from deps import get_db
from models import CallerMemoryEvent


def _as_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ----------------------------
# Existing longterm list/delete models
# ----------------------------

class LongTermMemoryRowOut(BaseModel):
    id: str
    created_at: datetime

    tenant_id: str
    caller_id: str
    call_sid: Optional[str] = None

    kind: str
    skill_key: str
    text: str

    data_json: Optional[Any] = None
    tags_json: Optional[Any] = None


class LongTermMemoryListOut(BaseModel):
    items: List[LongTermMemoryRowOut] = []
    has_more: bool = False
    next_offset: Optional[int] = None


class DeleteOut(BaseModel):
    ok: bool = True
    deleted_id: str


# ----------------------------
# New: call list + turn feed (for console chat)
# ----------------------------

class CallThreadOut(BaseModel):
    call_sid: str
    caller_id: Optional[str] = None
    first_at: datetime
    last_at: datetime
    turns: int


class CallThreadListOut(BaseModel):
    items: List[CallThreadOut] = []


class TurnSourceOut(BaseModel):
    file_id: Optional[str] = None
    filename: Optional[str] = None
    kind: Optional[str] = None
    chunk_index: Optional[int] = None
    score: Optional[float] = None


class TurnEventOut(BaseModel):
    id: str
    created_at: datetime
    role: str
    text: str
    # Optional debugging/citation fields if present in data_json
    call_sid: Optional[str] = None
    sources: Optional[List[TurnSourceOut]] = None
    data_json: Optional[Any] = None


class TurnEventListOut(BaseModel):
    items: List[TurnEventOut] = []
    has_more: bool = False
    next_since_ms: Optional[int] = None


def _extract_role(data_json: Any) -> str:
    if isinstance(data_json, dict):
        r = (data_json.get("role") or "").strip().lower()
        if r in ("user", "assistant", "system"):
            return r
    return "user"


def _extract_sources(data_json: Any) -> Optional[List[TurnSourceOut]]:
    """Attempt to extract KB sources from a turn event.

    We support several keys to make this forward-compatible:
      - data_json['kb_sources'] (preferred)
      - data_json['sources']
      - data_json['citations']

    Expected shapes:
      - list of dicts with (file_id, filename, kind, chunk_index, score)
    """
    if not isinstance(data_json, dict):
        return None
    raw = data_json.get("kb_sources") or data_json.get("sources") or data_json.get("citations")
    if not isinstance(raw, list):
        return None

    out: List[TurnSourceOut] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            TurnSourceOut(
                file_id=(str(item.get("file_id")) if item.get("file_id") is not None else None),
                filename=(str(item.get("filename")) if item.get("filename") is not None else None),
                kind=(str(item.get("kind")) if item.get("kind") is not None else None),
                chunk_index=(int(item.get("chunk_index")) if item.get("chunk_index") is not None else None),
                score=(float(item.get("score")) if item.get("score") is not None else None),
            )
        )
    return out or None


def build_memory_router(require_admin_key) -> APIRouter:
    """Return an APIRouter that is protected by the passed admin dependency."""
    router = APIRouter(
        prefix="/admin/memory",
        tags=["admin-memory"],
        dependencies=[Depends(require_admin_key)],
    )

    @router.get("/longterm", response_model=LongTermMemoryListOut)
    def list_longterm(
        q: Optional[str] = Query(default=None, description="Substring search"),
        search: Optional[str] = Query(default=None, description="Alias for q"),
        tenant_id: Optional[str] = Query(default=None),
        caller_id: Optional[str] = Query(default=None),
        call_sid: Optional[str] = Query(default=None),
        skill_key: Optional[str] = Query(default=None),
        kind: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> LongTermMemoryListOut:
        """List recent long-term memory events with optional filters.

        - default sort: newest first
        - pagination: offset/limit (+1 fetch to infer has_more)
        - search: ILIKE on common fields (and JSON payloads cast to text)
        """
        query = db.query(CallerMemoryEvent)

        # Exact filters first
        if tenant_id:
            query = query.filter(CallerMemoryEvent.tenant_id == tenant_id)
        if caller_id:
            query = query.filter(CallerMemoryEvent.caller_id == caller_id)
        if call_sid:
            query = query.filter(CallerMemoryEvent.call_sid == call_sid)
        if skill_key:
            query = query.filter(CallerMemoryEvent.skill_key == skill_key)
        if kind:
            query = query.filter(CallerMemoryEvent.kind == kind)

        # Substring search (server-side)
        q_str = (q or search or "").strip()
        if q_str:
            like = f"%{q_str}%"
            query = query.filter(
                or_(
                    cast(CallerMemoryEvent.tenant_id, Text).ilike(like),
                    cast(CallerMemoryEvent.caller_id, Text).ilike(like),
                    cast(CallerMemoryEvent.call_sid, Text).ilike(like),
                    cast(CallerMemoryEvent.kind, Text).ilike(like),
                    cast(CallerMemoryEvent.skill_key, Text).ilike(like),
                    cast(CallerMemoryEvent.text, Text).ilike(like),
                    cast(CallerMemoryEvent.tags_json, Text).ilike(like),
                    cast(CallerMemoryEvent.data_json, Text).ilike(like),
                )
            )

        query = query.order_by(CallerMemoryEvent.created_at.desc())

        rows = query.offset(offset).limit(limit + 1).all()
        has_more = len(rows) > limit
        rows = rows[:limit]

        items = [
            LongTermMemoryRowOut(
                id=str(r.id),
                created_at=_as_utc(r.created_at),
                tenant_id=str(r.tenant_id),
                caller_id=str(r.caller_id),
                call_sid=(str(r.call_sid) if r.call_sid else None),
                kind=str(r.kind),
                skill_key=str(r.skill_key),
                text=str(r.text),
                data_json=r.data_json,
                tags_json=r.tags_json,
            )
            for r in rows
        ]

        return LongTermMemoryListOut(
            items=items,
            has_more=has_more,
            next_offset=(offset + limit) if has_more else None,
        )

    @router.delete("/longterm/{memory_id}", response_model=DeleteOut)
    def delete_longterm(memory_id: str, db: Session = Depends(get_db)) -> DeleteOut:
        row = db.query(CallerMemoryEvent).filter(CallerMemoryEvent.id == memory_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="not found")

        db.delete(row)
        db.commit()
        return DeleteOut(ok=True, deleted_id=str(memory_id))

    # ----------------------------
    # NEW: Call threads list
    # ----------------------------
    @router.get("/calls", response_model=CallThreadListOut)
    def list_calls(
        tenant_id: str = Query(..., description="Tenant UUID (string)"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> CallThreadListOut:
        """List recent call threads for a tenant.

        We group by call_sid over kind='turn' events to produce a list of calls that
        have conversational turns captured.
        """
        q = (
            db.query(
                CallerMemoryEvent.call_sid.label("call_sid"),
                func.max(CallerMemoryEvent.created_at).label("last_at"),
                func.min(CallerMemoryEvent.created_at).label("first_at"),
                func.max(CallerMemoryEvent.caller_id).label("caller_id"),
                func.count(CallerMemoryEvent.id).label("turns"),
            )
            .filter(CallerMemoryEvent.tenant_id == tenant_id)
            .filter(CallerMemoryEvent.kind == "turn")
            .filter(CallerMemoryEvent.call_sid.isnot(None))
            .group_by(CallerMemoryEvent.call_sid)
            .order_by(func.max(CallerMemoryEvent.created_at).desc())
            .offset(offset)
            .limit(limit)
        )

        rows = q.all()
        items: List[CallThreadOut] = []
        for r in rows:
            if not r.call_sid:
                continue
            items.append(
                CallThreadOut(
                    call_sid=str(r.call_sid),
                    caller_id=(str(r.caller_id) if r.caller_id else None),
                    first_at=_as_utc(r.first_at),
                    last_at=_as_utc(r.last_at),
                    turns=int(r.turns or 0),
                )
            )

        return CallThreadListOut(items=items)

    # ----------------------------
    # NEW: Turn feed for a call
    # ----------------------------
    @router.get("/turns", response_model=TurnEventListOut)
    def list_turns(
        tenant_id: str = Query(..., description="Tenant UUID (string)"),
        call_sid: str = Query(..., description="Twilio Call SID"),
        since_ms: Optional[int] = Query(default=None, description="Only return turns after this UTC ms timestamp"),
        limit: int = Query(default=200, ge=1, le=500),
        db: Session = Depends(get_db),
    ) -> TurnEventListOut:
        """Return turn events for a call, ordered oldest->newest.

        This is designed for a polling UI. Use since_ms to fetch only new turns.
        """
        query = (
            db.query(CallerMemoryEvent)
            .filter(CallerMemoryEvent.tenant_id == tenant_id)
            .filter(CallerMemoryEvent.kind == "turn")
            .filter(CallerMemoryEvent.call_sid == call_sid)
            .order_by(CallerMemoryEvent.created_at.asc())
        )

        if since_ms is not None:
            # created_at is stored as naive UTC in most deployments; compare against a UTC-aware datetime.
            dt = datetime.fromtimestamp(float(since_ms) / 1000.0, tz=timezone.utc)
            query = query.filter(CallerMemoryEvent.created_at > dt.replace(tzinfo=None))

        rows = query.limit(limit + 1).all()
        has_more = len(rows) > limit
        rows = rows[:limit]

        items: List[TurnEventOut] = []
        max_ts_ms: Optional[int] = None
        for r in rows:
            created = _as_utc(r.created_at)
            ts_ms = int(created.timestamp() * 1000)
            if max_ts_ms is None or ts_ms > max_ts_ms:
                max_ts_ms = ts_ms

            role = _extract_role(r.data_json)
            items.append(
                TurnEventOut(
                    id=str(r.id),
                    created_at=created,
                    role=role,
                    text=str(r.text),
                    call_sid=(str(r.call_sid) if r.call_sid else None),
                    sources=_extract_sources(r.data_json),
                    data_json=r.data_json,
                )
            )

        next_since = (max_ts_ms + 1) if (max_ts_ms is not None and not has_more) else max_ts_ms
        return TurnEventListOut(items=items, has_more=has_more, next_since_ms=next_since)

    return router
