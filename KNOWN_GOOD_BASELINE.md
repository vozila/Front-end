Next step is **Phase 2: ingestion + retrieval**, now that Phase 1 (upload/list/download/delete) is proven and the Admin UI is usable (tenant dropdown + collapsible Memory Bank). Your Control Plane logs already show the happy-path traffic working end‑to‑end for upload + download + delete. 

Here’s the **most stable, incremental path** forward.

---

## Step 0 — Freeze a “Known Good” checkpoint

Before we touch ingestion or runtime retrieval, tag the current state:

* **WebUI:** KB Files panel works, tenant auto-fills from Email Accounts, Memory Bank collapsible
* **Control Plane:** KB storage endpoints healthy
* **No changes** to voice flows

**Action**

* Create a git tag or release note like: `kb_phase1_file_mgmt_ui_ok`.

**Why**

* Ingestion is where bugs + performance issues tend to appear. We want clean rollback.

---

## Step 1 — Add Phase 2 DB schema (no runtime behavior change)

Add tables you already proposed:

* `kb_ingest_jobs`
* `kb_chunks` (with embeddings)
* Ensure **tenant_id is indexed everywhere**

### If using Postgres for vectors

Use **pgvector**:

* enable extension `vector`
* `kb_chunks.embedding vector(<dim>)` (dim depends on model; ex: OpenAI `text-embedding-3-small`)

**Test**

* migration applies cleanly
* basic insert/select works

**Rollback**

* revert migration

---

## Step 2 — Implement ingestion as an async job (Control Plane owns it)

Hard rule: **ingestion must not happen in request/response**.

### Minimal ingestion architecture (Render-friendly)

* Control Plane endpoint creates a row in `kb_ingest_jobs` with `status='queued'`
* A **separate worker process** (recommended) polls for queued jobs and processes them

  * This prevents long uploads/processing from impacting the web dyno responsiveness

**New admin endpoints (Control Plane)**

* `POST /admin/kb/files/{id}/ingest` (enqueue)
* `GET  /admin/kb/files/{id}` already returns status; extend it to include:

  * `ingest_status`, `ingest_error`, `ingested_at`
* Optional:

  * `GET /admin/kb/ingest-jobs?tenant_id=...`

**Test**

* Upload file → enqueue ingest → job transitions: `queued → running → ready`
* Verify job failure stores `error` and status `failed`

**Rollback**

* disable worker (scale to 0) + hide ingest button in UI (feature flag)

---

## Step 3 — Text extraction + chunking (start narrow)

Don’t boil the ocean. Start with these file types first:

* `.txt`, `.md` (easy, reliable)
* then add `.pdf` (most common, trickier)
* then `.docx`, `.html`

**Chunking default**

* chunk by ~800–1200 characters
* overlap 100–200 chars
* store `chunk_index`, `text`, `file_id`, `tenant_id`

**Why this first**

* You can validate ingestion + retrieval without fighting PDF edge cases.

---

## Step 4 — Generate embeddings + store chunks

In the ingestion worker:

* For each chunk:

  * compute embedding
  * insert into `kb_chunks`

**Feature flag**

* `KB_EMBEDDINGS_ENABLED=1` (so we can turn off if cost spikes)

**Test**

* After ingestion: `SELECT count(*) FROM kb_chunks WHERE tenant_id=... AND file_id=...` is > 0

---

## Step 5 — Runtime retrieval in Flow B (behind a flag)

Now connect KB retrieval into the **router path** (`/assistant/route`), not any 20ms audio loop.

### Behavior

When handling a user turn:

1. Retrieve long-term memory (existing)
2. Retrieve KB chunks (new)
3. Combine into one “context bundle”
4. Send to LLM (Responses) as:

   * **policy** blocks (high priority instructions)
   * **knowledge** chunks (citations / context)

### Feature flag

* `KB_RETRIEVAL_ENABLED=0|1` default `0`

**Test**

* With flag OFF: no KB queries happen (confirm via logs)
* With flag ON:

  * query “what are your hours?” (and a KB doc contains hours)
  * response reflects KB content

**Rollback**

* set `KB_RETRIEVAL_ENABLED=0`

---

## Step 6 — Policy docs vs Knowledge docs (treat differently)

