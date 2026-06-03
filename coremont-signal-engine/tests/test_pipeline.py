"""End-to-end tests: seed ingestion → normalization → scoring → signals → export."""
import datetime as dt

from sqlalchemy import select

from app.ingestion import export_job, formd, signal_job
from app.ingestion.edgar_client import parse_form_d_xml
from app.ingestion.pipeline import load_seed_records
from app.models import Filing, FundVehicle, Manager, Signal


def test_form_d_xml_parser_against_real_shaped_doc():
    import pathlib

    xml = (pathlib.Path(__file__).resolve().parent.parent / "seed" / "sample_primary_doc.xml").read_text()
    rec = parse_form_d_xml(xml, accession_no="0001900001-26-000101", date_filed="2026-05-20")
    assert rec.issuer_name == "Meridian Structured Credit Master Fund LP"
    assert rec.offering_amount == 750_000_000
    assert rec.amount_sold == 180_000_000
    assert rec.remaining_amount == 570_000_000
    assert rec.first_sale_date == dt.date(2026, 5, 15)
    assert rec.is_amendment is False
    assert "06b" in rec.exemptions
    assert rec.related_persons and rec.related_persons[0]["name"] == "Daniel Hartman"


def test_seed_ingestion_collapses_vehicles_under_one_manager(session):
    records = load_seed_records()
    formd.persist_records(session, records)
    session.flush()

    # Meridian master + offshore should roll up to a single manager with 2 vehicles.
    meridian = session.scalar(
        select(Manager).where(Manager.normalized_name == "meridian structured credit")
    )
    assert meridian is not None
    assert len(meridian.vehicles) == 2
    assert any(v.is_offshore for v in meridian.vehicles)
    assert any(v.is_master for v in meridian.vehicles)


def test_ingestion_is_idempotent(session):
    records = load_seed_records()
    formd.persist_records(session, records)
    session.flush()
    first = session.scalar(select(Filing).where(Filing.sec_accession_no.isnot(None)))
    n_filings = len(session.scalars(select(Filing)).all())
    n_managers = len(session.scalars(select(Manager)).all())

    formd.persist_records(session, records)  # re-run
    session.flush()
    assert len(session.scalars(select(Filing)).all()) == n_filings
    assert len(session.scalars(select(Manager)).all()) == n_managers


def test_full_pipeline_scores_and_tiers(session):
    records = load_seed_records()
    formd.persist_records(session, records)
    session.flush()
    today = dt.date(2026, 6, 3)
    signal_job.run(session, today=today)
    session.flush()

    meridian = session.scalar(
        select(Manager).where(Manager.normalized_name == "meridian structured credit")
    )
    # Strong ICP + multi-vehicle offshore/master + fresh raise → Tier 1.
    assert meridian.tier == 1
    assert meridian.total_score >= 75
    assert meridian.strategy_tags  # tags populated
    assert len(meridian.signals) >= 3

    # Venture fund should be low tier (negative strategy terms).
    venture = session.scalar(
        select(Manager).where(Manager.legal_name.like("%Northpath%"))
    )
    assert venture.tier >= 3


def test_export_queue_only_includes_tier1_and_tier2(session):
    records = load_seed_records()
    formd.persist_records(session, records)
    session.flush()
    signal_job.run(session, today=dt.date(2026, 6, 3))
    session.flush()

    rows = export_job.build_rows(session, min_tier=2)
    assert rows, "expected at least one exportable prospect"
    assert all(r["tier"] <= 2 for r in rows)
    csv_text = export_job.to_csv(rows)
    assert "manager" in csv_text.splitlines()[0]
    # Suggested persona present and from our buyer set.
    assert all(r["suggested_persona"] for r in rows)
