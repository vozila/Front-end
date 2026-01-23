# Control Plane: WebSearch + Notify Proxy (MVP)

This patch adds Control Plane proxy routes so the WebUI never talks to the backend directly.

## New required env vars (Render -> vozlia-control service)
- `VOZLIA_BACKEND_BASE_URL` = your backend base URL (example: https://vozlia-backend.onrender.com)
- `BACKEND_ADMIN_API_KEY` (optional) = admin key for backend; if omitted, Control Plane will reuse `ADMIN_API_KEY`

## Proxied routes
All require: `X-Vozlia-Admin-Key: <ADMIN_API_KEY>`

- `POST /admin/websearch/search`  -> backend `POST /admin/websearch/search`
- `GET /admin/websearch/skills`   -> backend `GET /admin/websearch/skills`
- `POST /admin/websearch/skills`  -> backend `POST /admin/websearch/skills`
- `DELETE /admin/websearch/skills/{skill_id}` -> backend delete
- `GET /admin/websearch/schedules` -> backend list schedules
- `POST /admin/websearch/schedules` -> backend upsert daily schedule

Notify:
- `POST /notify/sms`
- `POST /notify/whatsapp`
- `POST /notify/email`
- `POST /notify/call`

