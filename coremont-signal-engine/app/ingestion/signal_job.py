"""Job 4: Signal generation + scoring.

For each manager, assemble facts from filings/vehicles/adviser enrichment, run
the rules engine, persist discrete signals with their reason strings, and cache
the manager-level score/tier/tags for fast ranking.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from .. import scoring, signals as sig
from ..models import Manager, Signal
from .adviser import enrich_manager


def _build_facts(manager: Manager, adviser, today: dt.date) -> tuple[scoring.ManagerFacts, str | None, str]:
    vehicles = manager.vehicles
    # Text corpus for strategy matching: manager + vehicle names + adviser summary.
    text_parts = [manager.legal_name, manager.normalized_name]
    text_parts += [v.legal_name for v in vehicles]
    if adviser.business_summary:
        text_parts.append(adviser.business_summary)
    text = " \n ".join(p for p in text_parts if p)

    filings = [
        scoring.FilingFacts(
            filing_date=f.filing_date,
            first_sale_date=f.first_sale_date,
            is_amendment=(f.filing_subtype == "amendment"),
            amount_sold=f.amount_sold,
            offering_amount=f.offering_amount,
        )
        for f in manager.filings
    ]

    has_master = any(v.is_master for v in vehicles)
    has_feeder = any(v.is_feeder for v in vehicles)
    has_offshore = any(v.is_offshore for v in vehicles)

    facts = scoring.ManagerFacts(
        text=text,
        filings=filings,
        vehicle_count=len(vehicles),
        has_master=has_master,
        has_feeder=has_feeder,
        has_offshore=has_offshore,
        adviser_known=adviser.adviser_known,
        adviser_fund_count=adviser.adviser_fund_count,
        identity_resolved=bool(manager.normalized_name),
        has_known_buyers=len(manager.contacts) > 0,
        has_outreach_path=bool(manager.website or manager.hq_city or manager.hq_state),
    )

    # Newest vehicle by launch estimate, for the reason string.
    newest = None
    dated = [v for v in vehicles if v.launch_date_est]
    if dated:
        newest = max(dated, key=lambda v: v.launch_date_est).legal_name
    elif vehicles:
        newest = vehicles[-1].legal_name

    return facts, newest, text


def generate_for_manager(session: Session, manager: Manager, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    adviser = enrich_manager(session, manager)
    facts, newest_vehicle, _text = _build_facts(manager, adviser, today)

    breakdown = scoring.score_manager(facts, today=today)
    detected = sig.detect_signals(
        facts,
        display_name=manager.legal_name,
        tags=breakdown.tags,
        newest_vehicle_name=newest_vehicle,
        today=today,
    )

    # Replace this manager's signals with the freshly computed set.
    session.execute(delete(Signal).where(Signal.manager_id == manager.id))
    for d in detected:
        session.add(
            Signal(
                manager_id=manager.id,
                signal_type=d.signal_type,
                signal_date=d.signal_date,
                strength=d.strength,
                reason=d.reason,
                freshness_score=breakdown.event_strength,
                strategy_fit_score=breakdown.strategy_fit,
                complexity_score=breakdown.complexity,
                reachability_score=breakdown.reachability,
                total_score=breakdown.total,
            )
        )

    # Cache manager-level rollups for ranking.
    manager.total_score = breakdown.total
    manager.tier = breakdown.tier
    manager.strategy_tags = breakdown.tags
    signal_dates = [d.signal_date for d in detected if d.signal_date]
    manager.last_signal_date = max(signal_dates) if signal_dates else today

    return {
        "manager_id": manager.id,
        "total_score": breakdown.total,
        "tier": breakdown.tier,
        "signals": len(detected),
    }


def run(session: Session, today: dt.date | None = None) -> dict:
    managers = session.scalars(
        select(Manager).options(
            selectinload(Manager.vehicles),
            selectinload(Manager.filings),
            selectinload(Manager.contacts),
        )
    ).all()
    results = [generate_for_manager(session, m, today=today) for m in managers]
    session.flush()
    tiers = {1: 0, 2: 0, 3: 0, 4: 0}
    for r in results:
        tiers[r["tier"]] += 1
    return {"managers": len(results), "tiers": tiers}
