"""End-to-end morning chain against mocked APIs:

form_d_sweep -> adv_fit_scorer -> apollo_contact_resolver -> outreach_drafter

Asserts the full flow turns one Form D filing into LinkedIn drafts sitting in
review (approved_at null), with provenance (source_run_id) at every hop.
Also runs the daily_morning_sweep orchestrator itself with the same fakes and
asserts the digest is produced with the drafting queue populated.
"""

from __future__ import annotations

import json
from pathlib import Path

from gtm.models.common import Channel
from gtm.skills import (
    adv_fit_scorer,
    apollo_contact_resolver,
    form_d_sweep,
    outreach_drafter,
)
from gtm.skills._shared.context import open_run
from gtm.skills._shared.llm import InjectedLLM
from gtm.skills._shared.sources import AdvProfile
from gtm.skills.tests.conftest import (
    VALID_CR,
    VALID_FOLLOWUP,
    FakeApollo,
    FakeEdgar,
    make_apollo_person,
    make_form_d,
    make_sources,
)


def _fixture_world(run_suffix, fresh_cik):
    """One new hedge-fund launch, its ADV record, and its COO in Apollo."""
    domain = f"sweepfund-{run_suffix}.com"
    crd = f"S{fresh_cik[:8]}"
    edgar = FakeEdgar(
        form_d=[make_form_d(fresh_cik, f"Sweepline Global Macro Fund {run_suffix} LP",
                            offering=500_000_000)],
        history_counts={fresh_cik: 1},
        adv={
            str(fresh_cik): AdvProfile(
                crd=crd, firm_name="Sweepline Global Macro Management",
                regulatory_aum_usd=800.0, aum_as_of="2026-03-31",
                website=domain, headquarters_city="New York",
                headquarters_country="United States",
            )
        },
    )
    apollo = FakeApollo(
        people_by_domain={
            domain: [
                make_apollo_person(
                    f"sw-{run_suffix}", f"Sweep Coo {run_suffix}", "Chief Operating Officer",
                    domain=domain, email=f"coo@{domain}",
                    linkedin_url=f"https://linkedin.com/in/sweep-coo-{run_suffix}",
                )
            ]
        }
    )
    return edgar, apollo, domain


def _track(cleanup, db, ctx, result):
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))


