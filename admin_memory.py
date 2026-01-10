# admin_memory.py
"""Admin Memory endpoints for the Vozlia Control Plane.

Purpose:
- Expose long-term memory rows (caller_memory_events) to the Admin WebUI for debugging.
- Provide simple server-side search + pagination.
- Allow permanent deletion of individual rows.

Security:
- These routes MUST be protected by the control-plane admin key dependency passed in
  from control_main.py (X-Vozlia-Admin-Key).

Notes:
- This is *debug/admin* functionality. Keep the query logic simple and predictable.
- Search is a basic substring match (ILIKE). This is NOT vector search.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Text, cast, or_
from sqlalchemy.orm import Session

from deps import get_db
from models import CallerMemoryEvent


def _as_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC.

    We store created_at using UTC timestamps, but depending on the column type
    (timestamp without time zone) + driver behavior, SQLAlchemy may return a
    *naive* datetime. If we serialize a naive datetime, JS clients may treat it
    as *local* time, which can make rows look like they were created on the wrong
    day/time.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class LongTermMemoryRowOut(BaseModel):
    id: str
    created_at: datetime

    tenant_id: str
    caller_id: str
    call_sid: Optional[str] = None

    kind: str
    skill_key: str
    text: str

    # IMPORTANT:
    # Historical rows may store JSON as either an object OR an array.
    # Example from prod: tags_json = ["skill:call_summary"].
    # This is an admin/debug endpoint, so we keep these flexible to avoid 500s.
    data_json: Optional[Any] = None
    tags_json: Optional[Any] = None


class LongTermMemoryListOut(BaseModel):
    items: List[LongTermMemoryRowOut] = []
    has_more: bool = False
    next_offset: Optional[int] = None


class DeleteOut(BaseModel):
    ok: bool = True
    deleted_id: str


def build_memory_router(require_admin_key) -> APIRouter:
    """Return an APIRouter that is protected by the passed admin dependency."""
    router = APIRouter(
        prefix="/admin/memory",
        tags=["admin-memory"],
        dependencies=[Depends(require_admin_key)],
    )

    @router.get("/longterm", response_model=LongTermMemoryListOut)
    def list_longterm_memory(
        # Primary search param used by the WebUI
        q: Optional[str] = Query(default=None, description="Substring search across common fields (ILIKE)"),
        # Alias for backwards/forwards compatibility (some clients use `search`)
        search: Optional[str] = Query(default=None, description="Alias for `q` (legacy/compat)"),
        # Optional exact filters (useful for narrowing during debugging)
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
                    CallerMemoryEvent.text.ilike(like),
                    CallerMemoryEvent.skill_key.ilike(like),
                    cast(CallerMemoryEvent.caller_id, Text).ilike(like),
                    CallerMemoryEvent.call_sid.ilike(like),
                    CallerMemoryEvent.kind.ilike(like),
                    cast(CallerMemoryEvent.tenant_id, Text).ilike(like),
                    # Include meta JSON for debugging searches (cast JSONB->text)
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
                call_sid=str(r.call_sid) if r.call_sid else None,
                kind=str(r.kind),
                skill_key=str(r.skill_key),
                text=str(r.text),
                data_json=r.data_json,
                tags_json=r.tags_json,
            )
            for r in rows
        ]

        next_offset = (offset + limit) if has_more else None
        return LongTermMemoryListOut(items=items, has_more=has_more, next_offset=next_offset)

    @router.delete("/longterm/{memory_id}", response_model=DeleteOut)
    def delete_longterm_memory_row(
        memory_id: str,
        db: Session = Depends(get_db),
    ) -> DeleteOut:
        row = db.query(CallerMemoryEvent).filter(CallerMemoryEvent.id == memory_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Memory row not found")
        db.delete(row)
        db.commit()
        return DeleteOut(ok=True, deleted_id=memory_id)

    return router
