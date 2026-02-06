# Vozlia Control Plane Patch — Regression Diag + Wizard DBQuery Op + DB Skill Naming (2026-02-05)

## What this patch does
1) Adds **/admin/diag/regression** (GET) for quick regression detection.
   - Calls key backend proxy endpoints used by WebUI:
     - /admin/websearch/skills
     - /admin/websearch/schedules
     - /admin/dbquery/skills
     - /admin/dbquery/schedules
     - /admin/dbquery/entities
   - Also checks Render API list-services (for Render Logs panel)

2) Updates **Configuration Wizard** DBQuery schema to allow filter op `has_concept`.

3) Enforces DBQuery skill naming:
   - Any created DBQuery skill is auto-prefixed as `DB: <name>` (if not already).

## Apply
Copy the files from this zip into your control-plane repo root, preserving paths.

## Smoke tests
1) `GET /admin/diag/regression` (with admin key header) returns JSON with checks.
2) Wizard can create DB skills without "couldn’t produce a valid action plan" for DBQuery ops.
