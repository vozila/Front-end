"""
kb_ingest_worker.py — Vozlia KB ingestion background worker (Control Plane)

What it does
- Polls kb_ingest_jobs for status='queued'
- Claims the next job with FOR UPDATE SKIP LOCKED (safe with >1 worker)
- Downloads the KB file from S3/R2 using storage_bucket + storage_key from kb_files
- Extracts text (txt/markdown/pdf/docx/html best-effort)
- Chunks the text and writes rows to kb_chunks
- Marks the job ready/failed with error info

How to run (Render Background Worker recommended)
- Start command:  python kb_ingest_worker.py
- One-shot (debug): python kb_ingest_worker.py --once
- Drain queue then exit: python kb_ingest_worker.py --drain

Required env vars (same as your Control Plane web service)
- DATABASE_URL
- KB_S3_ACCESS_KEY_ID
- KB_S3_SECRET_ACCESS_KEY
- KB_S3_ENDPOINT_URL   (required for Cloudflare R2; optional for AWS S3)
- KB_S3_REGION         (use "auto" for R2)

Optional worker tuning env vars
- KB_WORKER_POLL_S=2
- KB_CHUNK_CHARS=1200
- KB_CHUNK_OVERLAP_CHARS=150
- KB_CHUNK_MIN_CHARS=200
- KB_WORKER_LOG_LEVEL=INFO
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# boto3 is required for S3/R2 downloads
import boto3
from botocore.config import Config as BotoConfig


# ----------------------------
# Logging
# ----------------------------

LOG_LEVEL = os.getenv("KB_WORKER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kb_ingest_worker")


# ----------------------------
# Env helpers
# ----------------------------

def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        log.warning("%s=%r is not an int; using default=%s", name, v, default)
        return default


def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None else default


# ----------------------------
# Job + file records
# ----------------------------

@dataclass(frozen=True)
class JobRow:
    id: str
    tenant_id: str
    file_id: str


@dataclass(frozen=True)
class FileRow:
    id: str
    tenant_id: str
    kind: str
    filename: str
    content_type: str
    storage_bucket: str
    storage_key: str


# ----------------------------
# DB queries (Postgres)
# ----------------------------

CLAIM_NEXT_JOB_SQL = text(
    """
WITH next_job AS (
  SELECT id
  FROM kb_ingest_jobs
  WHERE status = 'queued'
  ORDER BY created_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE kb_ingest_jobs j
SET status = 'running',
    started_at = NOW(),
    error = NULL
FROM next_job
WHERE j.id = next_job.id
RETURNING j.id::text AS id,
          j.tenant_id::text AS tenant_id,
          j.file_id::text AS file_id;
"""
)

GET_FILE_SQL = text(
    """
SELECT
  id::text AS id,
  tenant_id::text AS tenant_id,
  COALESCE(kind, 'knowledge') AS kind,
  filename,
  content_type,
  storage_bucket,
  storage_key
FROM kb_files
WHERE id::text = :file_id
  AND tenant_id::text = :tenant_id
LIMIT 1;
"""
)

DELETE_CHUNKS_SQL = text(
    """
DELETE FROM kb_chunks
WHERE tenant_id::text = :tenant_id
  AND file_id::text = :file_id;
"""
)

INSERT_CHUNK_SQL = text(
    """
INSERT INTO kb_chunks (id, tenant_id, file_id, kind, chunk_index, text, created_at)
VALUES (:id, :tenant_id, :file_id, :kind, :chunk_index, :text, NOW());
"""
)

MARK_READY_SQL = text(
    """
UPDATE kb_ingest_jobs
SET status = 'ready',
    finished_at = NOW(),
    error = NULL
WHERE id::text = :job_id;
"""
)

MARK_FAILED_SQL = text(
    """
UPDATE kb_ingest_jobs
SET status = 'failed',
    finished_at = NOW(),
    error = :error
WHERE id::text = :job_id;
"""
)



# ----------------------------
# Worker heartbeat (optional)
# ----------------------------

HEARTBEAT_CREATE_SQL = text(
    """
CREATE TABLE IF NOT EXISTS kb_worker_heartbeat (
  id TEXT PRIMARY KEY,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
)

HEARTBEAT_UPSERT_SQL = text(
    """
INSERT INTO kb_worker_heartbeat (id, updated_at)
VALUES (:id, NOW())
ON CONFLICT (id) DO UPDATE SET updated_at = EXCLUDED.updated_at;
"""
)


# ----------------------------
# S3/R2 client
# ----------------------------

def _build_s3_client() -> Any:
    endpoint_url = _env_str("KB_S3_ENDPOINT_URL", "").strip() or None
    region = _env_str("KB_S3_REGION", "auto") or "auto"
    access_key = _env_str("KB_S3_ACCESS_KEY_ID", "").strip()
    secret_key = _env_str("KB_S3_SECRET_ACCESS_KEY", "").strip()

    if not access_key or not secret_key:
        raise RuntimeError("Missing KB_S3_ACCESS_KEY_ID / KB_S3_SECRET_ACCESS_KEY")

    # R2 typically wants path-style addressing. AWS S3 usually works either way.
    force_path_style = _env_str("KB_S3_FORCE_PATH_STYLE", "1") == "1"
    addressing_style = "path" if force_path_style else "virtual"

    cfg = BotoConfig(signature_version="s3v4", s3={"addressing_style": addressing_style})

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=cfg,
    )


