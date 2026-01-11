# kb_ingest.py
"""KB Phase 2: ingestion job queue + chunk storage (Control Plane).

Goals (Phase 2.0):
- Admin-only endpoint to enqueue ingestion for a KB file (per-tenant)
- Persist job status in Postgres (kb_ingest_jobs)
- Persist extracted/chunked text in Postgres (kb_chunks)

Notes:
- NO embeddings yet (Phase 2.1+)
- Designed to run ingestion asynchronously via a separate worker process
- Keep Web dyno responsive: enqueue is fast; worker does the heavy lifting
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, List
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Session

from db import Base


# -------------------------
# DB models (auto-created by Base.metadata.create_all)
# -------------------------

class KbIngestJob(Base):
    __tablename__ = "kb_ingest_jobs"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)

    tenant_id = Column(PGUUID(as_uuid=True), nullable=False, index=True)
    file_id = Column(PGUUID(as_uuid=True), ForeignKey("kb_files.id", ondelete="CASCADE"), nullable=False, index=True)

    # queued | running | ready | failed
    status = Column(String(24), nullable=False, default="queued", index=True)

    error = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index("ix_kb_ingest_tenant_status_created", "tenant_id", "status", "created_at"),
    )


class KbChunk(Base):
    __tablename__ = "kb_chunks"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)

    tenant_id = Column(PGUUID(as_uuid=True), nullable=False, index=True)
    file_id = Column(PGUUID(as_uuid=True), ForeignKey("kb_files.id", ondelete="CASCADE"), nullable=False, index=True)

    # Copy file kind ("knowledge" | "policy") for convenience at retrieval time
    kind = Column(String(24), nullable=False, default="knowledge", index=True)

    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index", name="uq_kb_chunks_file_chunk_index"),
        Index("ix_kb_chunks_tenant_file_chunk", "tenant_id", "file_id", "chunk_index"),
    )


# -------------------------
# API schemas
# -------------------------

class EnqueueIngestRequest(BaseModel):
    tenant_id: str = Field(..., description="Tenant UUID (required)")
    force: bool = Field(False, description="If true, enqueue even if a job already exists")


class IngestJobOut(BaseModel):
    id: str
    tenant_id: str
    file_id: str
    status: str
    error: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class EnqueueIngestResponse(BaseModel):
    ok: bool = True
    job: IngestJobOut


class ListIngestJobsResponse(BaseModel):
    items: List[IngestJobOut]
    has_more: bool
    next_offset: Optional[int] = None


# -------------------------
# Route registration helper
# -------------------------

# Type aliases to avoid importing from control_main (prevents circular imports)
RequireAdminFn = Callable[..., bool]
GetDbDep = Callable[..., Any]


def _now_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    try:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return dt.isoformat()


def _job_to_out(job: KbIngestJob) -> IngestJobOut:
    return IngestJobOut(
        id=str(job.id),
        tenant_id=str(job.tenant_id),
        file_id=str(job.file_id),
        status=str(job.status),
        error=job.error,
        created_at=_now_iso(job.created_at) or "",
        started_at=_now_iso(job.started_at),
        finished_at=_now_iso(job.finished_at),
    )


def _ensure_file_exists(db: Session, *, tenant_id: UUID, file_id: UUID) -> Dict[str, Any]:
    """Fetch minimal kb_files row. Uses SQL text to avoid tight coupling to KB file model."""
    row = (
        db.execute(
            text(
                """
                SELECT id, tenant_id, kind, content_type, filename
                FROM kb_files
                WHERE id = :file_id AND tenant_id = :tenant_id
                """
            ),
            {"file_id": str(file_id), "tenant_id": str(tenant_id)},
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="KB file not found")
    return dict(row)


def register_kb_ingest_routes(
    app: Any,
    *,
    require_admin: RequireAdminFn,
    get_db: GetDbDep,
) -> None:
    """Register admin-only KB ingest routes onto an existing FastAPI app."""

    @app.post("/admin/kb/files/{file_id}/ingest", response_model=EnqueueIngestResponse)
    def enqueue_kb_ingest(
        file_id: str,
        payload: EnqueueIngestRequest,
        admin_ok: bool = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        # Parse UUIDs
        try:
            tenant_uuid = UUID(payload.tenant_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid tenant_id (must be UUID)")
        try:
            file_uuid = UUID(file_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid file_id (must be UUID)")

        # Verify file ownership (tenant isolation)
        _ensure_file_exists(db, tenant_id=tenant_uuid, file_id=file_uuid)

        # If not forcing, avoid duplicate queued/running jobs
        if not payload.force:
            existing = (
                db.query(KbIngestJob)
                .filter(
                    KbIngestJob.tenant_id == tenant_uuid,
                    KbIngestJob.file_id == file_uuid,
                    KbIngestJob.status.in_(["queued", "running"]),
                )
                .order_by(KbIngestJob.created_at.desc())
                .first()
            )
            if existing:
                return EnqueueIngestResponse(ok=True, job=_job_to_out(existing))

        job = KbIngestJob(
            id=uuid4(),
            tenant_id=tenant_uuid,
            file_id=file_uuid,
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        return EnqueueIngestResponse(ok=True, job=_job_to_out(job))

    @app.get("/admin/kb/ingest-jobs", response_model=ListIngestJobsResponse)
    def list_kb_ingest_jobs(
        tenant_id: str = Query(..., description="Tenant UUID"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        admin_ok: bool = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        try:
            tenant_uuid = UUID(tenant_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid tenant_id (must be UUID)")

        q = (
            db.query(KbIngestJob)
            .filter(KbIngestJob.tenant_id == tenant_uuid)
            .order_by(KbIngestJob.created_at.desc())
        )

        items = q.offset(offset).limit(limit + 1).all()
        has_more = len(items) > limit
        items = items[:limit]

        next_offset = (offset + limit) if has_more else None

        return ListIngestJobsResponse(
            items=[_job_to_out(j) for j in items],
            has_more=has_more,
            next_offset=next_offset,
        )

    # Admin convenience: latest job status for a file
    @app.get("/admin/kb/files/{file_id}/ingest-status")
    def get_kb_file_ingest_status(
        file_id: str,
        tenant_id: str = Query(..., description="Tenant UUID"),
        admin_ok: bool = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        try:
            tenant_uuid = UUID(tenant_id)
            file_uuid = UUID(file_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid tenant_id or file_id (must be UUID)")

        # Verify file exists for tenant
        _ensure_file_exists(db, tenant_id=tenant_uuid, file_id=file_uuid)

        job = (
            db.query(KbIngestJob)
            .filter(KbIngestJob.tenant_id == tenant_uuid, KbIngestJob.file_id == file_uuid)
            .order_by(KbIngestJob.created_at.desc())
            .first()
        )
        if not job:
            return {"ok": True, "status": "not_ingested", "job": None}

        return {"ok": True, "status": job.status, "job": _job_to_out(job).model_dump()}
