"""Buyer-contact enrichment via Apollo.io (Job 3b).

Form D names the *fund vehicle*, not a person. This module derives the adviser
firm from the vehicle name, looks up likely Clarion buyers (COO, operations,
risk, finance, treasury) at that firm via Apollo's People Search API, and writes
them to the ``contacts`` table so every prospect row has someone to actually
reach out to.

Gated on ``APOLLO_API_KEY`` — a no-op when unset, so the pipeline degrades
gracefully (and runs offline / in tests) without it.
"""
from __future__ import annotations

import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, personas
from ..models import Contact, Manager

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"

# Titles we ask Apollo for, grouped to our personas (priority order).
BUYER_TITLES = [
    "Chief Operating Officer",
    "Head of Operations",
    "Chief Risk Officer",
    "Chief Financial Officer",
    "Treasurer",
    "Head of Treasury",
]

# Tokens that signal the end of the firm brand and the start of strategy/vehicle
# wording — we keep only the leading brand for the org lookup.
_FIRM_STOP = {
    "fund", "funds", "master", "feeder", "offshore", "onshore", "parallel",
    "global", "multi", "multistrategy", "multi-strategy", "macro", "fixed",
    "income", "credit", "relative", "value", "structured", "opportunistic",
    "opportunities", "asset", "backed", "mortgage", "securitized", "rates",
    "volatility", "arbitrage", "event", "driven", "special", "situations",
    "alpha", "trading", "partners", "lp", "l.p.", "llc", "ltd", "ltd.",
    "limited", "scsp", "raif", "i", "ii", "iii", "iv", "v", "vi", "spc",
}


def firm_name_guess(legal_name: str) -> str:
    """Best-effort adviser-firm brand from a fund-vehicle legal name.

    "AQR Multi-Strategy Fund XIX, L.P."        -> "AQR"
    "Kirkoswald Global Macro Fund Ltd"          -> "Kirkoswald"
    "Garda Fixed Income Relative Value ... Ltd" -> "Garda"
    """
    name = legal_name.replace("SAMPLE —", "").strip()
    tokens = re.split(r"[\s,]+", name)
    brand: list[str] = []
    for tok in tokens:
        clean = tok.strip(".,").lower()
        if not clean:
            continue
        if clean in _FIRM_STOP and brand:  # stop once into strategy/vehicle words
            break
        brand.append(tok.strip(","))
        if len(brand) >= 3:
            break
    return " ".join(brand) or name


class ApolloClient:
    def __init__(self, api_key: str | None = None, timeout: float = 25.0):
        self.api_key = api_key or config.apollo_api_key()
        self._client = httpx.Client(
            base_url="https://api.apollo.io",
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": self.api_key or "",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApolloClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def parse_people(payload: dict) -> list[dict]:
        """Extract (name, title, email, linkedin) from an Apollo search response."""
        people = payload.get("people", []) or payload.get("contacts", [])
        out = []
        for p in people:
            name = (p.get("name") or
                    " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)).strip()
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "title": p.get("title") or "",
                    "email": p.get("email"),  # often masked unless enriched
                    "linkedin": p.get("linkedin_url"),
                }
            )
        return out

    def find_buyers(self, firm: str, per_page: int = 5) -> list[dict]:
        body = {
            "q_organization_name": firm,
            "person_titles": BUYER_TITLES,
            "page": 1,
            "per_page": per_page,
        }
        resp = self._client.post("/api/v1/mixed_people/search", json=body)
        resp.raise_for_status()
        return self.parse_people(resp.json())


def enrich_manager(session: Session, manager: Manager, client: ApolloClient) -> int:
    """Look up and store buyer contacts for one manager. Returns count added."""
    if manager.contacts:  # already enriched
        return 0
    firm = firm_name_guess(manager.legal_name)
    try:
        people = client.find_buyers(firm)
    except httpx.HTTPError:
        return 0
    added = 0
    for p in people:
        session.add(
            Contact(
                manager_id=manager.id,
                full_name=p["name"],
                title=p["title"],
                persona=personas.classify_title(p["title"]),
                email=p.get("email"),
                source_url=p.get("linkedin"),
            )
        )
        added += 1
    return added


def enrich_top_managers(session: Session, max_tier: int = 2) -> dict:
    """Enrich Tier <= max_tier managers with buyer contacts (bounded API use)."""
    if not config.apollo_api_key():
        return {"enriched": 0, "reason": "no APOLLO_API_KEY"}
    managers = session.scalars(
        select(Manager).where(Manager.tier <= max_tier)
    ).all()
    total = 0
    with ApolloClient() as client:
        for m in managers:
            total += enrich_manager(session, m, client)
    session.flush()
    return {"enriched": total, "managers": len(managers)}
