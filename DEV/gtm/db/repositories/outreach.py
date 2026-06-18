"""Outreach repository: campaigns, drafts, attempts, replies."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from postgrest.exceptions import APIError

from gtm.db.repositories._base import BaseRepo
from gtm.models.common import OutreachStatus
from gtm.models.outreach import (
    Campaign,
    CampaignIn,
    Draft,
    DraftIn,
    OutreachAttempt,
    OutreachAttemptIn,
    Reply,
    ReplyIn,
)


class DuplicateSendError(Exception):
    """A (person, campaign, step) attempt already exists; the DB blocked it."""


class OutreachRepo(BaseRepo):
    def upsert_campaign(self, campaign: CampaignIn) -> Campaign:
        existing = (
            self.client.table("campaigns")
            .select("*")
            .eq("name", campaign.name)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        payload = self._dump(campaign)
        if existing.data:
            resp = (
                self.client.table("campaigns")
                .update(payload)
                .eq("id", existing.data[0]["id"])
                .execute()
            )
        else:
            resp = self.client.table("campaigns").insert(payload).execute()
        return Campaign.model_validate(resp.data[0])

    def create_draft(self, draft: DraftIn, source_run_id: UUID | None = None) -> Draft:
        payload = self._dump(draft, source_run_id=source_run_id)
        resp = self.client.table("drafts").insert(payload).execute()
        return Draft.model_validate(resp.data[0])

    def approve_draft(self, draft_id: UUID, approved_by: str) -> Draft:
        resp = (
            self.client.table("drafts")
            .update(
                {
                    "approved_by": approved_by,
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", str(draft_id))
            .execute()
        )
        return Draft.model_validate(resp.data[0])

    def record_attempt(
        self, attempt: OutreachAttemptIn, source_run_id: UUID | None = None
    ) -> OutreachAttempt:
        """Raises DuplicateSendError if (person, campaign, step) already exists."""
        payload = self._dump(attempt, source_run_id=source_run_id)
        try:
            resp = self.client.table("outreach_attempts").insert(payload).execute()
        except APIError as err:
            if self._is_unique_violation(err):
                raise DuplicateSendError(
                    f"attempt exists: person={attempt.person_id} "
                    f"campaign={attempt.campaign_id} step={attempt.step_number}"
                ) from err
            raise
        row = resp.data[0]
        if attempt.draft_id:
            self.client.table("drafts").update(
                {"sent_attempt_id": row["id"]}
            ).eq("id", str(attempt.draft_id)).execute()
        return OutreachAttempt.model_validate(row)

    def update_attempt_status(
        self,
        attempt_id: UUID,
        status: OutreachStatus,
        external_id: str | None = None,
        sent_at: datetime | None = None,
    ) -> OutreachAttempt:
        payload: dict = {"status": status.value}
        if external_id is not None:
            payload["external_id"] = external_id
        if sent_at is not None:
            payload["sent_at"] = sent_at.isoformat()
        resp = (
            self.client.table("outreach_attempts")
            .update(payload)
            .eq("id", str(attempt_id))
            .execute()
        )
        return OutreachAttempt.model_validate(resp.data[0])

    def record_reply(self, reply: ReplyIn, source_run_id: UUID | None = None) -> Reply:
        payload = self._dump(reply, source_run_id=source_run_id)
        resp = self.client.table("replies").insert(payload).execute()
        self.client.table("outreach_attempts").update(
            {"status": OutreachStatus.replied.value}
        ).eq("id", str(reply.outreach_attempt_id)).execute()
        return Reply.model_validate(resp.data[0])
