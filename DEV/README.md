# WAT v2 — Coremont Clarion SEC Prospecting Engine

A local, agentic prospecting engine that turns **public SEC filings** (Form D,
Form ADV, 13F) into **ranked, signal-tagged hedge-fund leads** for **Coremont
Clarion**, and drafts tailored outreach for human review.

Built on the **WAT v2** three-layer pattern:

| Layer | Location | Role |
|---|---|---|
| 1 — Workflows | `workflows/*.md` | RFC-2119 SOPs (the *what*) |
| 3 — Tools | `tools/*.py` | single-purpose scripts (the *how*), strict JSON I/O |
| Memory | `db/memory.db` | cross-session SQLite state |
| System prompt | `claude.md` | orchestration contract, autonomy tiers, safety |

> Data source: **free SEC EDGAR** via the [`edgartools`](https://github.com/dgunning/edgartools)
> library (no API key, no per-call spend). This `DEV/` system is *separate* from
> the repo-root `coremont-signal-engine/`; they differ in **architecture** (this
> is the WAT v2 agentic layer), not data source.

## Daily morning brief (start here)

The everyday way to use this: **double-click `brief.ps1`** (or run `.\brief.ps1`).
It scans recent EDGAR activity for buying signals, ranks them, and **opens a
dashboard** (`briefs/latest.html`) you read with coffee.

```powershell
.\brief.ps1                      # last few days, opens the dashboard
.\brief.ps1 --days 7             # widen the Form D lookback
.\brief.ps1 --cap-13f 150        # scan more 13F managers (use around 13F deadlines)
```

What it surfaces, ranked High / Medium / Watch:

| Signal | Meaning | Cadence |
|---|---|---|
| **New fund launch** | a new pooled **hedge-fund** Form D notice (VC/PE/RE filtered out) | daily |
| **AUM growth** | a tracked manager's 13F book up >15% vs. the prior quarter | quarterly* |
| **New derivatives** | a manager's options crossed/rose past 15% of the 13F book | quarterly* |

\* 13F is filed quarterly, so growth/derivatives signals cluster around the
filing deadlines (mid-Feb / May / Aug / Nov). Most mornings the brief is
launch-driven. The engine **auto-tracks** every 13F manager it sees, so the
quarter-over-quarter baselines build themselves over time (first sighting =
baseline, no signal). Pin specific funds in `config/watchlist.txt`.

Each card links to the EDGAR filing and shows the one-liner to draft outreach:
`py tools/sales_copilot.py --crd CIK<cik>`. **Nothing is ever sent** — drafting
only (Tier-4).

### Tuning the ICP filter (cutting the noise)

Form D has **no strategy field** — it only tags a fund as Hedge Fund / Private
Equity / Venture Capital / Other Investment Fund. So PE, VC, real-estate, and
private-credit vehicles leak in (mostly as "Other Investment Fund"). The brief
classifies each launch from its **name + type** using editable lexicons in
**`config/icp_filters.json`**:

- `exclude_fund_types` — hard-drop these Form D types (default: PE, VC).
- `negative_terms` — drop any fund whose name signals real estate, private
  credit / direct lending, infrastructure, energy, buyout, royalty, etc.
- `positive_terms` — strategy keywords (global macro, relative value, fixed
  income, structured credit, CLO, convertible/vol arb, multi-strategy, …) with
  weights that score and rank a fund.
- `require_strategy_match` (default `true`) — non-hedge-fund types must name an
  ICP strategy to appear; "Hedge Fund" type is always kept (ranked low unless a
  strategy is named). Set `false` to broaden.

The dashboard shows a green banner with how many non-ICP vehicles were filtered
and why, so you can audit and tune. Only pooled investment funds are considered;
operating companies and non-fund issuers are dropped automatically.

### Verification (the noise-killer that name filters can't do)

Form D self-classification is unreliable — a real-estate HTC private lender
(Octagon Finance) filed as "Hedge Fund" with "Credit" in its name. So the brief
also consumes an **authoritative verdict store**, `config/verifications.json`,
that overrides the heuristics. A verdict is produced by checking what the manager
*actually is* (web / Form ADV), following **`workflows/04_verify_candidates.md`**:

```powershell
py tools/verify_store.py pending          # candidates needing a verdict
py tools/verify_store.py set --cik 2064620 --target false `
    --business "Real-estate / HTC private lender (Octagon Finance)"
py tools/morning_brief.py                 # re-run; non-targets drop, targets -> High + "Verified"
```

Verdicts persist, so verification knowledge **compounds** — a manager confirmed
as noise is never surfaced again. Cards show a green **Verified** badge with the
confirmed business; un-checked candidates show **Unverified – needs review**.
Run verification agentically: ask Claude Code to "verify today's pending
candidates" and it web-checks each and records the verdicts.

## Quickstart (one-time setup)

```powershell
cd DEV
py -m pip install -r requirements.txt   # use `py`, not `python` (Store stub) on this machine
Copy-Item .env.example .env             # edit .env: set EDGAR_IDENTITY = "Name email"
py tools/init_memory_db.py              # creates db/memory.db
```

Then drive the pipeline by following the workflows in order:

```powershell
# 01 — ingest (free EDGAR; respects EDGAR_MAX_FILINGS)
py tools/sec_downloader.py query    --form D --from 2026-03-01 --to 2026-03-07
py tools/sec_downloader.py download --accession <acc> --kind formd
py tools/sec_downloader.py download --accession <acc> --kind 13f

# 02 — evaluate (local; no network)
py tools/sec_parser.py --13f data/filings/13f_<acc>.xml
# audit_delay only if you supply an external ADV record (ADV is NOT on EDGAR):
py tools/sec_parser.py --adv data/filings/<adv>.json --what audit

# 03 — draft outreach (local; Tier-4 send is forbidden)
py tools/sales_copilot.py --crd <CRD>
```

Every tool prints a JSON envelope
(`{"status": "...", "data": ..., "error": ...}`) and exits `0` (success/skip),
`75` (retry), or `2` (fatal).

## Signals → Clarion pitch

| Signal | Source | EDGAR-native? | Clarion value prop |
|---|---|---|---|
| `derivatives_complex` | 13F options >15% of book | ✅ yes | quant library, real-time risk, valuation |
| `greenfield_launch` | new pooled-investment Form D | ✅ yes | turnkey, minimal middle-office headcount |
| `audit_delay` | ADV Sch. D §7.B.(1) Q.23 | ❌ ADV is on IARD, not EDGAR — needs external feed | consolidated IBOR/ABOR + Operations Concierge |

## Safety

- **No keys, free data** — SEC EDGAR is public; only an `EDGAR_IDENTITY`
  ("Name email") fair-access User-Agent is needed, read from `.env` (git-ignored).
- **Volume guard** — `EDGAR_MAX_FILINGS` bounds how many filings a query pulls.
- **Tier-4 boundary** — the engine drafts only; sending email / CRM writes /
  deletions require explicit human approval (see `claude.md`).
- `audit_delay` detection is **defensive**: it fires only when the audit field is
  confidently located, and the exact field path is marked `# VERIFY` in
  `tools/sec_parser.py` pending confirmation against live ADV data.

## Directory map

```
DEV/
├─ claude.md                 system prompt (read first)
├─ workflows/                Layer 1 SOPs
│  ├─ 01_sec_data_ingestion.md
│  ├─ 02_evaluate_prospects.md
│  └─ 03_outreach_generation.md
├─ tools/                    Layer 3 scripts
│  ├─ _shared.py             JSON envelope + path resolution
│  ├─ init_memory_db.py
│  ├─ sec_downloader.py
│  ├─ sec_parser.py
│  ├─ db_client.py
│  └─ sales_copilot.py
├─ db/                       memory.db (generated)
├─ data/filings/            downloaded raw filings (generated)
└─ drafts/                   outreach drafts (generated)
```
