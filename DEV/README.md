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

> Data source: **paid `api.sec-api.io`** (key required, calls metered).
> This is a *separate* system from the repo-root `coremont-signal-engine/`,
> which runs on free EDGAR.

## Quickstart

```powershell
cd DEV
pip install -r requirements.txt
Copy-Item .env.example .env          # then edit .env and paste your SEC_API_KEY
python tools/init_memory_db.py       # creates db/memory.db
```

Then drive the pipeline by following the workflows in order:

```powershell
# 01 — ingest (paid; respects SEC_API_BUDGET_CALLS)
python tools/sec_downloader.py query --form D --from 2026-01-01 --to 2026-06-01 `
    --keywords "Hedge Fund" "Private Offering"

# 02 — evaluate (local; no spend)
python tools/sec_parser.py --13f data/filings/<infotable>.xml
python tools/sec_parser.py --adv  data/filings/<adv>.json --what audit

# 03 — draft outreach (local; Tier-4 send is forbidden)
python tools/sales_copilot.py --crd <CRD>
```

Every tool prints a JSON envelope
(`{"status": "...", "data": ..., "error": ...}`) and exits `0` (success/skip),
`75` (retry), or `2` (fatal).

## Signals → Clarion pitch

| Signal | Source | Clarion value prop |
|---|---|---|
| `audit_delay` | ADV Sch. D §7.B.(1) Q.23 | consolidated IBOR/ABOR + Operations Concierge |
| `derivatives_complex` | 13F options >15% of book | quant library, real-time risk, valuation |
| `greenfield_launch` | new Form D / fund launch | turnkey, minimal middle-office headcount |

## Safety

- **No hardcoded keys** — `SEC_API_KEY` comes from `.env` (git-ignored).
- **Budget guard** — `SEC_API_BUDGET_CALLS` caps paid calls per run.
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
