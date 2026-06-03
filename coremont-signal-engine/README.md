# Coremont Signal Engine

A Postgres-backed prospecting engine that identifies **hedge fund managers showing
fresh SEC filing signals** — fund launch, capital raising, platform expansion, or
strategy change — and ranks them by **Clarion PMS fit** for Coremont's outbound
pipeline.

It is *not* a general EDGAR browser. It is a scoring-and-alerting engine: it turns
public Form D (and adviser) filings into ranked prospect lists, account briefs, and
CRM-ready outreach triggers.

---

## What it does

1. **Ingests** newly filed and amended **SEC Form D** offerings (issuer, filing date,
   first-sale date, offering amount, amount sold/remaining, related persons, raw payload).
2. **Normalizes** issuer names so a manager's many legal entities (master, offshore,
   feeder, parallel, opportunities vehicles) collapse onto **one advisory platform**.
3. **Enriches** each manager with adviser (IAPD/Form ADV-style) context to tell a real
   platform apart from a one-off issuer name.
4. **Scores** every manager with a transparent **100-point rules engine** and assigns a
   priority **tier (1–4)**, with a human-readable reason for every point.
5. **Surfaces** the highest-fit managers in a lightweight web app and **exports** Tier 1/2
   prospects to CSV or HubSpot with score, reason, strategy tags, and a suggested buyer persona.

## ICP focus

Multi-strategy, macro, fixed income, rates, credit, and structured-credit firms — where
derivatives, treasury, financing, and cross-book visibility get harder as the platform
grows. The strongest signals are new vehicles, active raises, feeder/offshore structures,
adviser-footprint growth, and strategy expansion into complex risk/P&L workflows.

---

## Scoring model (transparent, tunable)

| Bucket | Cap | Example rules |
|---|---:|---|
| **Event strength** | 30 | new Form D ≤30d **+18**, first sale ≤45d **+8**, Form D/A activity **+4** |
| **Strategy fit** | 30 | weighted keyword overlap (macro, multi-strat, rates, credit, structured credit, ABS/MBS/RMBS/CMBS/CLO…), minus low-fit terms (venture, long-only, ETF, retail…) |
| **Complexity** | 25 | multi-vehicle **+8**, master/feeder **+6**, offshore **+6**, adviser footprint **+5** |
| **Reachability** | 15 | resolved identity **+6**, identifiable buyers **+5**, outreach path **+4** |

**Tiers:** Tier 1 = 75–100 · Tier 2 = 55–74 · Tier 3 = 35–54 · Tier 4 < 35.
Tier 1 routes to same-day outreach; Tier 2 gets enrichment before sequencing.

Every score carries an explanation (`ScoreBreakdown.lines`) so the model can be tuned
against real win/loss outcomes rather than treated as a black box.

---

## Architecture

```
app/
  config.py            env-driven config (Postgres URL, SEC UA, lookback, HubSpot)
  db.py                SQLAlchemy engine/session (Postgres prod, SQLite dev/test)
  models.py            managers · fund_vehicles · filings · signals · contacts · research_notes
  taxonomy.py          weighted keyword dictionary (strong / medium / negative)
  normalization.py     manager-key collapsing + master/feeder/offshore classification
  scoring.py           100-point rules engine → ScoreBreakdown + tier
  signals.py           signal detection + sales-ready reason strings
  personas.py          Clarion buyer personas (COO/Ops/Risk/Finance/Treasury) + pain mapping
  ingestion/
    edgar_client.py    SEC daily-index discovery + Form D primary_doc.xml parser
    formd.py           Job 1+2: persist Form D, normalize entities
    adviser.py         Job 3: adviser (IAPD) enrichment
    signal_job.py      Job 4: generate signals + cache manager scores/tiers
    export_job.py      Job 5: CSV + HubSpot export
    pipeline.py        daily orchestrator (Jobs 1–5)
  web/server.py        FastAPI app: ranked managers, manager detail, filing explorer, export
  cli.py               initdb · ingest · export · stats
seed/                  offline sample Form D data + a real-shaped primary_doc.xml
tests/                 pytest: taxonomy, normalization, scoring, full pipeline, XML parser
```

**Data model** is manager-centred so multiple vehicles and filing events roll up to a
single advisory platform.

---

## Quick start

**One command** (creates an isolated `.venv`, installs, seeds, and serves):

```bash
# macOS / Linux
cd coremont-signal-engine
./run.sh
# → http://localhost:8000   (PORT=9000 ./run.sh to change port)
```

```powershell
# Windows PowerShell
cd coremont-signal-engine
.\run.ps1
# → http://localhost:8000
# If blocked ("running scripts is disabled"):
#   powershell -ExecutionPolicy Bypass -File .\run.ps1
```

Or step by step:

