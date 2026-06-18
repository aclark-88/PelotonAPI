# GitHub Actions cloud scheduling — setup

Why: a laptop Task Scheduler job only runs when the laptop is awake at 6am.
Proven gap: Jun 14-17 2026 the sweep didn't run (laptop closed at dawn), and
the Jun 18 catch-up was killed mid-run. GitHub Actions runs in the cloud on a
UTC cron regardless of the laptop. Same pattern as the existing morning-brief
pipeline (per project memory: GH schedules only fire from `master`).

## Workflow

`.github/workflows/gtm-morning-sweep.yml` — runs the discovery→verify→score→
resolve→draft-queue chain daily at **10:00 UTC (06:00 EDT)**. Drafting is NOT
done here (orchestrator-is-the-LLM): the chain produces the queue + digest;
you draft in a Claude session. Drafts/signals persist in Supabase; the digest
is committed back to the repo and uploaded as a 30-day artifact.

## One-time setup

### 1. Add repository secrets
GitHub repo → Settings → Secrets and variables → Actions → New repository
secret. Add each (values are in `DEV/.env`):

| Secret | Required | Used by |
|---|---|---|
| `EDGAR_IDENTITY` | yes | Form D / 13F fetch |
| `SUPABASE_URL` | yes | the data store |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | the data store |
| `APOLLO_API_KEY` | yes | contact resolution |
| `TAVILY_API_KEY` | yes | spinout + verification web search |
| `HUBSPOT_ACCESS_TOKEN` | optional | champion-relocation check |
| `HEYREACH_API_KEY` | optional | not used by the sweep (dispatch is local/manual) |
| `SLACK_WEBHOOK_URL` | optional | digest + failure pings |

### 2. Merge to `master`
GitHub only runs scheduled workflows from the default branch. The cron is
dormant until `.github/workflows/gtm-morning-sweep.yml` lives on `master`.

### 3. First run — manual
Actions tab → "GTM Morning Sweep" → "Run workflow" (optionally set a lookback
to backfill missed days). Confirms secrets work before trusting the cron.

## Known cloud-vs-local differences (by design, all graceful)

- **No ADV roster CSV** (the 40MB SEC FOIA file is git-ignored). ADV
  enrichment + the IAPD verification bonus are skipped; verification still
  runs on web + fund-name evidence (which caught every real-estate / PE /
  mining reject so far). To restore full ADV in the cloud, add a step that
  downloads the monthly roster, or commit it via git-lfs.
- **Dispatch stays local/manual.** The cloud sweep only discovers and queues.
  Sending to HeyReach happens after your approval in a session — the
  Tier-4 human gate is unchanged.

## Retire the laptop tasks (optional)
Once the cloud cron is confirmed, remove the duplicate local schedules:
```powershell
Get-ScheduledTask -TaskName "GTM *" | Unregister-ScheduledTask -Confirm:$false
```
Or keep "GTM Evening Close" / weekly ones local if you prefer; just avoid
running the morning sweep in both places (idempotency makes a double-run
harmless, but it wastes API calls).
