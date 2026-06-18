"""Re-run a skill against the raw payloads a prior run archived.

    py -m gtm.cli.replay <source_run_id>

Why this exists: when a parser changes, replay re-processes the archived API
responses WITHOUT re-hitting the APIs. Idempotency does the rest — unchanged
parsing dedupes to the same rows; changed parsing upserts corrections.

Currently replayable: form_d_sweep, spinout_watcher (their archived payloads
fully reconstruct their inputs). Skills that take per-entity kwargs
(adv_fit_scorer fund_id etc.) re-run trivially by hand and aren't wired here.
"""

from __future__ import annotations

import argparse
import sys

from gtm.db.client import get_client
from gtm.skills import form_d_sweep, spinout_watcher
from gtm.skills._shared.context import RepoBundle, open_run
from gtm.skills._shared.sources import FormDRecord, SourceBundle


class ReplayEdgar:
    """EdgarSource fed from a run's archived payloads."""

    def __init__(self, payloads: list[dict]) -> None:
        self.form_d: list[FormDRecord] = []
        self.history: dict[str, int] = {}
        for payload in payloads:
            op = (payload.get("request") or {}).get("op")
            if op == "form_d":
                self.form_d.append(FormDRecord.model_validate(payload["response"]))
            elif op == "form_d_history":
                cik = str(payload["request"].get("cik"))
                self.history[cik] = int(payload["response"].get("original_form_d_count", 1))
        self.current_run_id = None

    def recent_form_d(self, lookback_days: int, max_filings: int = 200):
        return self.form_d[:max_filings]

    def form_d_history_count(self, cik: str) -> int:
        return self.history.get(str(cik), 1)


class ReplayWeb:
    """TavilySearch fed from archived search responses, keyed by query."""

    def __init__(self, payloads: list[dict]) -> None:
        from gtm.skills._shared.web_search import SearchResult

        self.by_query: dict[str, list] = {}
        for payload in payloads:
            request = payload.get("request") or {}
            if request.get("op") != "search":
                continue
            results = [
                SearchResult(
                    title=item.get("title") or "", url=item.get("url") or "",
                    content=item.get("content") or "", score=float(item.get("score") or 0),
                    published_date=item.get("published_date"),
                )
                for item in (payload.get("response") or {}).get("results", [])
                if item.get("url")
            ]
            self.by_query[request.get("query", "")] = results
        self.current_run_id = None

    def search(self, query: str, **kwargs):
        return self.by_query.get(query, [])


REPLAYABLE = {
    "form_d_sweep": (form_d_sweep.run, lambda p: SourceBundle(edgar=ReplayEdgar(p))),
    "spinout_watcher": (spinout_watcher.run, lambda p: SourceBundle(web=ReplayWeb(p))),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_run_id")
    args = parser.parse_args()
    client = get_client()

    run_rows = (
        client.table("source_runs").select("*").eq("id", args.source_run_id).limit(1).execute()
    ).data
    if not run_rows:
        print(f"no source_run {args.source_run_id}")
        return 1
    skill_name = run_rows[0]["skill_name"]
    if skill_name not in REPLAYABLE:
        print(f"skill '{skill_name}' is not wired for replay (supported: {list(REPLAYABLE)})")
        return 1

    payloads = (
        client.table("raw_payloads").select("request, response")
        .eq("source_run_id", args.source_run_id).is_("deleted_at", "null")
        .limit(1000).execute()
    ).data
    print(f"replaying {skill_name} against {len(payloads)} archived payloads...")

    run_fn, bundle_factory = REPLAYABLE[skill_name]
    db = RepoBundle()
    with open_run(skill_name, sources=bundle_factory(payloads), db=db) as ctx:
        ctx.result.metadata["replay_of"] = args.source_run_id
        result = run_fn(ctx)

    print(f"replay run {ctx.run_id}: {result.status} | processed {result.records_processed} "
          f"| signals {len(result.signals_emitted)} (dedupe means unchanged parses no-op)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
