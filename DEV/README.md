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

## Quickstart

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
