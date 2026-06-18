"""Fund upsert and identity dedupe."""

from __future__ import annotations

from gtm.db.repositories.funds import FundsRepo
from gtm.models.funds import FundIn


def test_fund_upsert_dedupes_on_crd(db, run_suffix, cleanup):
    repo = FundsRepo(db)
    crd = f"TEST-{run_suffix}"

    first = repo.upsert_fund(
        FundIn(
            legal_name=f"Test Macro Partners {run_suffix} LP",
            crd=crd,
            strategies=["macro"],
            aum_usd_millions=750,
        )
    )
    cleanup.append(("funds", str(first.id)))

    second = repo.upsert_fund(
        FundIn(
            legal_name=f"Test Macro Partners {run_suffix} LP",
            crd=crd,
            strategies=["macro", "relative_value"],
            aum_usd_millions=900,
        )
    )

    assert second.id == first.id, "same CRD must update, not insert"
    assert second.aum_usd_millions == 900
    assert "relative_value" in second.strategies
    assert second.aum_band == "300_to_1b", "generated aum_band should reflect AUM"


def test_fund_upsert_rejects_unknown_strategy(db, run_suffix, cleanup):
    import pytest
    from postgrest.exceptions import APIError

    repo = FundsRepo(db)
    with pytest.raises(APIError):
        repo.upsert_fund(
            FundIn(
                legal_name=f"Bad Strategy Fund {run_suffix} LP",
                strategies=["interpretive_dance"],
            )
        )
