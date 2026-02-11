"""VOZLIA FILE PURPOSE
Purpose: Deterministic grounding helpers for the Configuration Wizard (citations, KB-safe rewrites, and refusal rules).
Hot path: no (admin wizard only).
Public interfaces: rewrite_dbquery_spec_for_kb(), build_grounded_reply_and_citations().
Reads/Writes: none (pure functions).
Feature flags: WIZARD_GROUNDING_ENFORCE, WIZARD_REQUIRE_CITATIONS, WIZARD_WEBSEARCH_REQUIRE_SOURCES, WIZARD_WEBSEARCH_MIN_SOURCES.
Failure mode: always returns safe strings; never raises.
Last touched: 2026-02-09 (reduce hallucinations + make wizard answers auditable)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core.logging import env_flag

# -------------------------
# Flags (safe-by-default)
# -------------------------

WIZARD_GROUNDING_ENFORCE = env_flag("WIZARD_GROUNDING_ENFORCE", "1", inherit_debug=False)
WIZARD_REQUIRE_CITATIONS = env_flag("WIZARD_REQUIRE_CITATIONS", "1", inherit_debug=False)

WIZARD_WEBSEARCH_REQUIRE_SOURCES = env_flag("WIZARD_WEBSEARCH_REQUIRE_SOURCES", "1", inherit_debug=False)
WIZARD_WEBSEARCH_MIN_SOURCES = int((os.getenv("WIZARD_WEBSEARCH_MIN_SOURCES") or "1").strip() or "1")

WIZARD_CITATIONS_MAX = int((os.getenv("WIZARD_CITATIONS_MAX") or "8").strip() or "8")

WIZARD_KBQUERY_REWRITE_ENABLED = env_flag("WIZARD_KBQUERY_REWRITE_ENABLED", "1", inherit_debug=False)


# -------------------------
# Tokenization helpers
# -------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-']{1,}")

_STOPWORDS = {
    # Generic
    "a", "an", "and", "are", "as", "at", "about", "be", "by", "can", "could",
    "did", "do", "does", "for", "from", "have", "how", "i", "in", "is", "it",
    "me", "my", "of", "on", "or", "our", "should", "so", "that", "the", "their",
    "this", "to", "us", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "you", "your",

    # Wizard / tooling noise
    "run", "dbquery", "entity", "filter", "filters", "return", "just", "only", "first",
    "rows", "row", "count", "list", "show", "tell", "please", "now",
}


def _tokens(s: str, *, max_tokens: int = 12) -> List[str]:
    raw = [m.group(0).lower() for m in _WORD_RE.finditer(s or "")]
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


def _best_keyword(message: str) -> Optional[str]:
    """Pick a single keyword for a conservative KB text search."""
    toks = _tokens(message, max_tokens=8)
    if not toks:
        return None
    # Prefer longer tokens (often more specific).
    toks_sorted = sorted(toks, key=lambda x: (-len(x), x))
    return toks_sorted[0]


_TIME_HINTS = (
    "today", "yesterday", "tomorrow",
    "this week", "last week",
    "this month", "last month",
    "this year", "last year",
    "in the last", "past", "since", "between", "from ", "to ",
)


def has_time_intent(message: str) -> bool:
    m = (message or "").lower()
    return any(h in m for h in _TIME_HINTS)


_INGEST_HINTS = (
    "upload", "uploaded", "ingest", "ingested", "import", "imported", "created_at", "when did you",
)


def wants_ingestion_time(message: str) -> bool:
    m = (message or "").lower()
    return any(h in m for h in _INGEST_HINTS)


# -------------------------
# DBQuery rewrite for KB
# -------------------------

_ALLOWED_KINDS = {
    # known/expected KB kinds
    "knowledge", "policy",
    # common menu-ish schemas (future-proof)
    "header", "item", "description",
}


def _safe_snippet(s: str, *, max_chars: int = 220) -> str:
    t = (s or "").replace("\n", " ").strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rstrip() + "…"


def _extract_text_terms_from_dbquery_spec(spec: Dict[str, Any]) -> List[str]:
    """Extract user-supplied text-match terms from a dbquery spec.

    We use this only for *display* (snippets), to ensure the shown snippet actually
    contains what we searched for. This is intentionally conservative.
    """
    out: List[str] = []
    if not isinstance(spec, dict):
        return out
    filters = spec.get("filters") or []
    if not isinstance(filters, list):
        return out
    for f in filters:
        if not isinstance(f, dict):
            continue
        if (f.get("field") or "").strip() != "text":
            continue
        op = (f.get("op") or "").strip()
        val = f.get("value")
        if not isinstance(val, str):
            continue
        v = val.strip()
        if not v:
            continue
        if op in ("icontains", "contains", "ilike", "like", "eq"):
            out.append(v)
    dedup: List[str] = []
    seen: set[str] = set()
    for t in out:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        dedup.append(t)
    return dedup


_NONPRINTABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _snippet_around_terms(s: str, terms: List[str], *, max_chars: int = 220) -> str:
    """Return a short snippet anchored around the first occurrence of any term.

    If no term matches, falls back to the head snippet.
    """
    t = (s or "").replace("\n", " ").strip()
    t = _NONPRINTABLE_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    if not terms:
        return _safe_snippet(t, max_chars=max_chars)

    lower = t.lower()
    best_idx: int | None = None
    for term in terms:
        if not isinstance(term, str):
            continue
        term_l = term.strip().lower()
        if not term_l:
            continue
        i = lower.find(term_l)
        if i >= 0 and (best_idx is None or i < best_idx):
            best_idx = i

    if best_idx is None:
        return _safe_snippet(t, max_chars=max_chars)

    ctx_before = 70
    start = max(0, best_idx - ctx_before)
    end = min(len(t), start + max_chars)

    if end - start < max_chars and start > 0:
        start = max(0, end - max_chars)

    snippet = t[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(t):
        snippet = snippet.rstrip() + "…"
    return snippet

def rewrite_dbquery_spec_for_kb(spec: Dict[str, Any], message: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Rewrite obviously-wrong KB dbquery specs into a safer text search.

    Why:
      - KB content lives in kb_chunks.text. Using kb_chunks.kind="steak" is almost
        certainly a planner mistake (kind is a schema tag, not a content label).
      - Timeframe presets like "today" make KB retrieval return 0 rows when KB was
        ingested earlier.

    Returns:
      (new_spec, meta)
    """
    meta: Dict[str, Any] = {"changed": False, "notes": []}
    if not WIZARD_KBQUERY_REWRITE_ENABLED:
        return spec, meta

    entity = (spec.get("entity") or "").strip()
    if entity not in ("kb_chunks", "kb_files"):
        return spec, meta

    new_spec = dict(spec)

    # Remove timeframe unless the user is explicitly asking about ingestion time.
    if new_spec.get("timeframe") and not wants_ingestion_time(message):
        new_spec["timeframe"] = None
        meta["changed"] = True
        meta["notes"].append("removed_timeframe")

    # Ensure select includes enough to cite.
    sel = new_spec.get("select")
    if not isinstance(sel, list) or not sel:
        new_spec["select"] = ["id", "file_id", "chunk_index", "kind", "text", "created_at"]
        meta["changed"] = True
        meta["notes"].append("set_select_defaults")

    # Normalize filters.
    filters = new_spec.get("filters") or []
    if not isinstance(filters, list):
        filters = []
    rewritten: List[Dict[str, Any]] = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        field = (f.get("field") or "").strip()
        op = (f.get("op") or "").strip()
        val = f.get("value")
        # Normalize text searches to case-insensitive containment (planner often uses eq/contains).
        if field == "text" and isinstance(val, str) and op in ("eq", "contains", "ilike"):
            rewritten.append({"field": "text", "op": "icontains", "value": val})
            meta["changed"] = True
            meta["notes"].append("normalize_text_op")
            continue
        # Rewrite kind='steak' -> text icontains 'steak' unless kind value looks like a schema kind.
        if field == "kind" and op in ("eq", "in"):
            # value can be str or list[str]
            vals: List[str] = []
            if isinstance(val, str):
                vals = [val.strip().lower()]
            elif isinstance(val, list):
                vals = [str(x).strip().lower() for x in val if str(x).strip()]
            if vals and all(v not in _ALLOWED_KINDS for v in vals):
                # Use the first non-empty as the search term.
                term = vals[0]
                rewritten.append({"field": "text", "op": "icontains", "value": term})
                meta["changed"] = True
                meta["notes"].append(f"rewrite_kind_to_text:{term}")
                continue

        rewritten.append(f)

    if not rewritten:
        kw = _best_keyword(message)
        if kw:
            rewritten = [{"field": "text", "op": "icontains", "value": kw}]
            meta["changed"] = True
            meta["notes"].append(f"added_text_filter:{kw}")

    new_spec["filters"] = rewritten

    # Keep KB queries bounded.
    lim = new_spec.get("limit")
    try:
        lim_n = int(lim) if lim is not None else None
    except Exception:
        lim_n = None
    if not lim_n or lim_n <= 0:
        new_spec["limit"] = 10
        meta["changed"] = True
        meta["notes"].append("set_limit_default")
    elif lim_n > 50:
        new_spec["limit"] = 50
        meta["changed"] = True
        meta["notes"].append("clamp_limit_50")

    return new_spec, meta


