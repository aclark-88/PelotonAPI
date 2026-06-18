"""Signal recording and dedupe_key collision behavior."""

from __future__ import annotations

from datetime import datetime, timezone

from gtm.db.repositories.funds import FundsRepo
from gtm.db.repositories.signals import SignalsRepo, dedupe_key
from gtm.models.funds import FundIn
from gtm.models.signals import SignalIn


def test_signal_dedupe_collision_returns_existing(db, run_suffix, cleanup):
    funds = FundsRepo(db)
    signals = SignalsRepo(db)

    fund = funds.upsert_fund(
        FundIn(legal_name=f"Signal Test Fund {run_suffix} LP", strategies=["credit"])
    )
    cleanup.append(("funds", str(fund.id)))

    signal_in = SignalIn(
        signal_type="new_fund_launch",
        source="edgar_tools",
        source_record_id=f"accession-{run_suffix}",
        observed_at=datetime.now(timezone.utc),
        fund_id=fund.id,
        payload={"form": "D", "test": True},
    )

    first = signals.record_signal(signal_in)
    cleanup.append(("signals", str(first.id)))
    second = signals.record_signal(signal_in)

    assert second.id == first.id, "identical source record must dedupe to one row"

    expected_key = dedupe_key("edgar_tools", f"accession-{run_suffix}", "new_fund_launch")
    assert first.dedupe_key == expected_key, "client-side md5 must mirror the DB"

    count = (
        db.table("signals")
        .select("id", count="exact")
        .eq("dedupe_key", expected_key)
        .execute()
    )
    assert count.count == 1
