# Clarion GTM data layer — database lifecycle.
#
# Uses the Supabase CLI via npx (no global install needed; Node is present).
# On Windows, run via Git Bash / WSL, or copy the underlying commands into
# PowerShell — each target is a single command.
#
# One-time: npx supabase link --project-ref taebvsfawhhaujhrcbqe

SUPABASE := npx supabase
PY       := py

.PHONY: db.migrate db.seed db.reset db.test db.types

## Apply pending migrations in supabase/migrations/ to the linked project.
db.migrate:
	$(SUPABASE) db push

## Upsert the signal_types vocabulary (idempotent).
db.seed:
	$(PY) -m gtm.db.seed

## Recreate the LOCAL database from migrations + seed. Requires Docker
## (supabase start). Never points at the remote project.
db.reset:
	$(SUPABASE) db reset
	$(PY) -m gtm.db.seed

## Integration tests (skip cleanly if SUPABASE_URL / SERVICE_ROLE_KEY unset).
db.test:
	$(PY) -m pytest gtm/db/tests -v

## Skill tests: external APIs faked, DB live.
skills.test:
	$(PY) -m pytest gtm/skills/tests -v

## Everything.
test:
	$(PY) -m pytest gtm/db/tests gtm/skills/tests -v

## Generate TypeScript types for a future frontend.
db.types:
	$(SUPABASE) gen types typescript --linked > supabase/types/database.types.ts

# ── Orchestrator entry points (see docs/scheduling.md) ──────────────────────
.PHONY: run.morning run.evening run.monday run.wednesday run.friday run.webhooks status

run.morning:
	$(PY) -m gtm.orchestrator.daily_morning_sweep

run.evening:
	$(PY) -m gtm.orchestrator.daily_evening_close

run.monday:
	$(PY) -m gtm.orchestrator.weekly_monday_brief

run.wednesday:
	$(PY) -m gtm.orchestrator.weekly_wednesday_competitive

run.friday:
	$(PY) -m gtm.orchestrator.weekly_friday_retro

run.webhooks:
	$(PY) -m gtm.orchestrator.event_handlers

status:
	$(PY) -m gtm.cli.status
