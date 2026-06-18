"""Signals repository: idempotent recording keyed on dedupe_key."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError

from gtm.db.repositories._base import BaseRepo
from gtm.models.common import Urgency
from gtm.models.signals import Signal, SignalIn


def dedupe_key(source: str, source_record_id: str, signal_type: str) -> str:
    """Must mirror the generated column:
    md5(source || ':' || source_record_id || ':' || signal_type)."""
    raw = f"{source}:{source_record_id}:{signal_type}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class SignalsRepo(BaseRepo):
    TABLE = "signals"

    _type_defaults: dict[str, dict] = {}

    def type_defaults(self, key: str) -> dict:
        """Default urgency / weight for a signal type, cached per process.
        signal_types is the single source of truth for urgency defaults."""
        if key not in self._type_defaults:
            resp = (
                self.client.table("signal_types")
                .select("key, default_urgency, default_score_weight")
                .eq("key", key)
                .limit(1)
                .execute()
            )
            self._type_defaults[key] = (
                resp.data[0]
                if resp.data
                else {"key": key, "default_urgency": "this_month", "default_score_weight": 1.0}
            )
        return self._type_defaults[key]

    def record_signal(self, signal: SignalIn, source_run_id: UUID | None = None) -> Signal:
        """Insert; on dedupe collision return the existing signal. Re-ingesting
        the same source record is always safe."""
        payload = self._dump(signal, source_run_id=source_run_id)
        try:
            resp = self.client.table(self.TABLE).insert(payload).execute()
            return Signal.model_validate(resp.data[0])
        except APIError as err:
            if not self._is_unique_violation(err):
                raise
            key = dedupe_key(signal.source, signal.source_record_id, signal.signal_type)
            resp = (
                self.client.table(self.TABLE)
                .select("*")
                .eq("dedupe_key", key)
                .single()
                .execute()
            )
            return Signal.model_validate(resp.data)

    def get(self, signal_id: UUID) -> Signal | None:
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("id", str(signal_id))
            .limit(1)
            .execute()
        )
        return Signal.model_validate(resp.data[0]) if resp.data else None

    def list_active(
        self,
        urgencies: tuple[Urgency, ...] = (Urgency.immediate, Urgency.this_week),
        signal_type: str | None = None,
        limit: int = 100,
    ) -> list[Signal]:
        """Live signals (not superseded, not deleted) at the given urgencies."""
        q = (
            self.client.table(self.TABLE)
            .select("*")
            .in_("urgency", [u.value for u in urgencies])
            .is_("superseded_by", "null")
            .is_("deleted_at", "null")
            .order("observed_at", desc=True)
            .limit(limit)
        )
        if signal_type:
            q = q.eq("signal_type", signal_type)
        return [Signal.model_validate(r) for r in q.execute().data]

    def update_urgency(
        self, signal_id: UUID, urgency: Urgency, metadata_patch: dict[str, Any] | None = None
    ) -> Signal:
        """Apply an urgency override (e.g. champion_relocation) post-emission."""
        current = self.get(signal_id)
        if current is None:
            raise ValueError(f"signal {signal_id} not found")
        payload: dict[str, Any] = {"urgency": urgency.value}
        if metadata_patch:
            payload["metadata"] = {**current.metadata, **metadata_patch}
        resp = (
            self.client.table(self.TABLE)
            .update(payload)
            .eq("id", str(signal_id))
            .execute()
        )
        return Signal.model_validate(resp.data[0])

    def supersede(self, old_signal_id: UUID, new_signal_id: UUID) -> None:
        self.client.table(self.TABLE).update(
            {"superseded_by": str(new_signal_id)}
        ).eq("id", str(old_signal_id)).execute()

    def list_for_fund(self, fund_id: UUID, limit: int = 100) -> list[Signal]:
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("fund_id", str(fund_id))
            .is_("deleted_at", "null")
            .order("observed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [Signal.model_validate(r) for r in resp.data]
