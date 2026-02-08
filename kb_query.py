"""kb_query.py — Vozlia Control Plane: KB retrieval + (optional) grounded Q&A

Why this exists
- You already ingest KB files into Postgres (kb_chunks). This endpoint is the
  retrieval + answer layer so WebUI (and later the voice agent) can ask questions
  "like ChatGPT", but grounded in tenant-specific KB docs.

Endpoint
- POST /admin/kb/query   (admin-auth only)

Design notes (guardrails)
- tenant_id is REQUIRED and is used on every DB query (multi-tenant isolation)
- Retrieval is best-effort with safe fallbacks:
    1) Full-text search (FTS) over kb_chunks.text
    2) Token ILIKE search over kb_chunks.text (OR)
    3) Filename match (kb_files.filename) -> pull all chunks from matching file(s)
    4) Fallback to most recent chunks (so generic questions return something)
- Answer mode calls OpenAI only if we have context text; otherwise returns a
  helpful message (avoids hallucinations + saves cost)
- Knowledge vs Policy:
    - policy chunks (kind='policy') are treated as *instructions*
    - knowledge chunks (kind='knowledge') are treated as *facts only*
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.logging import env_flag

logger = logging.getLogger("kb_query")

KB_QUERY_DEBUG = env_flag("KB_QUERY_DEBUG", "0", inherit_debug=True)


Mode = Literal["retrieve", "answer"]


# ----------------------------
# Request/response models
# ----------------------------

class KBQueryRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1, max_length=2000)
    mode: Mode = "answer"
    limit: int = Field(8, ge=1, le=20)
    include_policy: bool = True


class KBSource(BaseModel):
    file_id: str
    filename: Optional[str] = None
    content_type: Optional[str] = None
    kind: str
    chunk_index: int
    snippet: str
    score: Optional[float] = None


class KBQueryResponse(BaseModel):
    ok: bool
    tenant_id: str
    mode: Mode
    retrieval_strategy: str
    answer: Optional[str]
    sources: List[KBSource]
    policy_chars: int
    context_chars: int
    model: Optional[str]
    latency_ms: float


# ----------------------------
# Helpers
# ----------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-']{1,}")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "about", "be", "by", "can", "could",
    "did", "do", "does", "for", "from", "have", "how", "i", "in", "is", "it",
    "me", "my", "of", "on", "or", "our", "should", "so", "that", "the", "their",
    "this", "to", "us", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "you", "your", "summarize", "summary",
}


def _tokens(query: str, max_tokens: int = 12) -> List[str]:
    raw = [m.group(0).lower() for m in _WORD_RE.finditer(query or "")]
    out: List[str] = []
    seen = set()
    for t in raw:
        if t in _STOPWORDS:
            continue
        if len(t) < 3:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_tokens:
            break
    return out


def _clean_snippet(s: str, max_chars: int = 320) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


# ----------------------------
# SQL
# IMPORTANT: your schema currently mixes tenant_id types:
#   - kb_files.tenant_id is varchar
#   - kb_chunks.tenant_id is uuid
# So we join with c.tenant_id::text.
# ----------------------------

FTS_SQL = text(
    """
SELECT
  c.file_id::text AS file_id,
  c.kind          AS kind,
  c.chunk_index   AS chunk_index,
  c.text          AS chunk_text,
  c.created_at    AS created_at,
  f.filename      AS filename,
  f.content_type  AS content_type,
  ts_rank_cd(to_tsvector('english', c.text), plainto_tsquery('english', :q)) AS score
FROM kb_chunks c
LEFT JOIN kb_files f
  ON f.id = c.file_id
 AND f.tenant_id = c.tenant_id::text
WHERE c.tenant_id::text = :tenant_id
  AND c.kind = 'knowledge'
  AND to_tsvector('english', c.text) @@ plainto_tsquery('english', :q)
ORDER BY score DESC, c.created_at DESC
LIMIT :limit;
"""
)

RECENT_SQL = text(
    """