def test_full_chain_form_d_to_drafts(db, cleanup, run_suffix, fresh_cik):
    edgar, apollo, domain = _fixture_world(run_suffix, fresh_cik)

    # ── 1. discovery ─────────────────────────────────────────────────────────
    with open_run("form_d_sweep", sources=make_sources(edgar=edgar), db=db) as ctx1:
        sweep = form_d_sweep.run(ctx1)
    _track(cleanup, db, ctx1, sweep)
    assert sweep.status == "success"
    fund_id = sweep.metadata["fund_ids"][0]
    cleanup.append(("funds", fund_id))
    launch_signal = next(
        s for s in sweep.signals_emitted if db.signals.get(s).signal_type == "new_fund_launch"
    )

    # ── 2. scoring ───────────────────────────────────────────────────────────
    with open_run("adv_fit_scorer", sources=make_sources(edgar=edgar), db=db) as ctx2:
        scored = adv_fit_scorer.run(ctx2, fund_id=fund_id)
    _track(cleanup, db, ctx2, scored)
    assert scored.metadata["adv_available"] is True
    assert scored.metadata["tier"] in (1, 2), "macro launch in band must hit tier 1-2"

    fund = db.funds.get(__import__("uuid").UUID(fund_id))
    assert str(fund.primary_domain) == domain, "ADV website wired through as domain"
    assert fund.aum_band == "300_to_1b"
    assert "macro" in fund.strategies, "strategy inferred from ADV firm name"
    scoring_rows = (
        db.client.table("scoring_runs").select("id, reasoning").eq("entity_id", fund_id).execute()
    ).data
    for row in scoring_rows:
        cleanup.append(("scoring_runs", row["id"]))
    assert "Clarion coverage match" in scoring_rows[0]["reasoning"]

    # ── 3. contact resolution ────────────────────────────────────────────────
    with open_run("apollo_contact_resolver", sources=make_sources(apollo=apollo), db=db) as ctx3:
        contacts = apollo_contact_resolver.run(ctx3, fund_id=fund_id)
    _track(cleanup, db, ctx3, contacts)
    person_id = contacts.metadata["resolved"]["coo"][0]
    cleanup.append(("people", person_id))

    # ── 4. drafting (orchestrator-authored copy, injected) ──────────────────
    llm = InjectedLLM([json.dumps({
        "cr_variants": [VALID_CR, VALID_CR.replace("global macro", "macro launch"),
                        VALID_CR.replace("consolidated risk", "intraday Greeks")],
        "followup": VALID_FOLLOWUP,
    })])
    with open_run("outreach_drafter", sources=make_sources(llm=llm), db=db) as ctx4:
        drafts = outreach_drafter.run(
            ctx4, person_id=person_id, signal_id=str(launch_signal)
        )
    cleanup.append(("source_runs", str(ctx4.run_id)))
    for did in drafts.metadata["draft_ids"]:
        cleanup.append(("drafts", did))

    # ── the point of it all: drafts ready for review ─────────────────────────
    assert len(drafts.metadata["draft_ids"]) == 4
    rows = (
        db.client.table("drafts").select("*").in_("id", drafts.metadata["draft_ids"]).execute()
    ).data
    assert all(r["approved_at"] is None for r in rows), "nothing auto-approves"
    assert all(r["channel"] == "linkedin" for r in rows), "LinkedIn-only campaign"
    assert all(r["source_run_id"] == str(ctx4.run_id) for r in rows)

    # provenance chain end-to-end: every hop linked to its own run
    assert db.signals.get(launch_signal).source_run_id == ctx1.run_id
    person = db.people.get(__import__("uuid").UUID(person_id))
    assert person.source_run_id == ctx3.run_id
    assert person.is_buying_committee_member is True


def test_daily_morning_sweep_orchestrator(db, cleanup, run_suffix, fresh_cik, tmp_path):
    """The actual entry point, with fakes: digest written, queue populated."""
    from gtm.orchestrator import daily_morning_sweep

    edgar, apollo, _ = _fixture_world(run_suffix + "o", str(int(fresh_cik) + 7))
    # no web source: spinout_watcher degrades to partial; no hubspot: champion
    # check skipped — the sweep must still complete and produce a digest.
    sources = make_sources(edgar=edgar, apollo=apollo)

    summary = daily_morning_sweep.run_sweep(sources, db=db, dry_run=False, out_dir=tmp_path)

    # track for cleanup: runs are in summary; entity rows via scored list
    for entry in summary.get("scored", []):
        cleanup.append(("funds", entry["fund_id"]))

    assert summary["form_d_sweep"]["status"] == "success"
    assert summary["form_d_sweep"]["signals"] >= 2
    assert summary["scored"] and summary["scored"][0]["tier"] in (1, 2)
    assert summary["resolved"], "tier 1-2 fund must get contact resolution"

    queue = summary["draft_queue"]
    assert queue, "buying-committee member + fresh signals -> drafting queue entries"
    assert {"person_id", "signal_id", "signal_type", "urgency"} <= set(queue[0].keys())

    digest = Path(summary["digest_path"]).read_text(encoding="utf-8")
    assert "# Morning sweep digest" in digest
    assert "Drafting queue" in digest
    assert f"Sweepline Global Macro Fund {run_suffix}o LP" in digest

    # cleanup queue-related rows (signals/people created by the sweep)
    for item in queue:
        cleanup.append(("people", item["person_id"]))
        cleanup.append(("signals", item["signal_id"]))
