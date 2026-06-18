"""FastAPI web app — the four pipeline-generation screens.

  /                Ranked managers (home)
  /managers/{id}   Manager detail + "Why Coremont now?"
  /filings         Filing explorer
  /export          Export queue + CSV download / CRM push
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import config, personas, signals as sig
from ..db import get_session, init_db
from ..ingestion import export_job
from ..models import Filing, FundVehicle, Manager, Signal
from ..scoring import TIER_1_MIN, TIER_2_MIN, TIER_3_MIN

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

TIER_LABELS = {1: "Tier 1", 2: "Tier 2", 3: "Tier 3", 4: "Tier 4"}

app = FastAPI(title="Coremont Signal Engine")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --- Jinja helpers -----------------------------------------------------------
def _money(v) -> str:
    if not v:
        return "—"
    v = float(v)
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


templates.env.filters["money"] = _money
templates.env.globals["tier_labels"] = TIER_LABELS
templates.env.globals["signal_labels"] = sig.LABELS
templates.env.globals["data_source"] = config.get_data_source


# --- Ranked managers (home) --------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def ranked_managers(
    request: Request,
    session: Session = Depends(get_session),
    tier: int | None = Query(None),
    strategy: str | None = Query(None),
    signal_type: str | None = Query(None),
    state: str | None = Query(None),
    since_days: int | None = Query(None),
    q: str | None = Query(None),
):
    stmt = (
        select(Manager)
        .options(selectinload(Manager.signals), selectinload(Manager.vehicles))
        .order_by(Manager.total_score.desc(), Manager.last_signal_date.desc())
    )
    if tier:
        stmt = stmt.where(Manager.tier == tier)
    if state:
        stmt = stmt.where(Manager.hq_state == state)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(func.lower(Manager.legal_name).like(like))
    if since_days:
        cutoff = dt.date.today() - dt.timedelta(days=since_days)
        stmt = stmt.where(Manager.last_signal_date >= cutoff)
    if signal_type:
        stmt = stmt.where(
            Manager.id.in_(select(Signal.manager_id).where(Signal.signal_type == signal_type))
        )

    managers = session.scalars(stmt).all()
    if strategy:
        managers = [m for m in managers if strategy in (m.strategy_tags or [])]

    # Filter option values.
    all_tags = sorted(
        {t for m in session.scalars(select(Manager)).all() for t in (m.strategy_tags or [])}
    )
    all_states = [
        s for s in session.scalars(select(distinct(Manager.hq_state))).all() if s
    ]

    counts = {t: session.scalar(select(func.count()).select_from(Manager).where(Manager.tier == t)) for t in (1, 2, 3, 4)}

    return templates.TemplateResponse(
        request,
        "ranked.html",
        {
            "managers": managers,
            "all_tags": all_tags,
            "all_states": all_states,
            "all_signal_types": list(sig.LABELS.keys()),
            "counts": counts,
            "filters": {
                "tier": tier,
                "strategy": strategy,
                "signal_type": signal_type,
                "state": state,
                "since_days": since_days,
                "q": q,
            },
        },
    )


# --- Manager detail ----------------------------------------------------------
@app.get("/managers/{manager_id}", response_class=HTMLResponse)
def manager_detail(manager_id: int, request: Request, session: Session = Depends(get_session)):
    manager = session.get(
        Manager,
        manager_id,
        options=[
            selectinload(Manager.vehicles),
            selectinload(Manager.filings).selectinload(Filing.vehicle),
            selectinload(Manager.signals),
            selectinload(Manager.contacts),
            selectinload(Manager.research_notes),
        ],
    )
    if manager is None:
        return HTMLResponse("Manager not found", status_code=404)

    # Reconstruct DetectedSignal-likes for the narrative helper.
    detected = [
        sig.DetectedSignal(s.signal_type, s.signal_date, s.strength, s.reason)
        for s in manager.signals
    ]
    why = sig.why_coremont_now(detected)
    breakdown = manager.signals[0] if manager.signals else None
    timeline = sorted(
        manager.filings, key=lambda f: f.filing_date or dt.date.min, reverse=True
    )
    persona_order = [personas.label_for(p) for p in personas.DEFAULT_PERSONA_ORDER]

    return templates.TemplateResponse(
        request,
        "manager.html",
        {
            "manager": manager,
            "why": why,
            "breakdown": breakdown,
            "timeline": timeline,
            "persona_order": persona_order,
        },
    )


# --- Filing explorer ---------------------------------------------------------
@app.get("/filings", response_class=HTMLResponse)
def filing_explorer(
    request: Request,
    session: Session = Depends(get_session),
    form: str | None = Query(None),
    subtype: str | None = Query(None),
    min_raise: float | None = Query(None),
    structure: str | None = Query(None),
):
    stmt = (
        select(Filing)
        .options(selectinload(Filing.manager), selectinload(Filing.vehicle))
        .order_by(Filing.filing_date.desc())
        .limit(500)
    )
    if form:
        stmt = stmt.where(Filing.filing_type == form)
    if subtype:
        stmt = stmt.where(Filing.filing_subtype == subtype)
    if min_raise:
        stmt = stmt.where(
            or_(Filing.amount_sold >= min_raise, Filing.offering_amount >= min_raise)
        )
    filings = session.scalars(stmt).all()
    if structure:
        filings = [
            f for f in filings if f.vehicle and f.vehicle.vehicle_type == structure
        ]

    structures = [
        s for s in session.scalars(select(distinct(FundVehicle.vehicle_type))).all() if s
    ]
    return templates.TemplateResponse(
        request,
        "filings.html",
        {
            "filings": filings,
            "structures": structures,
            "filters": {"form": form, "subtype": subtype, "min_raise": min_raise, "structure": structure},
        },
    )


# --- Export queue ------------------------------------------------------------
@app.get("/export", response_class=HTMLResponse)
def export_queue(
    request: Request,
    session: Session = Depends(get_session),
    min_tier: int = Query(2),
):
    rows = export_job.build_rows(session, min_tier=min_tier)
    return templates.TemplateResponse(
        request,
        "export.html",
        {"rows": rows, "min_tier": min_tier, "hubspot": False},
    )


@app.get("/export.csv", response_class=PlainTextResponse)
def export_csv(session: Session = Depends(get_session), min_tier: int = Query(2)):
    rows = export_job.build_rows(session, min_tier=min_tier)
    csv_text = export_job.to_csv(rows)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=coremont_export.csv"},
    )


@app.post("/export/hubspot")
def export_hubspot(min_tier: int = Form(2), session: Session = Depends(get_session)):
    rows = export_job.build_rows(session, min_tier=min_tier)
    export_job.push_to_hubspot(rows)
    return RedirectResponse(url=f"/export?min_tier={min_tier}", status_code=303)