def _download_bytes(s3: Any, bucket: str, key: str) -> bytes:
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    if not isinstance(body, (bytes, bytearray)):
        body = bytes(body)
    return body


# ----------------------------
# Text extraction
# ----------------------------

def _guess_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    return k if k in ("knowledge", "policy") else "knowledge"


def _normalize_whitespace(s: str) -> str:
    # Keep newlines, collapse crazy spacing
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing spaces on each line
    s = "\n".join([ln.rstrip() for ln in s.split("\n")])
    # Collapse 3+ blank lines to 2 blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _extract_text(content_type: str, filename: str, data: bytes) -> str:
    ct = (content_type or "").lower().strip()
    fn = (filename or "").lower().strip()

    # Plain text / markdown
    if ct.startswith("text/") or fn.endswith((".txt", ".md", ".markdown")):
        return _normalize_whitespace(data.decode("utf-8", errors="replace"))

    # PDF
    if ct == "application/pdf" or fn.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "PDF ingestion requires 'pypdf'. Add it to requirements.txt and redeploy."
            ) from e

        reader = PdfReader(io.BytesIO(data))
        parts: List[str] = []
        for page in reader.pages:
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            if txt:
                parts.append(txt)
        return _normalize_whitespace("\n\n".join(parts))

    # DOCX
    if (
        ct in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)
        or fn.endswith(".docx")
    ):
        try:
            import docx  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "DOCX ingestion requires 'python-docx'. Add it to requirements.txt and redeploy."
            ) from e

        doc = docx.Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        return _normalize_whitespace("\n".join(parts))

    # HTML
    if ct in ("text/html", "application/xhtml+xml") or fn.endswith((".html", ".htm")):
        try:
            from bs4 import BeautifulSoup  # type: ignore
            soup = BeautifulSoup(data, "html.parser")
            return _normalize_whitespace(soup.get_text("\n"))
        except Exception:
            # Fallback: very rough tag strip
            s = data.decode("utf-8", errors="replace")
            s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
            s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
            s = re.sub(r"<[^>]+>", " ", s)
            return _normalize_whitespace(s)

    raise RuntimeError(f"Unsupported file type for ingestion: content_type={content_type!r}, filename={filename!r}")


# ----------------------------
# Chunking
# ----------------------------

def _chunk_text(text_in: str, max_chars: int, overlap: int, min_chars: int) -> List[str]:
    text_in = text_in.strip()
    if not text_in:
        return []

    # Prefer paragraph boundaries, then fall back to char slicing.
    paras = [p.strip() for p in re.split(r"\n\s*\n", text_in) if p.strip()]
    chunks: List[str] = []
    buf: List[str] = []
    buf_len = 0

    def flush_buf():
        nonlocal buf, buf_len
        if not buf:
            return
        chunk = "\n\n".join(buf).strip()
        if len(chunk) >= min_chars:
            chunks.append(chunk)
        buf = []
        buf_len = 0

    for p in paras:
        # If paragraph itself is huge, slice it
        if len(p) > max_chars:
            flush_buf()
            start = 0
            while start < len(p):
                end = min(len(p), start + max_chars)
                sub = p[start:end].strip()
                if len(sub) >= min_chars:
                    chunks.append(sub)
                if end >= len(p):
                    break
                start = max(0, end - overlap)
            continue

        # If adding would exceed max, flush
        if buf_len + len(p) + 2 > max_chars:
            flush_buf()

        buf.append(p)
        buf_len += len(p) + 2

    flush_buf()

    # Apply overlap at chunk boundaries (soft overlap)
    if overlap <= 0 or len(chunks) <= 1:
        return chunks

    overlapped: List[str] = []
    prev_tail = ""
    for i, ch in enumerate(chunks):
        if i == 0:
            overlapped.append(ch)
        else:
            tail = prev_tail[-overlap:] if prev_tail else ""
            merged = (tail + "\n" + ch).strip() if tail else ch
            overlapped.append(merged)
        prev_tail = ch
    return overlapped


# ----------------------------
# Worker core
# ----------------------------

def _make_engine() -> Engine:
    db_url = _env_str("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL is required")
    return create_engine(db_url, pool_pre_ping=True, future=True)



def _ensure_heartbeat_table(engine: Engine) -> None:
    """Create heartbeat table if missing (safe to call repeatedly)."""
    try:
        with engine.begin() as conn:
            conn.execute(HEARTBEAT_CREATE_SQL)
    except Exception as e:
        log.warning("heartbeat table create failed: %s", e)


def _heartbeat(engine: Engine, worker_id: str) -> None:
    """Update worker heartbeat timestamp (best-effort)."""
    try:
        with engine.begin() as conn:
            conn.execute(HEARTBEAT_UPSERT_SQL, {"id": worker_id})
    except Exception as e:
        log.warning("heartbeat upsert failed: %s", e)


