"""People repository: upsert, employment history, and the job-change flow."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from gtm.db.repositories._base import BaseRepo
from gtm.models.common import RoleFunction, Seniority
from gtm.models.people import EmploymentHistory, Person, PersonIn
from gtm.models.signals import Signal


class PeopleRepo(BaseRepo):
    TABLE = "people"

    def upsert_person(self, person: PersonIn, source_run_id: UUID | None = None) -> Person:
        """Identity precedence: apollo_id > linkedin_url > email >
        (full_name + current_fund_id).

        Manually verified contacts (metadata.manually_verified=true) are never
        overwritten by automated upserts — the existing row is returned as-is.
        Metadata is merged, never replaced."""
        existing = self._find_existing(person)
        payload = self._dump(person, source_run_id=source_run_id)
        if existing:
            existing_meta = existing.get("metadata") or {}
            if existing_meta.get("manually_verified"):
                return Person.model_validate(existing)
            payload["metadata"] = {**existing_meta, **(payload.get("metadata") or {})}
            resp = (
                self.client.table(self.TABLE)
                .update(payload)
                .eq("id", existing["id"])
                .execute()
            )
        else:
            resp = self.client.table(self.TABLE).insert(payload).execute()
        return Person.model_validate(resp.data[0])

    def _find_existing(self, person: PersonIn) -> dict[str, Any] | None:
        for col, val in (
            ("apollo_id", person.apollo_id),
            ("linkedin_url", person.linkedin_url),
            ("email", person.email),
        ):
            if val:
                resp = (
                    self.client.table(self.TABLE)
                    .select("*")
                    .eq(col, val)
                    .is_("deleted_at", "null")
                    .limit(1)
                    .execute()
                )
                if resp.data:
                    return resp.data[0]
        if person.current_fund_id:
            resp = (
                self.client.table(self.TABLE)
                .select("*")
                .ilike("full_name", person.full_name)
                .eq("current_fund_id", str(person.current_fund_id))
                .is_("deleted_at", "null")
                .limit(1)
                .execute()
            )
            if resp.data:
                return resp.data[0]
        return None

    def get(self, person_id: UUID) -> Person | None:
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("id", str(person_id))
            .limit(1)
            .execute()
        )
        return Person.model_validate(resp.data[0]) if resp.data else None

    def employment_history(self, person_id: UUID) -> list[EmploymentHistory]:
        resp = (
            self.client.table("employment_history")
            .select("*")
            .eq("person_id", str(person_id))
            .is_("deleted_at", "null")
            .order("started_at", desc=True)
            .execute()
        )
        return [EmploymentHistory.model_validate(r) for r in resp.data]

    def observe_job_change(
        self,
        person_id: UUID,
        new_fund_id: UUID,
        new_role: str,
        observed_at: datetime | None = None,
        function: RoleFunction = RoleFunction.unknown,
        seniority: Seniority = Seniority.unknown,
        source: str = "manual",
        source_run_id: UUID | None = None,
    ) -> Signal:
        """Atomic job-change observation via fn_observe_job_change: closes the
        open employment row, inserts the new one, updates people.current_*, and
        emits (or dedupes to) a new_role signal. Returns that signal."""
        params: dict[str, Any] = {
            "p_person_id": str(person_id),
            "p_new_fund_id": str(new_fund_id),
            "p_new_role": new_role,
            "p_function": function.value,
            "p_seniority": seniority.value,
            "p_source": source,
        }
        if observed_at is not None:
            params["p_observed_at"] = observed_at.isoformat()
        if source_run_id is not None:
            params["p_source_run_id"] = str(source_run_id)

        resp = self.client.rpc("fn_observe_job_change", params).execute()
        signal_id = resp.data
        sig = (
            self.client.table("signals")
            .select("*")
            .eq("id", signal_id)
            .single()
            .execute()
        )
        return Signal.model_validate(sig.data)
