# Scheduling the orchestrator

**Chosen: local Windows Task Scheduler** — this machine already runs the
morning-brief pipeline locally (`morning-open.ps1` at login), there is no
Docker for containerized runners, and laptop-based runs are the right rollout
posture while the system earns trust. The cloud-native alternative is
documented at the bottom; switch when runs need to survive the laptop being
off.

## Schedule

| Task | Script | When (ET) |
|---|---|---|
| Morning sweep | `py -m gtm.orchestrator.daily_morning_sweep` | Daily 06:00 |
| Evening close | `py -m gtm.orchestrator.daily_evening_close` | Daily 18:00 |
| Monday pipeline review | `py -m gtm.orchestrator.weekly_monday_brief` | Mon 07:00 |
| Competitive sweep | `py -m gtm.orchestrator.weekly_wednesday_competitive` | Wed 09:00 |
| Friday retro | `py -m gtm.orchestrator.weekly_friday_retro` | Fri 16:00 |

## One-time registration (run in an elevated PowerShell)

```powershell
$py = (Get-Command py).Source
$wd = "C:\Users\malex\PelotonAPI\DEV"

$jobs = @(
  @{Name="GTM Morning Sweep";   Args="-m gtm.orchestrator.daily_morning_sweep";        Trigger=(New-ScheduledTaskTrigger -Daily -At 6:00am)},
  @{Name="GTM Evening Close";   Args="-m gtm.orchestrator.daily_evening_close";        Trigger=(New-ScheduledTaskTrigger -Daily -At 6:00pm)},
  @{Name="GTM Monday Brief";    Args="-m gtm.orchestrator.weekly_monday_brief";        Trigger=(New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 7:00am)},
  @{Name="GTM Competitive";     Args="-m gtm.orchestrator.weekly_wednesday_competitive"; Trigger=(New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At 9:00am)},
  @{Name="GTM Friday Retro";    Args="-m gtm.orchestrator.weekly_friday_retro";        Trigger=(New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 4:00pm)}
)
foreach ($j in $jobs) {
  $action = New-ScheduledTaskAction -Execute $py -Argument $j.Args -WorkingDirectory $wd
  Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $j.Trigger -Force
}
```

Notes:
- Times above are local machine time — confirm the machine clock is ET or
  adjust the `-At` values.
- Tasks run only while the machine is on; add
  `-Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)` to catch up
  after wake.
- All scripts exit non-zero on stage failure, visible in Task Scheduler
  history; failures also post to Slack once `SLACK_WEBHOOK_URL` is set.

## Drafting is NOT scheduled

The morning sweep ends with a **drafting queue** in the digest. Generation is
done by an orchestrated Claude Code session (the orchestrator-is-the-LLM
convention): open a session and say "work the drafting queue" — Claude reads
the digest, calls `outreach_drafter.prepare_prompt()` per target, authors the
copy, and stores drafts for approval. Dispatch (`heyreach_dispatcher`) only
ever runs against human-approved drafts.

## Webhooks

`py -m gtm.orchestrator.event_handlers --port 8787` serves
`/webhooks/heyreach` and `/webhooks/hubspot`. For inbound reachability use an
ngrok tunnel during rollout; long-term, move these two handlers to a Supabase
Edge Function (the schema and repos already live there).

## Alternative (not implemented): Supabase Edge Functions + pg_cron

Cloud-native path when laptop scheduling outgrows itself: port each
orchestrator script to a Deno Edge Function, schedule with pg_cron
(`select cron.schedule('morning-sweep', '0 10 * * *', $$...$$)` — 10:00 UTC =
06:00 ET), and keep secrets in Supabase Vault. Cost: porting the Python
skills' EDGAR dependency (edgartools is Python-only — the Edge Function would
shell out to a hosted job or the GH Actions runner instead). That dependency
is why local-first won for now.
