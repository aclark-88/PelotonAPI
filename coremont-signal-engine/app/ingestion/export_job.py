"""Job 5: CRM export.

Pushes Tier 1 / Tier 2 managers to a CSV export queue (always) and to HubSpot
when ``HUBSPOT_TOKEN`` is configured. Each row carries score, reason, strategy
tags, freshness, and a suggested buyer persona so a rep can act immediately.
"""
from __future__ import annotations

import csv
import datetime as dt
import io

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import config, personas, signals as sig
from ..models import Manager, Signal

EXPORT_COLUMNS = [
    "manager",
    "tier",
    "total_score",
    "event_strength",
    "strategy_fit",
    "complexity",
    "reachability",
    "strategy_tags",
    "last_signal_date",
    "hq",
    "suggested_persona",
    "reason",
]


def _suggested_persona(manager: Manager) -> str:
    for c in manager.contacts:
        if c.persona:
            return personas.label_for(c.persona)
    return personas.label_for(personas.DEFAULT_PERSONA_ORDER[0])


def _manager_reason(manager: Manager) -> str:
    """Best available reason string for the manager (longest = most informative)."""
    if not manager.signals:
        return ""
    return max((s.reason for s in manager.signals), key=len)


def build_rows(session: Session, min_tier: int = 2) -> list[dict]:
    managers = session.scalars(
        select(Manager)
        .where(Manager.tier <= min_tier)
        .order_by(Manager.total_score.desc())
        .options(selectinload(Manager.signals), selectinload(Manager.contacts))
    ).all()

    rows = []
    for m in managers:
        first = m.signals[0] if m.signals else None
        rows.append(
            {
                "manager": m.legal_name,
                "tier": m.tier,
                "total_score": round(m.total_score, 1),
                "event_strength": round(first.freshness_score, 1) if first else 0,
                "strategy_fit": round(first.strategy_fit_score, 1) if first else 0,
                "complexity": round(first.complexity_score, 1) if first else 0,
                "reachability": round(first.reachability_score, 1) if first else 0,
                "strategy_tags": "|".join(m.strategy_tags or []),
                "last_signal_date": m.last_signal_date.isoformat() if m.last_signal_date else "",
                "hq": ", ".join(p for p in (m.hq_city, m.hq_state) if p),
                "suggested_persona": _suggested_persona(m),
                "reason": _manager_reason(m),
            }
        )
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_csv(session: Session, min_tier: int = 2) -> str:
    """Write the export queue to a timestamped CSV; return the file path."""
    rows = build_rows(session, min_tier=min_tier)
    path = config.export_dir() / f"coremont_export_{dt.date.today().isoformat()}.csv"
    path.write_text(to_csv(rows))
    return str(path)


def push_to_hubspot(rows: list[dict], token: str | None = None) -> dict:
    """Upsert export rows as HubSpot companies. No-op (CSV only) without a token."""
    token = token or config.hubspot_token()
    if not token:
        return {"pushed": 0, "skipped": len(rows), "reason": "no HUBSPOT_TOKEN"}

    pushed = 0
    with httpx.Client(
        base_url="https://api.hubapi.com",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=20.0,
    ) as client:
        for r in rows:
            payload = {
                "properties": {
                    "name": r["manager"],
                    "coremont_tier": str(r["tier"]),
                    "coremont_score": str(r["total_score"]),
                    "coremont_strategy_tags": r["strategy_tags"],
                    "coremont_reason": r["reason"],
                    "coremont_persona": r["suggested_persona"],
                }
            }
            resp = client.post("/crm/v3/objects/companies", json=payload)
            if resp.status_code < 300:
                pushed += 1
    return {"pushed": pushed, "skipped": len(rows) - pushed}
