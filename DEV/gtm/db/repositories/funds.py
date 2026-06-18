"""Fund repository: identity-aware upsert, scoring, lookup."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from gtm.db.repositories._base import BaseRepo
from gtm.models.funds import Fund, FundIn
from gtm.models.signals import ScoringRun, ScoringRunIn


class FundsRepo(BaseRepo):
    TABLE = "funds"

    def upsert_fund(self, fund: FundIn, source_run_id: UUID | None = None) -> Fund:
        """Insert or update by identity precedence: crd > lei > primary_domain >
        case-insensitive legal_name. Updates only the fields present on the
        input (None fields never clobber existing values)."""
        existing = self._find_existing(fund)
        payload = self._dump(fund, source_run_id=source_run_id)
        if existing:
            # metadata merges; never let an upsert wipe accumulated context
            payload["metadata"] = {
                **(existing.get("metadata") or {}),
                **(payload.get("metadata") or {}),
            }
            resp = (
                self.client.table(self.TABLE)
                .update(payload)
                .eq("id", existing["id"])
                .execute()
            )
        else:
            resp = self.client.table(self.TABLE).insert(payload).execute()
        return Fund.model_validate(resp.data[0])

    def _find_existing(self, fund: FundIn) -> dict[str, Any] | None:
        for col, val in (
            ("crd", fund.crd),
            ("lei", fund.lei),
            ("cik", fund.cik),
            ("primary_domain", fund.primary_domain),
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
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .ilike("legal_name", fund.legal_name)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def find_by_cik(self, cik: str) -> Fund | None:
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("cik", cik)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        return Fund.model_validate(resp.data[0]) if resp.data else None

    def update_metadata(self, fund_id: UUID, patch: dict[str, Any]) -> Fund:
        """Shallow-merge patch into funds.metadata (read-modify-write)."""
        current = self.get(fund_id)
        if current is None:
            raise ValueError(f"fund {fund_id} not found")
        merged = {**current.metadata, **patch}
        resp = (
            self.client.table(self.TABLE)
            .update({"metadata": merged})
            .eq("id", str(fund_id))
            .execute()
        )
        return Fund.model_validate(resp.data[0])

    def get(self, fund_id: UUID) -> Fund | None:
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("id", str(fund_id))
            .limit(1)
            .execute()
        )
        return Fund.model_validate(resp.data[0]) if resp.data else None

    def list_tam(self, min_fit_score: int = 60, limit: int = 100) -> list[Fund]:
        """TAM accounts: tier 1-3 OR fit_score >= threshold, live, with a domain
        (domain required because Apollo searches key on it)."""
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .or_(f"tier.lte.3,fit_score.gte.{min_fit_score}")
            .not_.is_("primary_domain", "null")
            .is_("deleted_at", "null")
            .order("fit_score", desc=True)
            .limit(limit)
            .execute()
        )
        return [Fund.model_validate(r) for r in resp.data]

    def search_by_name_fuzzy(self, name: str, limit: int = 5) -> list[Fund]:
        """Case-insensitive contains-match on legal/common name (pg_trgm
        indexes back this server-side)."""
        pattern = f"%{name}%"
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .or_(f"legal_name.ilike.{pattern},common_name.ilike.{pattern}")
            .is_("deleted_at", "null")
            .limit(limit)
            .execute()
        )
        return [Fund.model_validate(r) for r in resp.data]

    def list_by_tier(self, tier: int, limit: int = 100) -> list[Fund]:
        resp = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("tier", tier)
            .is_("deleted_at", "null")
            .order("fit_score", desc=True)
            .limit(limit)
            .execute()
        )
        return [Fund.model_validate(r) for r in resp.data]

    def record_fit_score(
        self,
        fund_id: UUID,
        score: int,
        model_version: str,
        reasoning: str | None = None,
        inputs: dict[str, Any] | None = None,
        tier: int | None = None,
        source_run_id: UUID | None = None,
    ) -> ScoringRun:
        """Append to scoring_runs (the audit trail) and refresh the cache on
        funds. Never overwrites history."""
        run_payload = self._dump(
            ScoringRunIn(
                entity_type="fund",
                entity_id=fund_id,
                model_version=model_version,
                score=score,
                reasoning=reasoning,
                inputs=inputs or {},
            ),
            source_run_id=source_run_id,
        )
        run_resp = self.client.table("scoring_runs").insert(run_payload).execute()

        cache: dict[str, Any] = {
            "fit_score": score,
            "fit_score_updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if tier is not None:
            cache["tier"] = tier
        self.client.table(self.TABLE).update(cache).eq("id", str(fund_id)).execute()
        return ScoringRun.model_validate(run_resp.data[0])

    def soft_delete(self, fund_id: UUID) -> None:
        self.client.table(self.TABLE).update(
            {"deleted_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", str(fund_id)).execute()

    def upsert_summary(
        self,
        fund_id: UUID,
        summary_text: str,
        embedding: list[float],
        embedding_model: str,
        source_run_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Soft-deletes prior live summaries for the fund, inserts the new one."""
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("fund_summaries").update({"deleted_at": now}).eq(
            "fund_id", str(fund_id)
        ).is_("deleted_at", "null").execute()
        payload: dict[str, Any] = {
            "fund_id": str(fund_id),
            "summary_text": summary_text,
            "embedding": embedding,
            "embedding_model": embedding_model,
        }
        if source_run_id:
            payload["source_run_id"] = str(source_run_id)
        resp = self.client.table("fund_summaries").insert(payload).execute()
        return resp.data[0]

    def match_summaries(
        self, query_embedding: list[float], match_count: int = 10
    ) -> list[dict[str, Any]]:
        """Semantic fund search via the match_fund_summaries SQL function."""
        resp = self.client.rpc(
            "match_fund_summaries",
            {"query_embedding": query_embedding, "match_count": match_count},
        ).execute()
        return resp.data or []
