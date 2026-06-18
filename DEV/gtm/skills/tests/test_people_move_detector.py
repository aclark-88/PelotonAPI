"""people_move_detector: Apollo path, champion relocation, idempotency, errors."""

from __future__ import annotations

from gtm.models.funds import FundIn
from gtm.models.people import PersonIn
from gtm.skills import people_move_detector
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import (
    FakeApollo,
    FakeHubSpot,
    make_apollo_person,
    make_sources,
)


def _seed_tam_fund(db, cleanup, run_suffix, domain):
    fund = db.funds.upsert_fund(
        FundIn(
            legal_name=f"Move Target Fund {run_suffix} LP",
            primary_domain=domain,
            strategies=["macro"],
        )
    )
    db.funds.record_fit_score(fund.id, 85, model_version="test", tier=1)
    cleanup.append(("funds", str(fund.id)))
    return db.funds.get(fund.id)


def _seed_prior_fund(db, cleanup, run_suffix):
    fund = db.funds.upsert_fund(FundIn(legal_name=f"Prior Employer {run_suffix} LP"))
    cleanup.append(("funds", str(fund.id)))
    return fund


def _track_signals(db, cleanup, result):
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))


def test_move_into_tam_fund_full_flow(db, cleanup, run_suffix):
    domain = f"movetarget-{run_suffix}.com"
    tam_fund = _seed_tam_fund(db, cleanup, run_suffix, domain)
    prior = _seed_prior_fund(db, cleanup, run_suffix)

    linkedin = f"https://linkedin.com/in/champion-{run_suffix}"
    person = db.people.upsert_person(
        PersonIn(
            full_name=f"Champion Mover {run_suffix}",
            linkedin_url=linkedin,
            email=f"champion-{run_suffix}@prior.com",
            current_fund_id=prior.id,
            current_role="VP Operations",
        )
    )
    cleanup.append(("people", str(person.id)))
    db.client.table("employment_history").insert(
        {
            "person_id": str(person.id), "fund_id": str(prior.id),
            "role": "VP Operations", "function": "ops", "seniority": "vp",
            "started_at": "2023-01-01", "source": "test",
        }
    ).execute()

    apollo = FakeApollo(
        people_by_domain={
            domain: [
                make_apollo_person(
                    f"ap-{run_suffix}", f"Champion Mover {run_suffix}",
                    "Chief Operating Officer", domain=domain,
                    email=f"champion-{run_suffix}@prior.com", linkedin_url=linkedin,
                )
            ]
        }
    )
    hubspot = FakeHubSpot(
        contacts=[{"id": "hs-123", "email": f"champion-{run_suffix}@prior.com"}]
    )

    with open_run(
        "people_move_detector", sources=make_sources(apollo=apollo, hubspot=hubspot), db=db
    ) as ctx:
        result = people_move_detector.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))
    _track_signals(db, cleanup, result)
    history = db.people.employment_history(person.id)
    for row in history:
        cleanup.append(("employment_history", str(row.id)))

    assert result.status in ("success", "partial")
    assert any(m["person"] == f"Champion Mover {run_suffix}" for m in result.metadata["moves"])

    # employment flow ran atomically
    open_rows = [h for h in history if h.ended_at is None]
    assert len(open_rows) == 1 and open_rows[0].fund_id == tam_fund.id

    refreshed = db.people.get(person.id)
    assert refreshed.current_fund_id == tam_fund.id
    assert refreshed.is_buying_committee_member is True

    # signals: new_role (champion-upgraded) + new_coo
    signals = [db.signals.get(s) for s in result.signals_emitted]
    types = {s.signal_type for s in signals}
    assert {"new_role", "new_coo"} <= types
    new_role = next(s for s in signals if s.signal_type == "new_role")
    assert new_role.urgency.value == "immediate", "champion relocation forces immediate"
    assert new_role.metadata["champion_relocation"] is True
    assert new_role.metadata["hubspot_contact_id"] == "hs-123"


def test_rerun_is_idempotent(db, cleanup, run_suffix):
    domain = f"idem-{run_suffix}.com"
    tam_fund = _seed_tam_fund(db, cleanup, run_suffix + "i", domain)
    apollo = FakeApollo(
        people_by_domain={
            domain: [
                make_apollo_person(
                    f"ap-i-{run_suffix}", f"Idem Person {run_suffix}",
                    "Chief Financial Officer", domain=domain,
                    linkedin_url=f"https://linkedin.com/in/idem-{run_suffix}",
                )
            ]
        }
    )
    sources = make_sources(apollo=apollo)

    with open_run("people_move_detector", sources=sources, db=db) as ctx1:
        first = people_move_detector.run(ctx1)
    cleanup.append(("source_runs", str(ctx1.run_id)))
    _track_signals(db, cleanup, first)
    moved = [m for m in first.metadata["moves"] if m["fund"] == tam_fund.legal_name]
    assert moved, "first run must record the move"

    with open_run("people_move_detector", sources=sources, db=db) as ctx2:
        second = people_move_detector.run(ctx2)
    cleanup.append(("source_runs", str(ctx2.run_id)))
    _track_signals(db, cleanup, second)

    assert not [
        m for m in second.metadata["moves"] if m["fund"] == tam_fund.legal_name
    ], "person already in seat — second run must not re-observe"

    person = db.people.upsert_person(
        PersonIn(full_name=f"Idem Person {run_suffix}", linkedin_url=f"https://linkedin.com/in/idem-{run_suffix}")
    )
    cleanup.append(("people", str(person.id)))
    history = db.people.employment_history(person.id)
    for row in history:
        cleanup.append(("employment_history", str(row.id)))
    assert len([h for h in history if h.ended_at is None]) == 1


def test_apollo_failure_records_partial(db, cleanup, run_suffix):
    _seed_tam_fund(db, cleanup, run_suffix + "f", f"fail-{run_suffix}.com")
    apollo = FakeApollo(fail=True)

    with open_run("people_move_detector", sources=make_sources(apollo=apollo), db=db) as ctx:
        result = people_move_detector.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "partial"
    assert any(e["where"] == "apollo_search" for e in result.errors)


def test_hubspot_absent_degrades_gracefully(db, cleanup, run_suffix):
    domain = f"nohs-{run_suffix}.com"
    _seed_tam_fund(db, cleanup, run_suffix + "h", domain)
    apollo = FakeApollo(
        people_by_domain={
            domain: [
                make_apollo_person(
                    f"ap-h-{run_suffix}", f"NoHubspot Person {run_suffix}",
                    "Head of Risk", domain=domain,
                    linkedin_url=f"https://linkedin.com/in/nohs-{run_suffix}",
                )
            ]
        }
    )

    # no hubspot in the bundle at all
    with open_run("people_move_detector", sources=make_sources(apollo=apollo), db=db) as ctx:
        result = people_move_detector.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))
    _track_signals(db, cleanup, result)
    person = db.people.upsert_person(
        PersonIn(full_name=f"NoHubspot Person {run_suffix}", linkedin_url=f"https://linkedin.com/in/nohs-{run_suffix}")
    )
    cleanup.append(("people", str(person.id)))
    for row in db.people.employment_history(person.id):
        cleanup.append(("employment_history", str(row.id)))

    assert result.status in ("success", "partial")
    signals = [db.signals.get(s) for s in result.signals_emitted]
    new_role = next(s for s in signals if s.signal_type == "new_role")
    assert "champion_relocation" not in new_role.metadata, "no champion check without HubSpot"
    assert new_role.urgency.value == "this_week", "default urgency stands"