SELECT
  c.file_id::text AS file_id,
  c.kind          AS kind,
  c.chunk_index   AS chunk_index,
  c.text          AS chunk_text,
  c.created_at    AS created_at,
  f.filename      AS filename,
  f.content_type  AS content_type,
  NULL::float8    AS score
FROM kb_chunks c
LEFT JOIN kb_files f
  ON f.id = c.file_id
 AND f.tenant_id = c.tenant_id::text
WHERE c.tenant_id::text = :tenant_id
  AND c.kind = 'knowledge'
ORDER BY c.created_at DESC
LIMIT :limit;
"""
)

POLICY_SQL = text(
    """
SELECT
  c.file_id::text AS file_id,
  c.chunk_index   AS chunk_index,
  c.text          AS chunk_text,
  c.created_at    AS created_at,
  f.filename      AS filename,
  f.content_type  AS content_type
FROM kb_chunks c
LEFT JOIN kb_files f
  ON f.id = c.file_id
 AND f.tenant_id = c.tenant_id::text
WHERE c.tenant_id::text = :tenant_id
  AND c.kind = 'policy'
ORDER BY c.created_at DESC, c.chunk_index ASC
LIMIT 50;
"""
)

FILENAME_MATCH_SQL = text(
    """
SELECT
  c.file_id::text AS file_id,
  c.kind          AS kind,
  c.chunk_index   AS chunk_index,
  c.text          AS chunk_text,
  c.created_at    AS created_at,
  f.filename      AS filename,
  f.content_type  AS content_type,
  NULL::float8    AS score
FROM kb_chunks c
JOIN kb_files f
  ON f.id = c.file_id
 AND f.tenant_id = c.tenant_id::text
WHERE c.tenant_id::text = :tenant_id
  AND c.kind = 'knowledge'
  AND ({filename_ors})
ORDER BY f.filename ASC, c.chunk_index ASC
LIMIT :limit;
"""
)


def _ilike_sql_for_tokens(tokens: List[str], field: str) -> Tuple[Any, Dict[str, Any]]:
    # field is either 'c.text' or 'f.filename'
    ors: List[str] = []
    params: Dict[str, Any] = {}
    for i, t in enumerate(tokens):
        k = f"t{i}"
        ors.append(f"{field} ILIKE :{k}")
        params[k] = f"%{t}%"
    return " OR ".join(ors), params


# ----------------------------
# Retrieval
# ----------------------------

def _fetch_policy_text(db: Session, tenant_id: str, max_chars: int) -> Tuple[str, int]:
    rows = db.execute(POLICY_SQL, {"tenant_id": tenant_id}).mappings().all()
    if not rows:
        return "", 0

    parts: List[str] = []
    used = 0
    for r in rows:
        chunk = (r.get("chunk_text") or "").strip()
        if not chunk:
            continue
        fname = (r.get("filename") or r.get("file_id") or "policy").strip()
        idx = int(r.get("chunk_index") or 0)
        block = f"[POLICY:{fname}#chunk{idx}]\n{chunk}\n"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 200:
                parts.append(block[:remaining])
                used += len(parts[-1])
            break
        parts.append(block)
        used += len(block)

    return "\n".join(parts).strip(), used


def _retrieve_knowledge(db: Session, tenant_id: str, query: str, limit: int) -> Tuple[str, List[Dict[str, Any]]]:
    q = (query or "").strip()
    toks = _tokens(q)

    # 1) Full-text search (best when query has meaningful tokens)
    if toks:
        rows = db.execute(FTS_SQL, {"tenant_id": tenant_id, "q": q, "limit": limit}).mappings().all()
        if rows:
            return "fts", [dict(r) for r in rows]

    # 2) Token OR match in chunk text (good fallback when FTS misses)
    if toks:
        ors, extra = _ilike_sql_for_tokens(toks, field="c.text")
        sql = text(
            f"""
SELECT
  c.file_id::text AS file_id,
  c.kind          AS kind,
  c.chunk_index   AS chunk_index,
  c.text          AS chunk_text,
  c.created_at    AS created_at,
  f.filename      AS filename,
  f.content_type  AS content_type,
  NULL::float8    AS score