def _resolve_worker_id() -> str:
    return (
        _env_str("KB_WORKER_ID", "").strip()
        or _env_str("RENDER_INSTANCE_ID", "").strip()
        or _env_str("HOSTNAME", "").strip()
        or f"kb-worker-{os.getpid()}"
    )


def _claim_next_job(engine: Engine) -> Optional[JobRow]:
    with engine.begin() as conn:
        row = conn.execute(CLAIM_NEXT_JOB_SQL).mappings().first()
        if not row:
            return None
        return JobRow(id=row["id"], tenant_id=row["tenant_id"], file_id=row["file_id"])


def _fetch_file(engine: Engine, tenant_id: str, file_id: str) -> Optional[FileRow]:
    with engine.begin() as conn:
        row = conn.execute(GET_FILE_SQL, {"tenant_id": tenant_id, "file_id": file_id}).mappings().first()
        if not row:
            return None
        return FileRow(
            id=row["id"],
            tenant_id=row["tenant_id"],
            kind=row["kind"],
            filename=row["filename"],
            content_type=row["content_type"],
            storage_bucket=row["storage_bucket"],
            storage_key=row["storage_key"],
        )


def _mark_failed(engine: Engine, job_id: str, error: str) -> None:
    # Truncate error to keep DB row reasonable
    error = (error or "").strip()
    if len(error) > 4000:
        error = error[:4000] + "…"
    with engine.begin() as conn:
        conn.execute(MARK_FAILED_SQL, {"job_id": job_id, "error": error})


def _mark_ready(engine: Engine, job_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(MARK_READY_SQL, {"job_id": job_id})


def _write_chunks(engine: Engine, tenant_id: str, file_id: str, kind: str, chunks: List[str]) -> None:
    tenant_uuid = UUID(tenant_id)

    with engine.begin() as conn:
        # Always replace chunks for this file (idempotent for re-ingest)
        conn.execute(DELETE_CHUNKS_SQL, {"tenant_id": tenant_id, "file_id": file_id})

        params: List[Dict[str, Any]] = []
        for i, ch in enumerate(chunks):
            params.append(
                {
                    "id": uuid4(),
                    "tenant_id": tenant_uuid,
                    "file_id": file_id,
                    "kind": kind,
                    "chunk_index": i,
                    "text": ch,
                }
            )
        if params:
            conn.execute(INSERT_CHUNK_SQL, params)


def _process_job(engine: Engine, s3: Any, job: JobRow) -> None:
    max_chars = _env_int("KB_CHUNK_CHARS", 1200)
    overlap = _env_int("KB_CHUNK_OVERLAP_CHARS", 150)
    min_chars = _env_int("KB_CHUNK_MIN_CHARS", 200)

    file_row = _fetch_file(engine, tenant_id=job.tenant_id, file_id=job.file_id)
    if not file_row:
        raise RuntimeError("kb_files row not found for tenant/file (file deleted or tenant mismatch)")

    kind = _guess_kind(file_row.kind)

    log.info("job=%s tenant=%s file=%s downloading %s", job.id, job.tenant_id, job.file_id, file_row.filename)
    data = _download_bytes(s3, bucket=file_row.storage_bucket, key=file_row.storage_key)

    text_out = _extract_text(file_row.content_type, file_row.filename, data)
    chunks = _chunk_text(text_out, max_chars=max_chars, overlap=overlap, min_chars=min_chars)

    log.info("job=%s extracted_chars=%s chunks=%s", job.id, len(text_out), len(chunks))
    _write_chunks(engine, tenant_id=job.tenant_id, file_id=job.file_id, kind=kind, chunks=chunks)


def main() -> None:
    once = "--once" in sys.argv
    drain = "--drain" in sys.argv
    poll_s = _env_int("KB_WORKER_POLL_S", 2)

    engine = _make_engine()
    s3 = _build_s3_client()

    worker_id = _resolve_worker_id()
    heartbeat_every_s = _env_int("KB_WORKER_HEARTBEAT_EVERY_S", 20)

    _ensure_heartbeat_table(engine)
    _heartbeat(engine, worker_id)
    last_hb = time.monotonic()

    log.info("KB worker started (poll=%ss, once=%s, drain=%s)", poll_s, once, drain)

    processed_any = False

    while True:
        # Heartbeat (so /admin/kb/health can detect worker liveness)
        now = time.monotonic()
        if heartbeat_every_s > 0 and (now - last_hb) >= heartbeat_every_s:
            _heartbeat(engine, worker_id)
            last_hb = now

        job = _claim_next_job(engine)
        if not job:
            if once:
                log.info("no queued jobs; exiting (--once)")
                return
            if drain:
                log.info("queue drained; exiting (--drain)")
                return
            time.sleep(poll_s)
            continue

        processed_any = True

        try:
            _process_job(engine, s3, job)
            _mark_ready(engine, job.id)
            log.info("job=%s READY", job.id)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log.exception("job=%s FAILED: %s", job.id, err)
            _mark_failed(engine, job.id, err)

        if once:
            return


if __name__ == "__main__":
    main()
