"""Repositories: the only layer that touches the Supabase client.

Orchestration code imports a repo and calls typed methods; it never builds
PostgREST queries itself.
"""

from gtm.db.repositories.funds import FundsRepo
from gtm.db.repositories.outreach import OutreachRepo
from gtm.db.repositories.people import PeopleRepo
from gtm.db.repositories.runs import RunsRepo
from gtm.db.repositories.signals import SignalsRepo

__all__ = ["FundsRepo", "OutreachRepo", "PeopleRepo", "RunsRepo", "SignalsRepo"]