# -------------------------
# Citations + grounded reply
# -------------------------

def _render_sources_md(citations: List[Dict[str, Any]]) -> str:
    if not citations:
        return "\n\n**Sources:** (none)\n"
    lines = ["\n\n**Sources:**"]
    for i, c in enumerate(citations, start=1):
        ctype = (c.get("type") or "source").strip()
        if ctype == "kb_chunk":
            cid = c.get("id") or "?"
            file_id = c.get("file_id") or "?"
            chunk_index = c.get("chunk_index")
            snip = c.get("snippet") or ""
            where = f"file={file_id}"
            if chunk_index is not None:
                where += f", chunk={chunk_index}"
            lines.append(f"{i}. KB {cid} ({where}) — {snip}")
        elif ctype == "web":
            title = c.get("title") or "Source"
            url = c.get("url") or ""
            snip = c.get("snippet") or ""
            lines.append(f"{i}. {title} — {url}\n   {snip}")
        elif ctype == "tool":
            tool = c.get("tool") or "tool"
            detail = c.get("detail") or ""
            lines.append(f"{i}. {tool}: {detail}")
        else:
            lines.append(f"{i}. {ctype}: {c}")
    return "\n".join(lines) + "\n"


def _citations_from_dbquery_kb_rows(rows: List[Dict[str, Any]], match_terms: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    terms = [t for t in (match_terms or []) if isinstance(t, str) and t.strip()]
    out: List[Dict[str, Any]] = []
    for r in rows[: WIZARD_CITATIONS_MAX]:
        if not isinstance(r, dict):
            continue
        txt = str(r.get("text") or "")
        out.append(
            {
                "type": "kb_chunk",
                "id": r.get("id"),
                "file_id": r.get("file_id"),
                "chunk_index": r.get("chunk_index"),
                "snippet": _snippet_around_terms(txt, terms, max_chars=220),
            }
        )
    return out

def _citations_from_web_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in sources[: WIZARD_CITATIONS_MAX]:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "type": "web",
                "title": s.get("title") or "",
                "url": s.get("url") or "",
                "snippet": _safe_snippet(str(s.get("snippet") or ""), max_chars=180),
            }
        )
    return out


