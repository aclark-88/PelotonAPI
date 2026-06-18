"""Job 1 + Job 2: Form D ingestion and entity normalization (persistence).

Takes parsed ``FormDRecord`` objects (from the live SEC client or seed data) and
upserts them into managers / fund_vehicles / filings, collapsing each manager's
many legal entities onto one platform via ``normalization.manager_key``.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import normalization
from ..models import Filing, FundVehicle, Manager
from .edgar_client import FormDRecord


def _get_or_create_manager(session: Session, record: FormDRecord) -> Manager:
    key = normalization.manager_key(record.issuer_name)
    mgr = session.scalar(select(Manager).where(Manager.normalized_name == key))
    if mgr is None:
        mgr = Manager(
            legal_name=record.issuer_name,
            normalized_name=key,
            cik=record.cik or None,
            hq_city=record.hq_city,
            hq_state=record.hq_state,
            country=record.jurisdiction,
            strategy_tags=[],
            status="active",
        )
        session.add(mgr)
        session.flush()
    else:
        # Backfill location/CIK if we learn it from a newer filing.
        mgr.cik = mgr.cik or (record.cik or None)
        mgr.hq_city = mgr.hq_city or record.hq_city
        mgr.hq_state = mgr.hq_state or record.hq_state
    return mgr


def _get_or_create_vehicle(session: Session, mgr: Manager, record: FormDRecord) -> FundVehicle:
    vnorm = normalization.normalize_entity_name(record.issuer_name)
    veh = session.scalar(
        select(FundVehicle).where(
            FundVehicle.manager_id == mgr.id, FundVehicle.normalized_name == vnorm
        )
    )
    structure = normalization.classify_vehicle(record.issuer_name, record.jurisdiction)
    if veh is None:
        veh = FundVehicle(
            manager_id=mgr.id,
            legal_name=record.issuer_name,
            normalized_name=vnorm,
            vehicle_type=structure.vehicle_type,
            domicile=record.jurisdiction,
            is_master=structure.is_master,
            is_feeder=structure.is_feeder,
            is_offshore=structure.is_offshore,
            launch_date_est=record.first_sale_date or record.filing_date,
            status="active",
        )
        session.add(veh)
        session.flush()
    return veh


def persist_records(session: Session, records: list[FormDRecord]) -> dict:
    """Upsert a batch of Form D records. Returns counts for logging."""
    new_filings = 0
    seen_managers: set[int] = set()
    for record in records:
        if not record.issuer_name:
            continue
        # Idempotency: skip filings we already stored.
        existing = session.scalar(
            select(Filing).where(Filing.sec_accession_no == record.accession_no)
        ) if record.accession_no else None
        if existing is not None:
            seen_managers.add(existing.manager_id)
            continue

        mgr = _get_or_create_manager(session, record)
        veh = _get_or_create_vehicle(session, mgr, record)
        seen_managers.add(mgr.id)

        filing = Filing(
            manager_id=mgr.id,
            fund_vehicle_id=veh.id,
            filing_type="D",
            filing_subtype="amendment" if record.is_amendment else "new",
            sec_accession_no=record.accession_no or f"seed-{mgr.id}-{veh.id}-{new_filings}",
            filing_date=record.filing_date or dt.date.today(),
            first_sale_date=record.first_sale_date,
            offering_amount=record.offering_amount,
            amount_sold=record.amount_sold,
            remaining_amount=record.remaining_amount,
            exemption_claimed=", ".join(record.exemptions) or None,
            raw_payload_json=record.raw_payload,
        )
        session.add(filing)
        new_filings += 1

    session.flush()
    return {"records": len(records), "new_filings": new_filings, "managers_touched": len(seen_managers)}
