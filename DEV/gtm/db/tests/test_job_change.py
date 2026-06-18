"""The fn_observe_job_change flow: history closed, person updated, signal emitted."""

from __future__ import annotations

from datetime import datetime, timezone

from gtm.db.repositories.funds import FundsRepo
from gtm.db.repositories.people import PeopleRepo
from gtm.models.common import RoleFunction, Seniority
from gtm.models.funds import FundIn
from gtm.models.people import PersonIn


def test_job_change_flow(db, run_suffix, cleanup):
    funds = FundsRepo(db)
    people = PeopleRepo(db)

    fund_a = funds.upsert_fund(
        FundIn(legal_name=f"Mothership Capital {run_suffix} LP", strategies=["multi_strategy"])
    )
    fund_b = funds.upsert_fund(
        FundIn(legal_name=f"Spinout Partners {run_suffix} LP", strategies=["macro"])
    )
    cleanup.append(("funds", str(fund_a.id)))
    cleanup.append(("funds", str(fund_b.id)))

    person = people.upsert_person(
        PersonIn(
            full_name=f"Test Champion {run_suffix}",
            linkedin_url=f"https://linkedin.com/in/test-{run_suffix}",
            current_fund_id=fund_a.id,
            current_role="Deputy COO",
            current_role_seniority=Seniority.vp,
            current_role_function=RoleFunction.ops,
        )
    )
    cleanup.append(("people", str(person.id)))
    assert person.is_buying_committee_member is False, "vp/ops is not committee"

    # open employment row at fund A
    db.table("employment_history").insert(
        {
            "person_id": str(person.id),
            "fund_id": str(fund_a.id),
            "role": "Deputy COO",
            "function": "ops",
            "seniority": "vp",
            "started_at": "2024-01-01",
            "source": "test",
        }
    ).execute()

    observed = datetime.now(timezone.utc)
    signal = people.observe_job_change(
        person_id=person.id,
        new_fund_id=fund_b.id,
        new_role="Chief Operating Officer",
        observed_at=observed,
        function=RoleFunction.ops,
        seniority=Seniority.c_suite,
        source="test",
    )
    cleanup.append(("signals", str(signal.id)))

    # 1. signal emitted with the right shape
    assert signal.signal_type == "new_role"
    assert signal.fund_id == fund_b.id
    assert signal.person_id == person.id
    assert signal.payload["previous_fund_id"] == str(fund_a.id)

    # 2. old employment row closed, new one open at fund B
    history = people.employment_history(person.id)
    for row in history:
        cleanup.append(("employment_history", str(row.id)))
    open_rows = [h for h in history if h.ended_at is None]
    closed_rows = [h for h in history if h.ended_at is not None]
    assert len(open_rows) == 1
    assert open_rows[0].fund_id == fund_b.id
    assert any(c.fund_id == fund_a.id for c in closed_rows)

    # 3. people.current_* updated; c_suite/ops now flips the committee flag
    refreshed = people.get(person.id)
    assert refreshed.current_fund_id == fund_b.id
    assert refreshed.current_role == "Chief Operating Officer"
    assert refreshed.is_buying_committee_member is True

    # 4. idempotent: same observation dedupes to the same signal AND does not
    #    churn the employment timeline
    again = people.observe_job_change(
        person_id=person.id,
        new_fund_id=fund_b.id,
        new_role="Chief Operating Officer",
        observed_at=observed,
        function=RoleFunction.ops,
        seniority=Seniority.c_suite,
        source="test",
    )
    assert again.id == signal.id
    history_after = people.employment_history(person.id)
    assert len(history_after) == len(history), "re-observation must not add employment rows"
    assert sum(1 for h in history_after if h.ended_at is None) == 1
