"""apollo_contact_resolver: resolution, gaps, manual-verification protection."""

from __future__ import annotations

from gtm.models.funds import FundIn
from gtm.models.people import PersonIn
from gtm.skills import apollo_contact_resolver
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import FakeApollo, make_apollo_person, make_sources


def _seed_fund(db, cleanup, run_suffix, domain):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"Resolver Fund {run_suffix} LP", primary_domain=domain,
               strategies=["macro"])
    )
    cleanup.append(("funds", str(fund.id)))
    return fund


def test_resolves_contacts_and_flags_gaps(db, cleanup, run_suffix):
    domain = f"resolver-{run_suffix}.com"
    fund = _seed_fund(db, cleanup, run_suffix, domain)
    apollo = FakeApollo(
        people_by_domain={
            domain: [
                make_apollo_person(
                    f"res-{run_suffix}", f"Res Coo {run_suffix}", "Chief Operating Officer",
                    domain=domain, linkedin_url=f"https://linkedin.com/in/res-coo-{run_suffix}",
                    email=f"coo-{run_suffix}@{domain}",
                )
            ]
        }
    )

    with open_run("apollo_contact_resolver", sources=make_sources(apollo=apollo), db=db) as ctx:
        result = apollo_contact_resolver.run(ctx, fund_id=str(fund.id), roles=["coo", "head_risk"])
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))
    for ids in result.metadata["resolved"].values():
        for pid in ids:
            cleanup.append(("people", pid))

    assert result.status in ("success", "partial")
    assert "coo" in result.metadata["resolved"]
    assert result.metadata["gaps"] == ["head_risk"]

    person = db.people.get(__import__("uuid").UUID(result.metadata["resolved"]["coo"][0]))
    assert person.current_fund_id == fund.id
    assert person.current_role_function.value == "ops"
    assert person.is_buying_committee_member is True
    assert person.metadata["resolved_role"] == "coo"

    gap = db.signals.get(result.signals_emitted[0])
    assert gap.signal_type == "contact_gap"
    assert gap.payload["role"] == "head_risk"


def test_gap_signal_dedupes_on_rerun(db, cleanup, run_suffix):
    domain = f"gapdedupe-{run_suffix}.com"
    fund = _seed_fund(db, cleanup, run_suffix + "g", domain)
    apollo = FakeApollo(people_by_domain={})
    sources = make_sources(apollo=apollo)

    with open_run("apollo_contact_resolver", sources=sources, db=db) as ctx1:
        first = apollo_contact_resolver.run(ctx1, fund_id=str(fund.id), roles=["cto"])
    cleanup.append(("source_runs", str(ctx1.run_id)))
    for sid in first.signals_emitted:
        cleanup.append(("signals", str(sid)))

    with open_run("apollo_contact_resolver", sources=sources, db=db) as ctx2:
        second = apollo_contact_resolver.run(ctx2, fund_id=str(fund.id), roles=["cto"])
    cleanup.append(("source_runs", str(ctx2.run_id)))

    assert [str(s) for s in first.signals_emitted] == [str(s) for s in second.signals_emitted]


def test_manually_verified_contact_never_overwritten(db, cleanup, run_suffix):
    domain = f"verified-{run_suffix}.com"
    fund = _seed_fund(db, cleanup, run_suffix + "v", domain)
    linkedin = f"https://linkedin.com/in/verified-{run_suffix}"

    person = db.people.upsert_person(
        PersonIn(
            full_name=f"Hand Checked {run_suffix}",
            linkedin_url=linkedin,
            current_role="Chief Operating Officer (verified by Alex)",
            metadata={"manually_verified": True},
        )
    )
    cleanup.append(("people", str(person.id)))

    apollo = FakeApollo(
        people_by_domain={
            domain: [
                make_apollo_person(
                    f"ver-{run_suffix}", f"Hand Checked {run_suffix}", "Chief Operating Officer",
                    domain=domain, linkedin_url=linkedin, email=f"wrong-{run_suffix}@stale.com",
                )
            ]
        }
    )

    with open_run("apollo_contact_resolver", sources=make_sources(apollo=apollo), db=db) as ctx:
        result = apollo_contact_resolver.run(ctx, fund_id=str(fund.id), roles=["coo"])
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))

    refreshed = db.people.get(person.id)
    assert refreshed.current_role == "Chief Operating Officer (verified by Alex)"
    assert refreshed.email is None, "Apollo's stale email must not overwrite a verified contact"
    assert refreshed.metadata["manually_verified"] is True


def test_no_domain_is_clean_error(db, cleanup, run_suffix):
    fund = db.funds.upsert_fund(FundIn(legal_name=f"NoDomain Fund {run_suffix} LP"))
    cleanup.append(("funds", str(fund.id)))

    with open_run("apollo_contact_resolver", sources=make_sources(apollo=FakeApollo()), db=db) as ctx:
        result = apollo_contact_resolver.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "partial"
    assert "primary_domain" in result.errors[0]["error"]
