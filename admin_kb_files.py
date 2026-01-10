# admin_kb_files.py
"""
KB File Management (Control Plane)

Phase 1 scope:
- Per-tenant KB file upload (stored in object storage, metadata in Postgres)
- List / metadata / delete / download
- Upload/download are performed via short-lived signed tokens so the browser can talk
  directly to the Control Plane without ever receiving the admin key.

Security model:
- Admin endpoints require X-Vozlia-Admin-Key (require_admin_key dependency passed in).
- Upload/download endpoints require a signed KB token (HMAC).
- Every operation is tenant-scoped. tenant_id is required on all admin endpoints and is
  validated against stored metadata on id-based operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from deps import get_db
from kb_storage import KBStorageError, delete_object, stream_download, upload_fileobj
from kb_tokens import KBTokenError, mint_token, verify_token
from models import KBFile


def _utc(dt: datetime) -> datetime:
    # Ensure created_at is serialized in UTC.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# -------------------------
# Pydantic models
# -------------------------

class KBFileOut(BaseModel):
    id: str
    tenant_id: str
    kind: str
    status: str

    filename: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None

    storage_bucket: str
    storage_key: str

    uploaded_by: Optional[str] = None
    created_at: datetime


class KBFileListOut(BaseModel):
    items: List[KBFileOut]
    has_more: bool
    next_offset: Optional[int] = None


class UploadTokenIn(BaseModel):
    tenant_id: str
    filename: str
    content_type: Optional[str] = None
    kind: str = "knowledge"  # "knowledge" | "policy"
    uploaded_by: Optional[str] = None


class UploadTokenOut(BaseModel):
    upload_url: str
    upload_token: str
    expires_in_s: int


class DownloadTokenOut(BaseModel):
    download_url: str
    download_token: str
    expires_in_s: int


class DeleteOut(BaseModel):
    ok: bool
    deleted_id: str


# -------------------------
# Router builder
# -------------------------

def build_kb_router(require_admin_key):
    root = APIRouter()

    admin = APIRouter(
        prefix="/admin/kb/files",
        tags=["admin-kb"],
        dependencies=[Depends(require_admin_key)],
    )

    public = APIRouter(prefix="/kb", tags=["kb"])

    # ---- Admin endpoints ----

    @admin.post("/upload-token", response_model=UploadTokenOut)
    def create_upload_token(payload: UploadTokenIn, request: Request) -> UploadTokenOut:
        tenant_id = (payload.tenant_id or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")

        filename = (payload.filename or "").strip()
        if not filename:
            raise HTTPException(status_code=400, detail="filename is required")

        kind = (payload.kind or "knowledge").strip().lower()
        if kind not in ("knowledge", "policy"):
            raise HTTPException(status_code=400, detail="kind must be 'knowledge' or 'policy'")

        expires_in_s = 15 * 60
        upload_token = mint_token(
            {
                "op": "upload",
                "tenant_id": tenant_id,
                "filename": filename,
                "content_type": payload.content_type,
                "kind": kind,
                "uploaded_by": payload.uploaded_by,
            },
            ttl_seconds=expires_in_s,
        )

        base = str(request.base_url).rstrip("/")
        upload_url = f"{base}/kb/upload"

        return UploadTokenOut(upload_url=upload_url, upload_token=upload_token, expires_in_s=expires_in_s)

    @admin.get("", response_model=KBFileListOut)
    def list_kb_files(
        tenant_id: str = Query(..., description="Tenant scope (required)"),
        q: Optional[str] = Query(None, description="Search filename substring"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
    ) -> KBFileListOut:
        tenant_id = (tenant_id or "").strip()
        query = db.query(KBFile).filter(KBFile.tenant_id == tenant_id)

        if q:
            qq = f"%{q.strip()}%"
            query = query.filter(or_(KBFile.filename.ilike(qq), KBFile.kind.ilike(qq), KBFile.status.ilike(qq)))

        total = query.count()
        rows = (
            query.order_by(KBFile.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = [
            KBFileOut(
                id=r.id,
                tenant_id=r.tenant_id,
                kind=r.kind,
                status=r.status,
                filename=r.filename,
                content_type=r.content_type,
                size_bytes=r.size_bytes,
                sha256=r.sha256,
                storage_bucket=r.storage_bucket,
                storage_key=r.storage_key,
                uploaded_by=r.uploaded_by,
                created_at=_utc(r.created_at),
            )
            for r in rows
        ]

        has_more = (offset + limit) < total
        next_offset = (offset + limit) if has_more else None
        return KBFileListOut(items=items, has_more=has_more, next_offset=next_offset)

    @admin.get("/{file_id}", response_model=KBFileOut)
    def get_kb_file(
        file_id: str,
        tenant_id: str = Query(...),
        db: Session = Depends(get_db),
    ) -> KBFileOut:
        tenant_id = (tenant_id or "").strip()
        row = db.query(KBFile).filter(KBFile.id == file_id, KBFile.tenant_id == tenant_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="KB file not found")
        return KBFileOut(
            id=row.id,
            tenant_id=row.tenant_id,
            kind=row.kind,
            status=row.status,
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            storage_bucket=row.storage_bucket,
            storage_key=row.storage_key,
            uploaded_by=row.uploaded_by,
            created_at=_utc(row.created_at),
        )

    @admin.get("/{file_id}/download-token", response_model=DownloadTokenOut)
    def create_download_token(
        file_id: str,
        request: Request,
        tenant_id: str = Query(...),
        db: Session = Depends(get_db),
    ) -> DownloadTokenOut:
        tenant_id = (tenant_id or "").strip()
        row = db.query(KBFile).filter(KBFile.id == file_id, KBFile.tenant_id == tenant_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="KB file not found")

        expires_in_s = 15 * 60
        download_token = mint_token(
            {
                "op": "download",
                "tenant_id": tenant_id,
                "file_id": file_id,
            },
            ttl_seconds=expires_in_s,
        )

        base = str(request.base_url).rstrip("/")
        download_url = f"{base}/kb/download?token={download_token}"

        return DownloadTokenOut(download_url=download_url, download_token=download_token, expires_in_s=expires_in_s)

    @admin.get("/{file_id}/download")
    def admin_download(
        file_id: str,
        tenant_id: str = Query(...),
        db: Session = Depends(get_db),
    ):
        """
        Admin-key authenticated download (server-to-server).
        The Admin WebUI should prefer /download-token + /kb/download to avoid proxying large responses.
        """
        tenant_id = (tenant_id or "").strip()
        row = db.query(KBFile).filter(KBFile.id == file_id, KBFile.tenant_id == tenant_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="KB file not found")

        try:
            content_type, content_length, body = stream_download(bucket=row.storage_bucket, key=row.storage_key)
        except KBStorageError as e:
            raise HTTPException(status_code=500, detail=str(e))

        headers = {
            "Content-Disposition": f'attachment; filename="{row.filename}"',
        }
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        return StreamingResponse(body, media_type=content_type, headers=headers)

    @admin.delete("/{file_id}", response_model=DeleteOut)
    def delete_kb_file(
        file_id: str,
        tenant_id: str = Query(...),
        db: Session = Depends(get_db),
    ) -> DeleteOut:
        tenant_id = (tenant_id or "").strip()
        row = db.query(KBFile).filter(KBFile.id == file_id, KBFile.tenant_id == tenant_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="KB file not found")

        try:
            delete_object(bucket=row.storage_bucket, key=row.storage_key)
        except KBStorageError as e:
            # Still allow metadata delete if object is already gone?
            raise HTTPException(status_code=500, detail=str(e))

        db.delete(row)
        db.commit()
        return DeleteOut(ok=True, deleted_id=file_id)

    # ---- Public endpoints (token-auth) ----

    @public.post("/upload")
    def kb_upload(
        file: UploadFile = File(...),
        x_vozlia_upload_token: Optional[str] = Header(default=None, alias="X-Vozlia-Upload-Token"),
        db: Session = Depends(get_db),
    ):
        token = (x_vozlia_upload_token or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="Missing upload token")

        try:
            payload = verify_token(token)
        except KBTokenError as e:
            raise HTTPException(status_code=401, detail=str(e))

        if payload.get("op") != "upload":
            raise HTTPException(status_code=401, detail="Invalid token op")

        tenant_id = (payload.get("tenant_id") or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="Invalid token tenant_id")

        kind = (payload.get("kind") or "knowledge").strip().lower()
        if kind not in ("knowledge", "policy"):
            kind = "knowledge"

        filename = (payload.get("filename") or file.filename or "file").strip()
        content_type = (payload.get("content_type") or file.content_type or "application/octet-stream").strip()
        uploaded_by = (payload.get("uploaded_by") or None)

        # Upload to object storage
        try:
            file_id, bucket, size_bytes, sha256_hex, storage_key = upload_fileobj(
                tenant_id=tenant_id,
                filename=filename,
                content_type=content_type,
                fileobj=file.file,
            )
        except KBStorageError as e:
            raise HTTPException(status_code=500, detail=str(e))

        row = KBFile(
            id=file_id,
            tenant_id=tenant_id,
            kind=kind,
            status="uploaded",
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256_hex,
            storage_bucket=bucket,
            storage_key=storage_key,
            uploaded_by=uploaded_by,
        )
        db.add(row)
        db.commit()

        return {
            "ok": True,
            "file": {
                "id": file_id,
                "tenant_id": tenant_id,
                "kind": kind,
                "status": "uploaded",
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "sha256": sha256_hex,
                "created_at": _utc(row.created_at).isoformat(),
            },
        }

    @public.get("/download")
    def kb_download(
        token: str = Query(..., description="download token"),
        db: Session = Depends(get_db),
    ):
        try:
            payload = verify_token(token)
        except KBTokenError as e:
            raise HTTPException(status_code=401, detail=str(e))

        if payload.get("op") != "download":
            raise HTTPException(status_code=401, detail="Invalid token op")

        tenant_id = (payload.get("tenant_id") or "").strip()
        file_id = (payload.get("file_id") or "").strip()
        if not tenant_id or not file_id:
            raise HTTPException(status_code=400, detail="Invalid token")

        row = db.query(KBFile).filter(KBFile.id == file_id, KBFile.tenant_id == tenant_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="KB file not found")

        try:
            content_type, content_length, body = stream_download(bucket=row.storage_bucket, key=row.storage_key)
        except KBStorageError as e:
            raise HTTPException(status_code=500, detail=str(e))

        headers = {
            "Content-Disposition": f'attachment; filename="{row.filename}"',
        }
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        return StreamingResponse(body, media_type=content_type, headers=headers)

    root.include_router(admin)
    root.include_router(public)
    return root