You already separated `kind`:

* `kind=policy` → should become “rules” injected as high‑priority instructions
* `kind=knowledge` → retrieval context

**Phase 2.0 simplest rule**

* If policy docs exist: include the **first N policy chunks** (or a pre-generated policy summary) every call
* Put a hard cap (ex: 2–4k chars) to avoid prompt bloat

Later (Phase 2.1):

* summarize policy docs into a single “Policy Capsule” during ingestion

---

## Step 7 — Extend “recall audit” to include KB sources

You already have “memory recall audit” concept. Next is to add:

* `kb_recall_audit` or extend existing audit rows to include:

  * `kb_file_id`
  * `kb_chunk_ids`
  * `kb_topk_scores`
  * model + timestamp

This makes debugging “why did it answer that?” much easier.

---

## Step 8 — Update Admin UI: “Ingest” + status column

In the **KB Files panel**:

* add “Ingest” button (enqueue)
* show status: `uploaded | ingesting | ready | failed`
* show error tooltip on failed

This completes Phase 2 operationally.

---

# What I recommend we do next (1 small step)

**Do Step 1 only next:** add the Phase 2 DB tables + migrations (no behavior changes).
Then we’ll do Step 2 (enqueue endpoint + worker) as the next iteration.

That’s the most stable progression and keeps rollbacks clean.

---

# AUTO-SUMMARY PACK

1. **Current Goal**
   Move from Phase 1 KB file management (upload/list/download/delete) to Phase 2 ingestion + retrieval, safely and incrementally.

2. **Refactor Step Completed (what was moved/extracted)**
   Already completed in prior steps: Memory Bank made collapsible; KB Files upload panel functional with tenant auto-fill from Email Accounts.

3. **What Changed (Code/Config/Infra)**
   Current system confirms Control Plane endpoints handling KB operations successfully (upload/download/delete). 
   No new changes applied in this message; this message defines next steps.

4. **Current System State (Flow A + Flow B)**

* **Flow A — Realtime (Legacy/Experimental):** Twilio → FastAPI WS (/twilio/stream) → OpenAI Realtime → Twilio (stable, untouched)
* **Flow B — Deepgram + ElevenLabs (Current MVP):** Twilio → Deepgram realtime STT → `/assistant/route` (FSM + OpenAI Responses text) → ElevenLabs TTS → Twilio (stable, keep hot paths clean)

5. **Known Issues**

* Phase 2 ingestion must be async to avoid web dyno blocking and to protect system stability.

6. **Evidence (≤5 log lines)**
   Control Plane logs show:

* `POST /kb/upload … 200 OK`
* `GET /admin/kb/files … 200 OK`
* `GET /kb/download … 200 OK`
* `DELETE /admin/kb/files … 200 OK` 

7. **Tests Performed / To Perform**
   Performed: Phase 1 upload/list/download/delete verification.
   To perform next (Phase 2):

* DB migration apply test
* enqueue ingest job test
* worker processes job → ready
* retrieval flag ON/OFF behavior in `/assistant/route`

8. **Next Actions (ordered)**

1) Freeze a known-good checkpoint/tag for Phase 1.
2) Add DB schema: `kb_ingest_jobs`, `kb_chunks` (+ pgvector if used).
3) Add ingest enqueue endpoint and a worker to process jobs async.
4) Implement extraction+chunking (start with txt/md).
5) Add embeddings + store in `kb_chunks`.
6) Integrate retrieval into Flow B router behind `KB_RETRIEVAL_ENABLED`.
7) Inject policy docs as instruction blocks; knowledge docs as retrieval context.
8) Extend recall audit to include KB chunk IDs and scores.
9) Update UI with ingest status + Ingest button.

9. **Safe Defaults + Rollback Point**

* Safe defaults: retrieval OFF by default (`KB_RETRIEVAL_ENABLED=0`), ingestion async, strict tenant_id filtering.
* Rollback: disable worker + revert migration + set retrieval flag OFF.

10. **Open Questions**

* Do you want ingestion worker inside Control Plane (separate Render service) or do you want Control Plane to call a private Backend ingestion endpoint?
* Vector storage preference: pgvector in existing Postgres vs dedicated vector DB.
