"""VOZLIA FILE PURPOSE
Purpose: FastAPI bootstrap + route registration for the backend web service.
Hot path: yes (process-level startup), but no per-frame audio work.
Public interfaces: create_app(), app.
Reads/Writes: DB schema init (Base.metadata.create_all), skills loader.
Feature flags: HTTP_REQUEST_LOG_ENABLED, HTTP_CAPTURE_BODY_ON_ERROR, DYNAMIC_SKILLS_AUTOSYNC.
Failure mode: never fails startup due to optional recovery paths (autosync is best-effort).
Last touched: 2026-02-03 (add request_id + optional debug request logging middleware)
"""

# main.py
from __future__ import annotations

import os
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request

from core.logging import logger  # IMPORTANT: initializes logging early
from core.request_context import set_request_id, reset_request_id

from db import Base, engine, SessionLocal
from skills.loader import load_skills_from_disk

from api.routers.health import router as health_router
from api.routers.twilio import router as twilio_router, mount_twilio_ws
from api.routers.assistant import router as assistant_router
from api.routers.gmail_api import router as gmail_api_router
from api.routers.user_settings import router as user_settings_router
from api.routers.kb import router as kb_router
from api.routers.notify import router as notify_router
from api.routers.websearch import router as websearch_router
from api.routers.dbquery import router as dbquery_router

# Admin troubleshooting routes
from api.routers.admin_settings import router as admin_settings_router
from api.routers.admin_dynamic_skills import router as admin_dynamic_skills_router


def _truthy_env(name: str, default: str = "0") -> bool:
    v = (os.getenv(name) or default).strip().lower()
    return v in ("1", "true", "yes", "on")


def create_app() -> FastAPI:
    app = FastAPI()

    # -------------------------
    # Request correlation + debug logging (safe-by-default)
    # -------------------------
    http_log_enabled = _truthy_env("HTTP_REQUEST_LOG_ENABLED", "0")
    http_log_prefix = (os.getenv("HTTP_REQUEST_LOG_PATH_PREFIX") or "/admin").strip() or "/admin"

    capture_body_on_error = _truthy_env("HTTP_CAPTURE_BODY_ON_ERROR", "0")
    capture_body_prefix = (os.getenv("HTTP_CAPTURE_BODY_PATH_PREFIX") or "/admin").strip() or "/admin"
    try:
        capture_body_max = int((os.getenv("HTTP_CAPTURE_BODY_MAX_BYTES") or "2048").strip() or "2048")
    except Exception:
        capture_body_max = 2048

    @app.middleware("http")
    async def _request_context_mw(request: Request, call_next):
        # Use upstream request id if present; otherwise mint a new one.
        rid = (
            (request.headers.get("x-vozlia-request-id") or "").strip()
            or (request.headers.get("x-request-id") or "").strip()
            or str(uuid4())
        )
        token = set_request_id(rid)
        t0 = perf_counter()

        # Optionally capture body (for admin debugging only).
        body_bytes: bytes | None = None
        if capture_body_on_error and request.url.path.startswith(capture_body_prefix):
            try:
                # Avoid giant uploads; rely on Content-Length if present.
                cl = request.headers.get("content-length")
                if cl is None or int(cl) <= capture_body_max:
                    body_bytes = await request.body()  # starlette caches; safe for downstream reads
            except Exception:
                body_bytes = None

        try:
            response = await call_next(request)
        except Exception:
            # Add context-rich log; keep request body small.
            if body_bytes:
                preview = body_bytes[:capture_body_max].decode("utf-8", errors="replace")
                logger.exception(
                    "HTTP_UNHANDLED_EXCEPTION method=%s path=%s qs=%s body_preview=%s",
                    request.method,
                    request.url.path,
                    request.url.query,
                    preview,
                )
            else:
                logger.exception(
                    "HTTP_UNHANDLED_EXCEPTION method=%s path=%s qs=%s",
                    request.method,
                    request.url.path,
                    request.url.query,
                )
            raise
        finally:
            dt_ms = (perf_counter() - t0) * 1000.0
            try:
                # Always attach a response header when we have a response object.
                # (If we raised above, we won't reach here with a response.)
                pass
            finally:
                # Only log requests when enabled and path matches prefix.
                if http_log_enabled and request.url.path.startswith(http_log_prefix):
                    try:
                        status_code = getattr(locals().get("response"), "status_code", None)
                        logger.info(
                            "HTTP_REQUEST method=%s path=%s status=%s ms=%.1f qs=%s",
                            request.method,
                            request.url.path,
                            status_code,
                            dt_ms,
                            request.url.query,
                        )
                    except Exception:
                        # Never break request handling due to logging.
                        pass
                reset_request_id(token)

        # Ensure request id is visible to callers.
        try:
            response.headers["X-Vozlia-Request-Id"] = rid
        except Exception:
            pass
        return response

    # -------------------------
    # Core routes
    # -------------------------
    app.include_router(health_router)
    app.include_router(twilio_router)
    mount_twilio_ws(app)

    # Skill / helper routes
    app.include_router(assistant_router)
    app.include_router(gmail_api_router)

    # Settings routes (restores /me/settings/* endpoints used by the stream)
    app.include_router(user_settings_router)

    # KB router (placeholder health route + future KB endpoints)
    app.include_router(kb_router)

    # Notifications + WebSearch admin routes (control plane should proxy these)
    app.include_router(notify_router)
    app.include_router(websearch_router)
    app.include_router(dbquery_router)

    # Admin settings + troubleshooting
    app.include_router(admin_settings_router)
    app.include_router(admin_dynamic_skills_router)

    @app.on_event("startup")
    def _startup() -> None:
        # Ensure DB schema exists and load YAML skills.
        Base.metadata.create_all(bind=engine)
        load_skills_from_disk()

        # Best-effort: repair dynamic-skill routing after rollbacks / drift.
        # Rollback: set DYNAMIC_SKILLS_AUTOSYNC=0 to disable.
        try:
            from services.dynamic_skill_autosync import dynamic_skills_autosync_enabled, autosync_dynamic_skills
            from services.user_service import get_or_create_primary_user

            if dynamic_skills_autosync_enabled():
                db = SessionLocal()
                try:
                    user = get_or_create_primary_user(db)
                    autosync_dynamic_skills(db, user, force=False)
                    db.commit()
                finally:
                    db.close()
        except Exception:
            # Never crash startup due to autosync; it is purely a recovery mechanism.
            logger.exception("DYNAMIC_SKILLS_AUTOSYNC_STARTUP_FAIL")

        logger.info("Database tables ensured and skills loaded.")

    @app.get("/")
    async def root():
        return {"ok": True}

    return app


app = create_app()