```bash
cd coremont-signal-engine
pip install -r requirements.txt

# 1. Create tables (SQLite by default; set DATABASE_URL for Postgres)
python -m app.cli initdb

# 2. Load the bundled sample and run the full pipeline (Jobs 1–5), offline
python -m app.cli ingest --seed

# 3. Launch the web app
uvicorn app.web.server:app --reload --port 8000
# → http://localhost:8000
```

### Live SEC ingestion

```bash
export SEC_USER_AGENT="Your Firm (you@example.com)"   # SEC requires a contact UA
python -m app.cli ingest --lookback 7                 # scan last 7 business days
```

SEC EDGAR requires a descriptive `User-Agent` and rate-limits requests; the client
respects both. If outbound SEC access is blocked in your environment, use `--seed`.

### Postgres (production)

```bash
export DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/coremont"
python -m app.cli initdb
python -m app.cli ingest
```

### Daily email digest (automatic)

Get the full Tier 1+2 queue in your inbox every morning, with newly-surfaced
managers flagged **NEW**.

1. Configure email once. Copy `.env.example` → `.env` and set the SMTP block
   (Gmail needs a 16-char **App Password**, not your login password —
   Google Account → Security → 2-Step Verification → App passwords):

   ```
   SMTP_USER=you@gmail.com
   SMTP_PASSWORD=your-app-password
   DIGEST_TO=you@gmail.com
   ```

2. Build + send a digest now:

   ```bash
   python -m app.cli digest            # live SEC refresh + email (seed fallback)
   python -m app.cli digest --seed     # use bundled sample data
   python -m app.cli digest --no-email # just write exports/digest.html
   ```

3. **Schedule it daily (Windows):**

   ```powershell
   .\run.ps1                 # once, to create the .venv (Ctrl-C after it starts)
   powershell -ExecutionPolicy Bypass -File .\setup-daily.ps1            # 7:00 AM
   # or pick a time:  ... -File .\setup-daily.ps1 -Time 06:30
   ```

   This registers a `CoremontSignalDigest` scheduled task that runs
   `daily-digest.ps1` every morning (refresh → build → email; logs to
   `exports\digest.log`). Test immediately with
   `Start-ScheduledTask -TaskName CoremontSignalDigest`, remove with
   `Unregister-ScheduledTask -TaskName CoremontSignalDigest -Confirm:$false`.

   On macOS/Linux, schedule `python -m app.cli digest` via cron, e.g.
   `0 7 * * * cd /path/to/coremont-signal-engine && .venv/bin/python -m app.cli digest`.

### CRM export

```bash
python -m app.cli export --min-tier 2     # writes exports/coremont_export_<date>.csv
export HUBSPOT_TOKEN="pat-..."            # optional: enables HubSpot company upsert
```

---

## Web screens

- **Ranked Managers** (`/`) — home queue ranked by score, with filters for tier,
  strategy tag, signal type, geography, freshness, and search.
- **Manager Detail** (`/managers/{id}`) — profile, vehicles, filing timeline, score
  breakdown, signals, suggested personas, and a plain-English **"Why Coremont now?"**.
- **Filing Explorer** (`/filings`) — raw + normalized filings with filters for form,
  amendment status, raise size, and vehicle structure.
- **Export Queue** (`/export`) — outreach-ready Tier 1/2 prospects with CSV download and
  HubSpot push.

---

## Signals detected

| Signal | Trigger |
|---|---|
| New fund launch | new (non-amendment) Form D for a fresh vehicle, ≤30 days |
| Active capital raise | non-zero amount sold or a meaningful target |
| Ongoing raise momentum | a Form D/A amending an earlier filing |
| Platform expansion | adviser/footprint with multiple related private funds |
| Strategy expansion | strong overlap with Clarion's macro / credit / rates ICP |
| Structural complexity | master / feeder / offshore patterns |

---

## Tests

```bash
python -m pytest -q      # 21 tests: taxonomy, normalization, scoring, full pipeline, XML parser
```

The Form D XML parser is tested against a **real-shaped `primary_doc.xml`**, so it keeps
working even with no live SEC access.

## Scheduling

Jobs 1–5 run linearly in `pipeline.run_pipeline()` — schedule `python -m app.cli ingest`
daily via cron, a GitHub Action, or any task runner. No queue or broker required for v1.

## Roadmap

- **Phase 1 (this build):** Form D ingestion, normalization, strategy tagging, rules
  scoring, ranked queue, CSV export.
- **Phase 2:** deeper adviser enrichment, signal histories, live HubSpot sync.
- **Phase 3:** broader web enrichment, people mapping, AI-generated account briefs.

---

*Data sources: SEC EDGAR Form D (Reg D exempt offerings) and Investment Adviser Public
Disclosure / Form ADV context via `data.sec.gov`. v1 deliberately avoids full-document
NLP across all form types — it targets high-value public signals, normalizes them at the
manager level, and turns them into actionable Clarion-fit scores.*
