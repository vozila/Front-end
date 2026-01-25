VOZLIA CONTROL PLANE PATCH — Wizard DBQuery Wiring (incremental)

What this patch fixes
- The portal chat "wizard" can now answer internal analytics questions using the backend DBQuery engine.
- It can also create DBQuery-backed skills (triggers + spec) from chat, instead of hallucinating or asking for a "data source".

Key new Wizard actions
- dbquery_run: run a one-off DB query (tenant-scoped) and return a spoken-style summary.
- dbquery_skill_create: persist a DBQuery skill in backend (also registers it into skills_config so Voice can trigger it).

Files included
- Front-end-main/services/config_wizard_service.py  (full replacement)

How to apply
1) In your Control Plane repo, replace:
   services/config_wizard_service.py
   with the version from this patch zip.

2) Redeploy the Control Plane service.

Backend requirement
- Backend must already have /admin/dbquery/run, /admin/dbquery/skills, /admin/dbquery/entities.
  (You already confirmed POST /admin/dbquery/skills works.)

Smoke tests (portal chat)
A) One-off (no pre-created skill)
   "how many calls did we receive this week?"
   Expected: returns a count summary (via dbquery_run), then offers to save as a Skill.

B) Explicit create
   "Create a skill called 'Callers Today' that tells me how many customers called today."
   Expected: creates a dbquery_* skill with triggers.

Voice test
- After creating the DBQuery skill, call Vozlia and say one of the triggers (e.g. "callers today").
  Expected: dynamic skill executes and reads the summary.

Rollback
- Revert services/config_wizard_service.py to the prior version (from the previous control-plane wizard patch).
