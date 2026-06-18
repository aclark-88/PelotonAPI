---
workflow: 03_outreach_generation
objective: Synthesize qualified leads into hyper-targeted sales copy matching firm-specific operational pain points to Clarion solutions.
inputs:
  - QUALIFIED entities + their observations (db/memory.db)
outputs:
  - Tailored email drafts written to drafts/{firm_name}_outreach.md
tools:
  - tools/sales_copilot.py
  - tools/db_client.py
tier: 3   # generates outbound *drafts* only; sending is Tier-4 and forbidden here
---

# Workflow 03 — Outreach Generation

## Execution steps
1. For each lead flagged `QUALIFIED`, the agent MUST read the strategy tags,
   executive contact details, and identified signals
   (`tools/db_client.py get --crd <crd>`).
2. The agent MUST draft customized email copy that references the **specific**
   operational signal:
   - **Audit Delay** → pitch Clarion's consolidated **IBOR/ABOR** and Coremont's
     middle-office **Operations Concierge** to eliminate data mismatches and
     speed up audit delivery.
   - **Derivatives Complex** → highlight Clarion's **quantitative library**,
     **real-time risk**, and **valuation framework**.
   - **Greenfield Launch** → position Clarion as an institutional-grade,
     **turnkey** setup requiring minimal internal middle-office headcount.
3. The agent MUST write the draft to `drafts/{firm_name}_outreach.md`
   (`tools/sales_copilot.py --crd <crd>`). The agent MAY then transition the
   entity to `OUTREACH_READY`.
4. The agent MUST NOT attempt to send the email or update CRM records directly.
   **This is a Tier-4 safety boundary that requires human review.**

## Error handling
- If no actionable signal is present, `sales_copilot` returns `skip`; the agent
  MUST NOT fabricate a generic pitch — leave the lead un-drafted.
- The agent MUST `log_execution` each draft outcome.

## Learnings
<!-- self-healing loop notes, newest first -->
