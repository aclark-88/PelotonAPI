---
workflow: 04_verify_candidates
objective: Verify that each Form D launch candidate is a genuine securities-trading hedge fund (active management needing real-time risk/P&L) and not a tangential vehicle (real estate, private credit / direct lending, PE, operating company) that mis-classified itself on Form D.
inputs:
  - briefs/latest.json (today's candidates)
  - web access (manager websites, IAPD/ADV, news)
outputs:
  - config/verifications.json verdicts (authoritative; consumed by the brief)
  - a refreshed dashboard with verified targets only
tools:
  - tools/verify_store.py
  - tools/morning_brief.py
tier: 2   # read-only web research; writing verdicts is local
---

# Workflow 04 — Verify Candidates (agentic)

Form D self-classification and fund names are **unreliable** (e.g. an HTC
real-estate private lender filed as "Hedge Fund" with "Credit" in its name). The
agent MUST confirm what each manager actually is before it reaches outreach.

## Execution steps
1. The agent MUST list the candidates still lacking a verdict:
   `python tools/verify_store.py pending`.
2. For each pending candidate, the agent MUST research the **actual manager**,
   not the fund name. It SHOULD use the Form D issuer's related persons / adviser
   and address (from EDGAR) plus a web search of the management entity, and MAY
   consult the SEC IAPD/Form ADV brochure for the adviser's described business.
3. The agent MUST classify each as:
   - **is_target = true** — an active securities-trading manager whose book needs
     real-time risk/P&L across OTC derivatives, structured credit, fixed income,
     relative value, global macro, equity long/short, multi-strategy, volatility,
     convertibles, or similar liquid strategies.
   - **is_target = false** — real estate / development, private credit / direct
     lending, PE / buyout / VC, operating companies, family offices, fund-of-
     funds, or anything that does not trade a liquid book.
   The agent MUST default to **false** when the evidence is inconclusive, and
   record the uncertainty in the business note (do not invent a verdict).
4. The agent MUST record each verdict:
   `python tools/verify_store.py set --cik <cik> --target true|false --business "<one line on what it actually is>"`.
5. The agent MUST re-run `python tools/morning_brief.py` so the dashboard
   reflects the verdicts (non-targets drop; targets are promoted to High).
6. The agent MUST NOT mark a vehicle as a target on the strength of its name or
   Form D type alone — a verdict requires evidence about the real business.

## Notes
- Verdicts are **persistent and authoritative**: once recorded, a manager is
  never re-surfaced as noise, so verification knowledge compounds over time.
- This is read-only research (Tier 2). It performs no outreach (Tier 4).

## Learnings
- 2026-06-03 — Octagon Credit Partners II Feeder (CIK 2064620) filed as "Hedge
  Fund" but Octagon Finance LLC is an HTC real-estate private lender → is_target
  false. Confirms name/type heuristics are insufficient; web verification added.
