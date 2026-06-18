"""Source-run lifecycle and raw payload archival."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError

from gtm.db.repositories._base import BaseRepo
from gtm.models.common import RunStatus
from gtm.models.runs import RawPayload, SourceRun


def payload_hash(response: Any) -> str:
    """sha256 over canonical (sorted-keys) JSON of the response."""
    canonical = json.dumps(response, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RunsRepo(BaseRepo):
    def start_run(self, skill_name: str, metadata: dict[str, Any] | None = None) -> SourceRun:
        resp = (
            self.client.table("source_runs")
            .insert({"skill_name": skill_name, "metadata": metadata or {}})
            .execute()
        )
        return SourceRun.model_validate(resp.data[0])

    def finish_run(
        self,
        run_id: UUID,
        status: RunStatus,
        records_processed: int = 0,
        records_inserted: int = 0,
        records_updated: int = 0,
        errors: list[dict[str, Any]] | None = None,
    ) -> SourceRun:
        resp = (
            self.client.table("source_runs")
            .update(
                {
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "status": status.value,
                    "records_processed": records_processed,
                    "records_inserted": records_inserted,
                    "records_updated": records_updated,
                    "errors": errors or [],
                }
            )
            .eq("id", str(run_id))
            .execute()
        )
        return SourceRun.model_validate(resp.data[0])

    def archive_payload(
        self,
        source: str,
        response: Any,
        request: dict[str, Any] | None = None,
        source_run_id: UUID | None = None,
    ) -> RawPayload:
        """Append-only archive; identical responses dedupe on payload_hash and
        return the existing row."""
        h = payload_hash(response)
        payload: dict[str, Any] = {
            "source": source,
            "response": response,
            "payload_hash": h,
        }
        if request is not None:
            payload["request"] = request
        if source_run_id is not None:
            payload["source_run_id"] = str(source_run_id)
        try:
            resp = self.client.table("raw_payloads").insert(payload).execute()
            return RawPayload.model_validate(resp.data[0])
        except APIError as err:
            if not self._is_unique_violation(err):
                raise
            resp = (
                self.client.table("raw_payloads")
                .select("*")
                .eq("payload_hash", h)
                .single()
                .execute()
            )
            return RawPayload.model_validate(resp.data)
