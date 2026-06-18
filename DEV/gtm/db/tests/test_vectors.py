"""Vector similarity smoke test via match_fund_summaries."""

from __future__ import annotations

import math
import random

from gtm.db.repositories.funds import FundsRepo
from gtm.models.funds import FundIn


def _unit_vector(seed: int, dim: int = 1536) -> list[float]:
    rng = random.Random(seed)
    v = [rng.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def test_vector_similarity_smoke(db, run_suffix, cleanup):
    repo = FundsRepo(db)

    fund = repo.upsert_fund(
        FundIn(legal_name=f"Vector Test Fund {run_suffix} LP", strategies=["quant"])
    )
    cleanup.append(("funds", str(fund.id)))

    embedding = _unit_vector(seed=42)
    summary = repo.upsert_summary(
        fund_id=fund.id,
        summary_text=f"Quant fund test summary {run_suffix}",
        embedding=embedding,
        embedding_model="test-model",
    )
    cleanup.append(("fund_summaries", summary["id"]))

    matches = repo.match_summaries(query_embedding=embedding, match_count=5)

    ours = [m for m in matches if m["fund_id"] == str(fund.id)]
    assert ours, "the inserted summary must come back for its own embedding"
    assert ours[0]["similarity"] > 0.99, "self-similarity should be ~1.0 (cosine)"