def build_grounded_reply_and_citations(
    message: str,
    executed_actions: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Build a user-visible reply that is grounded in executed tool outputs.

    Policy:
      - Prefer deterministic tool outputs over planner prose.
      - If enforcement is enabled and there is no evidence, refuse (no guessing).
      - Always include a Sources section (even if empty) so the UI/user can audit.
    """

    grounding: Dict[str, Any] = {
        "mode": "none",
        "refused": False,
        "evidence_count": 0,
        "citations_count": 0,
        "tools": [a.get("type") for a in executed_actions if isinstance(a, dict)],
    }

    # Prefer KB dbquery results when present.
    chosen: Optional[Dict[str, Any]] = None
    for a in executed_actions:
        if isinstance(a, dict) and a.get("type") == "dbquery_run":
            chosen = a
            grounding["mode"] = "dbquery"
            break
    if chosen is None:
        for a in executed_actions:
            if isinstance(a, dict) and a.get("type") == "websearch_run":
                chosen = a
                grounding["mode"] = "websearch"
                break

    citations: List[Dict[str, Any]] = []

    # Always include a tool receipt citation if we ran anything.
    if executed_actions:
        citations.append(
            {
                "type": "tool",
                "tool": "wizard_actions",
                "detail": ", ".join([str(a.get("type")) for a in executed_actions if isinstance(a, dict)]),
            }
        )

    if not chosen:
        reply = "I didn't run any tools for this turn, so I can't answer reliably yet."
        grounding["refused"] = bool(WIZARD_GROUNDING_ENFORCE and WIZARD_REQUIRE_CITATIONS)
        grounding["evidence_count"] = 0
        grounding["citations_count"] = len(citations)
        reply += _render_sources_md(citations if WIZARD_REQUIRE_CITATIONS else [])
        return reply, (citations if WIZARD_REQUIRE_CITATIONS else []), grounding

    t = chosen.get("type")
    result = chosen.get("result") or {}

    if t == "websearch_run":
        sources = result.get("sources") or []
        if not isinstance(sources, list):
            sources = []
        answer = (result.get("answer") or "").strip()

        grounding["evidence_count"] = len(sources)

        # Enforce: no sources -> no answer
        if WIZARD_GROUNDING_ENFORCE and WIZARD_WEBSEARCH_REQUIRE_SOURCES and len(sources) < max(1, WIZARD_WEBSEARCH_MIN_SOURCES):
            grounding["refused"] = True
            reply = "I couldn't find reliable web sources for that request, so I’m not going to guess."
            citations.extend(_citations_from_web_sources(sources))
            grounding["citations_count"] = len(citations)
            reply += _render_sources_md(citations if WIZARD_REQUIRE_CITATIONS else [])
            return reply, (citations if WIZARD_REQUIRE_CITATIONS else []), grounding

        # Normal: return answer + citations
        if not answer:
            answer = "I ran a web search but didn't get a usable answer text."
        citations.extend(_citations_from_web_sources(sources))
        grounding["citations_count"] = len(citations)
        reply = answer + _render_sources_md(citations if WIZARD_REQUIRE_CITATIONS else [])
        return reply, (citations if WIZARD_REQUIRE_CITATIONS else []), grounding

    if t == "dbquery_run":
        entity = (result.get("entity") or chosen.get("spec", {}).get("entity") or "").strip()
        rows = result.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        grounding["evidence_count"] = len(rows)

        # KB-specific presentation
        if entity == "kb_chunks":
            match_terms = _extract_text_terms_from_dbquery_spec(chosen.get('spec') or {})
            citations.extend(_citations_from_dbquery_kb_rows(rows, match_terms))
            grounding["citations_count"] = len(citations)

            if not rows:
                grounding["refused"] = bool(WIZARD_GROUNDING_ENFORCE and WIZARD_REQUIRE_CITATIONS)
                reply = "I didn't find any matching records in kb_chunks."
                reply += _render_sources_md(citations if WIZARD_REQUIRE_CITATIONS else [])
                return reply, (citations if WIZARD_REQUIRE_CITATIONS else []), grounding

            # Show snippets deterministically (no guessing about 'items')
            lines = [f"I found {len(rows)} matching KB chunk(s). Here are up to {min(len(rows), 10)} snippet(s):"]
            for i, r in enumerate(rows[:10], start=1):
                if not isinstance(r, dict):
                    continue
                snip = _snippet_around_terms(str(r.get("text") or ""), match_terms, max_chars=220)
                cid = r.get("id") or "?"
                lines.append(f"{i}. ({cid}) {snip}")
            reply = "\n".join(lines) + _render_sources_md(citations if WIZARD_REQUIRE_CITATIONS else [])
            return reply, (citations if WIZARD_REQUIRE_CITATIONS else []), grounding

        # Generic DBQuery: just summarize row count deterministically.
        citations.append({"type": "tool", "tool": "dbquery_result", "detail": f"entity={entity} rows={len(rows)}"})
        grounding["citations_count"] = len(citations)

        if WIZARD_GROUNDING_ENFORCE and WIZARD_REQUIRE_CITATIONS and not rows:
            grounding["refused"] = True
            reply = f"I didn't find any matching records in {entity}." + _render_sources_md(citations)
            return reply, citations, grounding

        # Provide a compact preview of the first few rows.
        preview_lines = [f"Here are the latest results from {entity} (showing up to 10 rows):"]
        for r in rows[:10]:
            preview_lines.append(str(r)[:480])
        reply = "\n".join(preview_lines) + _render_sources_md(citations if WIZARD_REQUIRE_CITATIONS else [])
        return reply, (citations if WIZARD_REQUIRE_CITATIONS else []), grounding

    # Unknown tool type
    reply = "I ran a tool, but I don't know how to present its result safely yet."
    grounding["refused"] = bool(WIZARD_GROUNDING_ENFORCE and WIZARD_REQUIRE_CITATIONS)
    grounding["citations_count"] = len(citations)
    reply += _render_sources_md(citations if WIZARD_REQUIRE_CITATIONS else [])
    return reply, (citations if WIZARD_REQUIRE_CITATIONS else []), grounding