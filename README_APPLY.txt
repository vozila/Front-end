KB Ingestion (Phase 2.0) — Routes + Worker (Control Plane)

This zip intentionally DOES NOT overwrite your existing control_main.py,
because your deployed repo likely contains additional KB storage routes.

Files included:
- kb_ingest.py         -> defines DB models (kb_ingest_jobs, kb_chunks) and registers 3 admin endpoints:
    POST /admin/kb/files/{file_id}/ingest
    GET  /admin/kb/ingest-jobs
    GET  /admin/kb/files/{file_id}/ingest-status

- kb_ingest_worker.py  -> optional Render Background Worker for async ingestion (Phase 2.0 supports text/* only)

How to apply:
1) Copy kb_ingest.py and kb_ingest_worker.py into your Control Plane repo root (same folder as control_main.py).

2) In control_main.py:
   a) Add import near other imports:
        from kb_ingest import register_kb_ingest_routes

   b) After your app is created AND after require_admin_key and get_db exist,
      register routes (ideally near where other routers are registered):
        register_kb_ingest_routes(app, require_admin=require_admin_key, get_db=get_db)

3) Redeploy the Control Plane service on Render.

4) Verify:
   curl -sS -X POST "https://vozlia-control.onrender.com/admin/kb/files/<FILE_ID>/ingest" \
     -H "X-Vozlia-Admin-Key: $ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"tenant_id":"<TENANT_ID>","force":false}'

If you still get {"detail":"Not Found"}, the Control Plane service is still running an older build
(or control_main.py entrypoint is not what you're deploying).
