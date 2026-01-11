# kb_ingest_worker.py
"""KB Phase 2 ingestion worker (Control Plane).

Run this as a separate Render Background Worker service.
It polls kb_ingest_jobs for queued work, downloads files from object storage,
extracts text (Phase 2.0: text/* only), chunks, and stores into kb_chunks.

Env vars:
  - DATABASE_URL (required)
  - KB_S3_ACCESS_KEY_ID / KB_S3_SECRET_ACCESS_KEY (required)
  - KB_S3_ENDPOINT (optional; for Cloudflare R2 or custom S3)
  - KB_S3_REGION (optional; default 'auto')
  - KB_INGEST_POLL_SECONDS (default 2)
  - KB_CHUNK_CHARS (default 1000)
  - KB_CHUNK_OVERLAP (default 200)
  - KB_INGEST_MAX_BYTES (default 5_000_000)
  - KB_WORKER_ONCE (default 0) -> if 1, process one job then exit
"""

from __future__ import annotations

import os
import time
import logging

logger = logging.getLogger("vozlia.kb_ingest_worker")
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

import boto3
from botocore.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import SessionLocal
from kb_ingest import KbIngestJob, KbChunk


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _s3_client():
    access = (os.getenv("KB_S3_ACCESS_KEY_ID") or "").strip()
    secret = (os.getenv("KB_S3_SECRET_ACCESS_KEY") or "").strip()
    if not access or not secret:
        raise RuntimeError("KB_S3_ACCESS_KEY_ID / KB_S3_SECRET_ACCESS_KEY not configured")

    endpoint = (os.getenv("KB_S3_ENDPOINT") or "").strip() or None
    region = (os.getenv("KB_S3_REGION") or "").strip() or "auto"

    sess = boto3.session.Session(
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=region,
    )
    return sess.client(
        "s3",
        endpoint_url=endpoint,
        config=Config(signature_version="s3v4"),
    )


def _get_kb_file_row(db: Session, *, tenant_id: UUID, file_id: UUID) -> Dict[str, Any]:
    row = (
        db.execute(
            text(
                """
                SELECT id, tenant_id, kind, content_type, filename, storage_bucket, storage_key, size_bytes
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
        raise RuntimeError("KB file not found (or tenant mismatch)")
    return dict(row)


def _download_object(s3, *, bucket: str, key: str, max_bytes: int) -> bytes:
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    if len(body) > max_bytes:
        raise RuntimeError(f"File too large for Phase 2.0 (bytes={len(body)} max={max_bytes})")
    return body


def _extract_text(payload: bytes, *, content_type: str) -> str:
    ct = (content_type or "").lower().strip()
    if ct.startswith("text/") or ct in ("application/json",):
        return payload.decode("utf-8", errors="ignore")
    raise RuntimeError(f"Unsupported content_type for Phase 2.0 ingestion: {content_type}")


def _chunk_text(text_str: str, *, chunk_chars: int, overlap: int) -> List[str]:
    t = (text_str or "").strip()
    if not t:
        return []
    # Normalize newlines a bit
    t = t.replace("\r\n", "\n").replace("\r", "\n")

    n = len(t)
    chunks: List[str] = []
    start = 0
    while start < n:
        end = min(n, start + chunk_chars)
        chunk = t[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(0, end - overlap)
    return chunks


def _claim_next_job(db: Session) -> Optional[KbIngestJob]:
    # Postgres only: SKIP LOCKED prevents multiple workers fighting
    job = (
        db.query(KbIngestJob)
        .filter(KbIngestJob.status == "queued")
        .order_by(KbIngestJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if not job:
        return None

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _fail_job(db: Session, job: KbIngestJob, err: str) -> None:
    job.status = "failed"
    job.error = (err or "unknown error")[:4000]
    job.finished_at = datetime.now(timezone.utc)
    db.add(job)
    db.commit()


def _finish_job(db: Session, job: KbIngestJob) -> None:
    job.status = "ready"
    job.error = None
    job.finished_at = datetime.now(timezone.utc)
    db.add(job)
    db.commit()


def _store_chunks(db: Session, *, tenant_id: UUID, file_id: UUID, kind: str, chunks: List[str]) -> None:
    # Re-ingest behavior: wipe existing chunks for this file+tenant
    db.query(KbChunk).filter(KbChunk.tenant_id == tenant_id, KbChunk.file_id == file_id).delete()
    db.commit()

    objs: List[KbChunk] = []
    for i, ch in enumerate(chunks):
        objs.append(
            KbChunk(
                tenant_id=tenant_id,
                file_id=file_id,
                kind=(kind or "knowledge"),
                chunk_index=i,
                text=ch,
                created_at=datetime.now(timezone.utc),
            )
        )
    if objs:
        db.bulk_save_objects(objs)
        db.commit()


def main() -> None:
    # Basic logging for worker process
    level = (os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO))

    poll_s = float(_env_int("KB_INGEST_POLL_SECONDS", 2))
    chunk_chars = _env_int("KB_CHUNK_CHARS", 1000)
    overlap = _env_int("KB_CHUNK_OVERLAP", 200)
    max_bytes = _env_int("KB_INGEST_MAX_BYTES", 5_000_000)
    once = _truthy(os.getenv("KB_WORKER_ONCE"))

    logger.info(
        "KB_WORKER_START poll_s=%s chunk_chars=%s overlap=%s max_bytes=%s once=%s",
        poll_s,
        chunk_chars,
        overlap,
        max_bytes,
        once,
    )

    s3 = _s3_client()

    while True:
        db = SessionLocal()
        try:
            job = _claim_next_job(db)
            if not job:
                db.close()
                if once:
                    logger.info("KB_WORKER_EXIT no_job")
                    return
                time.sleep(poll_s)
                continue

            logger.info("KB_INGEST_JOB_START job_id=%s tenant_id=%s file_id=%s", job.id, job.tenant_id, job.file_id)

            try:
                file_row = _get_kb_file_row(db, tenant_id=job.tenant_id, file_id=job.file_id)
                if str(file_row.get("tenant_id")) != str(job.tenant_id):
                    raise RuntimeError("Tenant mismatch between job and file row")

                bucket = str(file_row.get("storage_bucket") or "").strip()
                key = str(file_row.get("storage_key") or "").strip()
                if not bucket or not key:
                    raise RuntimeError("KB file missing storage_bucket/storage_key")

                ct = str(file_row.get("content_type") or "").strip()
                kind = str(file_row.get("kind") or "knowledge").strip()

                payload = _download_object(s3, bucket=bucket, key=key, max_bytes=max_bytes)
                text_str = _extract_text(payload, content_type=ct)
                chunks = _chunk_text(text_str, chunk_chars=chunk_chars, overlap=overlap)

                _store_chunks(db, tenant_id=job.tenant_id, file_id=job.file_id, kind=kind, chunks=chunks)
                _finish_job(db, job)

                logger.info(
                    "KB_INGEST_JOB_READY job_id=%s chunks=%s bytes=%s filename=%s",
                    job.id,
                    len(chunks),
                    len(payload),
                    file_row.get("filename"),
                )

            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                logger.exception("KB_INGEST_JOB_FAIL job_id=%s err=%s", job.id, err)
                _fail_job(db, job, err)

            finally:
                db.close()

            if once:
                return

        except Exception as e:
            logger.exception("KB_WORKER_LOOP_ERR %s", e)
            try:
                db.close()
            except Exception:
                pass
            if once:
                return
            time.sleep(poll_s)


if __name__ == "__main__":
    main()