FROM kb_chunks c
LEFT JOIN kb_files f
  ON f.id = c.file_id
 AND f.tenant_id = c.tenant_id::text
WHERE c.tenant_id::text = :tenant_id
  AND c.kind = 'knowledge'
  AND ({ors})
ORDER BY c.created_at DESC
LIMIT :limit;
"""
        )
        params = {"tenant_id": tenant_id, "limit": limit}
        params.update(extra)
        rows = db.execute(sql, params).mappings().all()
        if rows:
            return "ilike_chunk_text", [dict(r) for r in rows]

    # 3) Filename match (useful for "summarize settlement agreement" where the filename contains it)
    if toks:
        filename_ors, extra = _ilike_sql_for_tokens(toks, field="f.filename")
        sql = text(FILENAME_MATCH_SQL.text.format(filename_ors=filename_ors))
        params = {"tenant_id": tenant_id, "limit": limit}
        params.update(extra)
        rows = db.execute(sql, params).mappings().all()
        if rows:
            return "filename_match", [dict(r) for r in rows]

    # 4) Final fallback: most recent chunks for tenant
    rows = db.execute(RECENT_SQL, {"tenant_id": tenant_id, "limit": limit}).mappings().all()
    return "recent_fallback", [dict(r) for r in rows]


def _rows_to_sources(rows: List[Dict[str, Any]]) -> Tuple[List[KBSource], int]:
    sources: List[KBSource] = []
    total_chars = 0
    for r in rows:
        txt = (r.get("chunk_text") or "")
        total_chars += len(txt)
        sources.append(
            KBSource(
                file_id=str(r.get("file_id") or ""),
                filename=r.get("filename"),
                content_type=r.get("content_type"),
                kind=str(r.get("kind") or "knowledge"),
                chunk_index=int(r.get("chunk_index") or 0),
                snippet=_clean_snippet(txt),
                score=(float(r["score"]) if r.get("score") is not None else None),
            )
        )
    return sources, total_chars


def _build_context(rows: List[Dict[str, Any]], max_chars: int) -> Tuple[str, int]:
    blocks: List[str] = []
    used = 0
    for r in rows:
        fname = (r.get("filename") or r.get("file_id") or "file").strip()
        idx = int(r.get("chunk_index") or 0)
        chunk = (r.get("chunk_text") or "").strip()
        if not chunk:
            continue

        block = f"[{fname}#chunk{idx}]\n{chunk}\n"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 300:
                blocks.append(block[:remaining])
                used += len(blocks[-1])
            break
        blocks.append(block)
        used += len(block)

    return "\n".join(blocks).strip(), used


# ----------------------------
# OpenAI answer
# ----------------------------

def _get_openai_key() -> str:
    # Allow a Vozlia-specific alias so you aren't forced into one env name.
    return (
        os.getenv("OPENAI_API_KEY", "").strip()
        or os.getenv("VOZLIA_OPENAI_API_KEY", "").strip()
    )


def _answer_with_openai(query: str, policy_text: str, context_text: str) -> Tuple[str, str]:
    api_key = _get_openai_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="KB Q&A unavailable: OPENAI_API_KEY is not configured")

    try:
        from openai import OpenAI  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"KB Q&A unavailable: openai SDK not installed ({e})")

    model = os.getenv("KB_QA_MODEL", "gpt-4o-mini")
    max_tokens = int(os.getenv("KB_QA_MAX_OUTPUT_TOKENS", "450"))

    system_instructions = (
        "You are a Vozlia business assistant.\n"
        "Follow POLICY rules if provided.\n"
        "Use only the CONTEXT excerpts as your factual basis.\n"
        "If the answer is not present in CONTEXT, say you don't have enough information from the uploaded documents.\n"
        "Always cite sources inline using brackets exactly like: [filename#chunkN].\n"
        "If the question is legal/contract related, provide a neutral summary and add: 'This is not legal advice.'\n"
        "Do NOT invent facts.\n"
    )

    user_input = (
        f"USER QUESTION:\n{query.strip()}\n\n"
        f"POLICY (if any):\n{policy_text.strip() if policy_text else '(none)'}\n\n"
        f"CONTEXT (excerpts from uploaded KB):\n{context_text.strip() if context_text else '(none)'}\n"
    )

    try:
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            instructions=system_instructions,
            input=user_input,
            max_output_tokens=max_tokens,
        )
        answer = (getattr(resp, "output_text", None) or "").strip()
        if not answer:
            answer = "I couldn't generate an answer from the uploaded documents."
        return answer, model
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("OpenAI call failed")
        raise HTTPException(status_code=502, detail=f"KB Q&A failed calling model: {type(e).__name__}: {e}")


# ----------------------------
# FastAPI registration
# ----------------------------

def register_kb_query_routes(app, require_admin, get_db) -> None:
    router = APIRouter()

    @router.post("/admin/kb/query", response_model=KBQueryResponse)
    def kb_query(
        req: KBQueryRequest,
        db: Session = Depends(get_db),
        _admin_ok: Any = Depends(require_admin),
    ) -> KBQueryResponse:
        t0 = time.perf_counter()

        if KB_QUERY_DEBUG:
            try:
                q_prev = (req.query or '').replace('\n', ' ').strip()
                if len(q_prev) > 200:
                    q_prev = q_prev[:200] + '…'
                logger.info(
                    'KB_QUERY_IN tenant_id=%s mode=%s limit=%s include_policy=%s query=%r',
                    req.tenant_id,
                    req.mode,
                    req.limit,
                    bool(req.include_policy),
                    q_prev,
                )
            except Exception:
                pass

        tenant_id = (req.tenant_id or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")

        # Policy text (optional)
        policy_chars = 0
        policy_text = ""
        if req.include_policy:
            max_policy_chars = int(os.getenv("KB_QA_MAX_POLICY_CHARS", "6000"))
            policy_text, policy_chars = _fetch_policy_text(db, tenant_id=tenant_id, max_chars=max_policy_chars)

        # Retrieve knowledge
        strategy, rows = _retrieve_knowledge(db, tenant_id=tenant_id, query=req.query, limit=req.limit)
        if KB_QUERY_DEBUG:
            try:
                logger.info('KB_QUERY_RETRIEVAL strategy=%s rows=%s', strategy, len(rows or []))
            except Exception:
                pass
        sources, _raw_context_chars = _rows_to_sources(rows)

        # Build context for model
        max_context_chars = int(os.getenv("KB_QA_MAX_CONTEXT_CHARS", "12000"))
        context_text, used_context_chars = _build_context(rows, max_chars=max_context_chars)
        if KB_QUERY_DEBUG:
            try:
                logger.info('KB_QUERY_GROUNDING evidence=%s policy_chars=%s context_chars=%s', bool(context_text.strip()), policy_chars, used_context_chars)
            except Exception:
                pass

        answer: Optional[str] = None
        model: Optional[str] = None

        if req.mode == "answer":
            if not context_text.strip():
                # Avoid calling the model with no grounding context.
                answer = (
                    "I couldn’t find any relevant information in the uploaded knowledge base for that tenant. "
                    "Try rephrasing the question (include keywords like the document name), or upload/ingest the document that contains the answer."
                )
            else:
                answer, model = _answer_with_openai(req.query, policy_text=policy_text, context_text=context_text)

        latency_ms = (time.perf_counter() - t0) * 1000.0

        if KB_QUERY_DEBUG:
            try:
                ans_len = len(answer or '') if isinstance(answer, str) else 0
                logger.info('KB_QUERY_OUT ok=%s sources=%s answer_len=%s ms=%.1f', bool(answer), len(sources), ans_len, (time.perf_counter()-t0)*1000.0)
            except Exception:
                pass
        return KBQueryResponse(
            ok=True,
            tenant_id=tenant_id,
            mode=req.mode,
            retrieval_strategy=strategy,
            answer=answer,
            sources=sources,
            policy_chars=int(policy_chars),
            context_chars=int(used_context_chars),
            model=model,
            latency_ms=float(latency_ms),
        )

    app.include_router(router)
