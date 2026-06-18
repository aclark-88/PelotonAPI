"""SkillContext / SkillResult contracts and the run lifecycle.

The orchestrator owns the lifecycle:

    sources = build_sources()
    with open_run("form_d_sweep", sources=sources) as ctx:
        result = form_d_sweep.run(ctx, lookback_days=1)

open_run() opens the source_runs row, yields the context, and closes the row
with final counts and status — `failed` on exception, `partial` when errors
accumulated, `success` otherwise. Every DB write inside the skill passes
ctx.run_id as source_run_id.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field

from gtm.db.client import get_client
from gtm.db.repositories import FundsRepo, OutreachRepo, PeopleRepo, RunsRepo, SignalsRepo
from gtm.models.common import RunStatus
from gtm.skills._shared.logging import get_skill_logger
from gtm.skills._shared.sources import SourceBundle

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"


class RepoBundle:
    """All repositories on one shared client. ctx.db.<aggregate>."""

    def __init__(self, client: Any | None = None) -> None:
        self.client = client or get_client()
        self.funds = FundsRepo(self.client)
        self.people = PeopleRepo(self.client)
        self.signals = SignalsRepo(self.client)
        self.outreach = OutreachRepo(self.client)
        self.runs = RunsRepo(self.client)


class SkillResult(BaseModel):
    status: Literal["success", "partial", "failed"] = "success"
    records_processed: int = 0
    records_inserted: int = 0
    records_updated: int = 0
    signals_emitted: list[UUID] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResultBuilder:
    """Mutable accumulator a skill fills while running; open_run() reads it to
    close the source_runs row even if the skill raises mid-batch."""

    def __init__(self) -> None:
        self.records_processed = 0
        self.records_inserted = 0
        self.records_updated = 0
        self.signals_emitted: list[UUID] = []
        self.errors: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {}

    def error(self, where: str, exc: Exception | str, **extra: Any) -> None:
        self.errors.append({"where": where, "error": str(exc), **extra})

    def emit(self, signal_id: UUID | None) -> None:
        if signal_id is not None:
            self.signals_emitted.append(signal_id)

    def status(self) -> Literal["success", "partial"]:
        return "partial" if self.errors else "success"

    def build(self, **metadata: Any) -> SkillResult:
        self.metadata.update(metadata)
        return SkillResult(
            status=self.status(),
            records_processed=self.records_processed,
            records_inserted=self.records_inserted,
            records_updated=self.records_updated,
            signals_emitted=self.signals_emitted,
            errors=self.errors,
            metadata=self.metadata,
        )


class SkillContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    skill_name: str
    run_id: UUID
    db: RepoBundle
    sources: SourceBundle
    logger: Any
    dry_run: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    result: ResultBuilder = Field(default_factory=ResultBuilder)


def load_config(skill_name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """<skill_name>.yaml merged with icp.yaml (under key 'icp'); explicit
    overrides win. Missing files are empty dicts — the YAML is the contract,
    but a skill must not crash because a knob file does not exist yet."""
    config: dict[str, Any] = {}
    for shared_key, filename in (("icp", "icp.yaml"), ("clarion", "clarion_coverage.yaml")):
        path = CONFIGS_DIR / filename
        if path.exists():
            config[shared_key] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    skill_path = CONFIGS_DIR / f"{skill_name}.yaml"
    if skill_path.exists():
        config.update(yaml.safe_load(skill_path.read_text(encoding="utf-8")) or {})
    # Shared vocab files the skill opts into via `includes:` in its yaml
    # (e.g. motherships.yaml, vendors.yaml). Skill-level keys win.
    for include in config.get("includes") or []:
        include_path = CONFIGS_DIR / include
        if include_path.exists():
            for key, value in (yaml.safe_load(include_path.read_text(encoding="utf-8")) or {}).items():
                config.setdefault(key, value)
    if overrides:
        config.update({k: v for k, v in overrides.items() if v is not None})
    return config


@contextmanager
def open_run(
    skill_name: str,
    *,
    sources: SourceBundle,
    db: RepoBundle | None = None,
    dry_run: bool = False,
    config_overrides: dict[str, Any] | None = None,
) -> Iterator[SkillContext]:
    db = db or RepoBundle()
    run = db.runs.start_run(skill_name, metadata={"dry_run": dry_run})
    ctx = SkillContext(
        skill_name=skill_name,
        run_id=run.id,
        db=db,
        sources=sources,
        logger=get_skill_logger(skill_name, str(run.id)),
        dry_run=dry_run,
        config=load_config(skill_name, config_overrides),
        result=ResultBuilder(),
    )
    # Bind the run to every source that archives raw payloads
    for field in type(sources).model_fields:
        src = getattr(sources, field, None)
        if src is not None and hasattr(src, "current_run_id"):
            src.current_run_id = run.id
    ctx.logger.info("run_started")
    try:
        yield ctx
    except Exception as exc:
        ctx.result.error("run", exc, fatal=True)
        ctx.logger.error("run_failed", error=str(exc))
        db.runs.finish_run(
            run.id,
            RunStatus.failed,
            records_processed=ctx.result.records_processed,
            records_inserted=ctx.result.records_inserted,
            records_updated=ctx.result.records_updated,
            errors=ctx.result.errors,
        )
        raise
    else:
        status = RunStatus.partial if ctx.result.errors else RunStatus.success
        db.runs.finish_run(
            run.id,
            status,
            records_processed=ctx.result.records_processed,
            records_inserted=ctx.result.records_inserted,
            records_updated=ctx.result.records_updated,
            errors=ctx.result.errors,
        )
        ctx.logger.info(
            "run_finished",
            status=status.value,
            processed=ctx.result.records_processed,
            signals=len(ctx.result.signals_emitted),
        )
