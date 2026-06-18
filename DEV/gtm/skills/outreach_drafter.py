"""outreach_drafter — LinkedIn campaign assets for one (person, signal).

LinkedIn-only via HeyReach (explicit scope: no email in this campaign).
Produces per target:
  - 3 connection-request note variants (A/B/C, ≤300 chars, Nancy Tang
    structure: capability-first -> recipient-domain tie -> compare-notes close)
  - 1 post-accept follow-up message (≤5 sentences, soft meeting ask allowed)

Grounding: voice contract from configs/voice.md; capability claims from the
canonical clarion-capabilities.md (fallback: clarion_coverage.yaml); signal
payload as the Tier 1 evidence; top-N similar past drafts as few-shot
exemplars (Python-side cosine over drafts.embedding — an RPC would need a
migration, deferred until volume demands it).

Every variant passes a hard validator (length, em dashes, banned phrases,
incumbent system names). One corrective LLM round-trip on failure; variants
still failing are dropped. Drafts land with approved_at=null — NOTHING SENDS
from this skill; dispatch is heyreach_dispatcher after human approval.

GENERATION MODES (the llm source seam):
  - Orchestrator mode (default): the orchestrating Claude Code session is the
    model. Call prepare_prompt(ctx, person_id, signal_id) to get the grounded
    system+user prompt, author the JSON yourself, wrap it in
    InjectedLLM([json_str]) in the SourceBundle, then call run(). The
    validator gates the output identically either way.
  - Headless fallback: LLMClient (direct API, needs ANTHROPIC_API_KEY) for
    unattended runs. Optional — drafting is human-reviewed, so it can also
    just wait for the next orchestrated session.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from gtm.models.common import Channel
from gtm.models.outreach import CampaignIn, DraftIn
from gtm.skills._shared.context import CONFIGS_DIR, SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "outreach_drafter"


# ── validator (pure) ─────────────────────────────────────────────────────────

def incumbent_terms(cfg: dict[str, Any]) -> list[str]:
    terms = list(cfg.get("extra_incumbent_terms", []))
    for vendors in (cfg.get("vendors") or {}).values():
        for vendor in vendors:
            terms.append(vendor["name"])
            terms.extend(vendor.get("keywords", []))
    return terms


def validate_linkedin_copy(text: str, cfg: dict[str, Any], is_cr: bool = True) -> list[str]:
    """Returns violations; empty list = clean."""
    violations: list[str] = []
    if is_cr and len(text) > int(cfg.get("cr_max_chars", 300)):
        violations.append(f"over {cfg.get('cr_max_chars', 300)} chars ({len(text)})")
    if "—" in text or "–" in text:
        violations.append("contains em/en dash")
    lowered = text.lower()
    for phrase in cfg.get("banned_phrases", []):
        if phrase.lower() in lowered:
            violations.append(f"banned phrase: '{phrase}'")
    for term in incumbent_terms(cfg):
        if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
            violations.append(f"names incumbent system: '{term}'")
    return violations


def select_angle(strategies: list[str], cfg: dict[str, Any]) -> dict[str, str]:
    """Strategy-based play selection (first matching rule wins):
    clarion_pms for multi-asset/macro/credit/etc.; network_value (cap intro /
    outsourced back office / Alex's network) for long/short equity."""
    for angle in cfg.get("angles", []):
        if set(angle.get("when_strategies_any", [])) & set(strategies or []):
            return {"key": angle["key"], "focus": angle.get("focus", "")}
    default = str(cfg.get("default_angle", "clarion_pms"))
    for angle in cfg.get("angles", []):
        if angle["key"] == default:
            return {"key": default, "focus": angle.get("focus", "")}
    return {"key": default, "focus": ""}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


# ── context assembly ─────────────────────────────────────────────────────────

def _capabilities_text(cfg: dict[str, Any]) -> str:
    path = cfg.get("clarion_capabilities_path")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            pass
    # fallback: render the coverage config
    return yaml.safe_dump(cfg.get("clarion", {}), sort_keys=False)


def _voice_text() -> str:
    return (CONFIGS_DIR / "voice.md").read_text(encoding="utf-8")


def _exemplars(ctx: SkillContext, anchor_text: str) -> list[str]:
    embedder = getattr(ctx.sources, "embedder", None)
    if embedder is None:
        return []
    try:
        query_vec = embedder.embed_one(anchor_text)
        rows = (
            ctx.db.client.table("drafts")
            .select("body, embedding, approved_at")
            .eq("channel", "linkedin")
            .not_.is_("embedding", "null")
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .limit(int(ctx.config.get("exemplar_pool", 200)))
            .execute()
        ).data
        scored = []
        for row in rows:
            emb = row.get("embedding")
            if isinstance(emb, str):
                emb = json.loads(emb)
            if emb:
                scored.append((_cosine(query_vec, emb), row))
        scored.sort(key=lambda t: t[0], reverse=True)
        # approved drafts are stronger exemplars; stable sort keeps similarity order
        scored.sort(key=lambda t: bool(t[1].get("approved_at")), reverse=True)
        return [r["body"] for _, r in scored[: int(ctx.config.get("exemplar_count", 5))]]
    except Exception as exc:
        ctx.logger.warning("exemplar_retrieval_failed", error=str(exc))
        return []


def _resolve_campaign(ctx: SkillContext, signal_type: str, campaign_id: str | None):
    if campaign_id:
        return UUID(str(campaign_id))
    mapping = {
        c["signal_type_key"]: c["name"]
        for c in ctx.config.get("campaigns", [])
        if c.get("signal_type_key")
    }
    name = mapping.get(signal_type)
    if name is None:
        return None
    campaign = ctx.db.outreach.upsert_campaign(
        CampaignIn(name=name, signal_type_key=signal_type, channel=Channel.linkedin)
    )
    return campaign.id


# ── prompt ───────────────────────────────────────────────────────────────────

def _build_prompt(
    person, fund, signal, exemplars: list[str], cfg: dict[str, Any],
    angle: dict[str, str] | None = None,
) -> tuple[str, str]:
    angle = angle or select_angle(fund.strategies if fund else [], cfg)
    system = (
        "You draft LinkedIn outreach for Alex, Enterprise AE at Coremont (Clarion). "
        "Follow the voice contract EXACTLY. Output ONLY valid JSON: "
        '{"cr_variants": ["...", "...", "..."], "followup": "..."}. '
        f"Each cr_variant <= {cfg.get('cr_max_chars', 300)} characters, uses the literal token "
        "{{firstName}} as the greeting name. The followup is <= "
        f"{cfg.get('followup_max_sentences', 5)} short sentences, references the same anchor, "
        "may softly ask for 15 minutes.\n\n=== VOICE CONTRACT ===\n" + _voice_text() +
        "\n\n=== CLARION CAPABILITIES (canonical; only source of capability claims) ===\n" +
        _capabilities_text(cfg)[:20000]
    )
    clarion_match = ", ".join(
        ac for ac, spec in (cfg.get("clarion", {}).get("asset_classes") or {}).items()
        if set(spec.get("strategies", [])) & set(fund.strategies if fund else [])
    )
    user = json.dumps(
        {
            "person": {
                "name": person.full_name,
                "title": person.current_role,
                "function": str(person.current_role_function.value),
                "seniority": str(person.current_role_seniority.value),
            },
            "fund": {
                "name": (fund.common_name or fund.legal_name) if fund else None,
                "strategies": fund.strategies if fund else [],
                "aum_band": fund.aum_band if fund else "unknown",
                "is_emerging_manager": fund.is_emerging_manager if fund else None,
                "clarion_coverage_match": clarion_match,
            },
            "trigger_signal": {
                "type": signal.signal_type,
                "observed_at": signal.observed_at.isoformat(),
                "payload": signal.payload,
            },
            "angle": angle,  # the play: clarion_pms vs network_value (see voice.md Mission)
            "exemplars_of_past_drafts": exemplars,
            "task": (
                f"Draft {cfg.get('cr_variant_count', 3)} distinct connection-request note "
                "variants (different capability/topic angles, same structure) and 1 "
                "post-accept follow-up message."
            ),
        },
        default=str,
    )
    return system, user


def _parse_response(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM response contained no JSON object")
    return json.loads(match.group(0))


# ── skill ────────────────────────────────────────────────────────────────────

def prepare_prompt(
    ctx: SkillContext, person_id: str, signal_id: str
) -> dict[str, str] | None:
    """Orchestrator-mode helper: the grounded (system, user) prompt for this
    target, exactly as run() would build it. The orchestrating Claude authors
    the JSON response itself, then calls run() with InjectedLLM([response])."""
    person = ctx.db.people.get(UUID(str(person_id)))
    signal = ctx.db.signals.get(UUID(str(signal_id)))
    if person is None or signal is None:
        return None
    fund = ctx.db.funds.get(signal.fund_id) if signal.fund_id else (
        ctx.db.funds.get(person.current_fund_id) if person.current_fund_id else None
    )
    anchor = f"{signal.signal_type} {fund.legal_name if fund else ''} {person.current_role or ''}"
    system, user = _build_prompt(person, fund, signal, _exemplars(ctx, anchor), ctx.config)
    return {"system": system, "user": user}


def run(
    ctx: SkillContext,
    person_id: str,
    signal_id: str,
    campaign_id: str | None = None,
) -> SkillResult:
    person = ctx.db.people.get(UUID(str(person_id)))
    signal = ctx.db.signals.get(UUID(str(signal_id)))
    if person is None or signal is None:
        ctx.result.error("resolve", f"person={person_id} signal={signal_id} not found")
        return ctx.result.build()
    fund = ctx.db.funds.get(signal.fund_id) if signal.fund_id else (
        ctx.db.funds.get(person.current_fund_id) if person.current_fund_id else None
    )
    if not person.linkedin_url:
        ctx.result.error("resolve", f"{person.full_name} has no linkedin_url — LinkedIn-only campaign needs one")
        return ctx.result.build()

    try:
        llm = ctx.sources.require("llm")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    cfg = ctx.config
    resolved_campaign_id = _resolve_campaign(ctx, signal.signal_type, campaign_id)
    angle = select_angle(fund.strategies if fund else [], cfg)
    ctx.logger.info("angle_selected", angle=angle["key"],
                    strategies=fund.strategies if fund else [])
    anchor_text = (f"{angle['key']} {signal.signal_type} "
                   f"{fund.legal_name if fund else ''} {person.current_role or ''}")
    exemplars = _exemplars(ctx, anchor_text)
    system, user = _build_prompt(person, fund, signal, exemplars, cfg, angle=angle)

    response = llm.complete(system=system, user=user, model=cfg.get("model"))
    parsed = _parse_response(response)
    ctx.result.records_processed = 1

    # validate; one corrective retry for failures
    assets: list[tuple[str, str, bool]] = []  # (variant_label, body, is_cr)
    for i, variant in enumerate(parsed.get("cr_variants", [])[: int(cfg.get("cr_variant_count", 3))]):
        assets.append((chr(ord("A") + i), variant, True))
    if parsed.get("followup"):
        assets.append(("followup", parsed["followup"], False))

    final_assets: list[tuple[str, str]] = []
    rejected: list[dict[str, Any]] = []
    for label, body, is_cr in assets:
        violations = validate_linkedin_copy(body, cfg, is_cr=is_cr)
        if violations and int(cfg.get("max_validation_retries", 1)) > 0:
            fix = llm.complete(
                system=system,
                user=(
                    f"This draft violates the voice contract: {violations}. "
                    f"Rewrite it fixing every violation, same anchor and structure. "
                    f'Output ONLY JSON: {{"text": "..."}}\n\nDRAFT:\n{body}'
                ),
                model=cfg.get("model"),
            )
            try:
                body = _parse_response(fix)["text"]
                violations = validate_linkedin_copy(body, cfg, is_cr=is_cr)
            except Exception:
                pass
        if violations:
            rejected.append({"variant": label, "violations": violations})
            ctx.logger.warning("variant_rejected", variant=label, violations=violations)
            continue
        final_assets.append((label, body))

    if not any(label != "followup" for label, _ in final_assets):
        ctx.result.error("validation", "no CR variant survived validation", rejected=rejected)
        return ctx.result.build(rejected=rejected, draft_ids=[])

    # embeddings (optional — degrade without OPENAI key)
    embeddings: list[list[float] | None] = [None] * len(final_assets)
    embedder = getattr(ctx.sources, "embedder", None)
    if embedder is not None:
        try:
            embeddings = embedder.embed([body for _, body in final_assets])
        except Exception as exc:
            ctx.result.error("embeddings", exc)

    draft_ids: list[str] = []
    if not ctx.dry_run:
        for (label, body), embedding in zip(final_assets, embeddings):
            draft = ctx.db.outreach.create_draft(
                DraftIn(
                    person_id=person.id,
                    signal_id=signal.id,
                    campaign_id=resolved_campaign_id,
                    channel=Channel.linkedin,
                    variant_label=label,
                    subject=None,  # LinkedIn has no subject
                    body=body,
                    model=str(cfg.get("model")),
                    prompt_version=str(cfg.get("prompt_version")),
                    embedding=embedding,
                    metadata={
                        "char_count": len(body),
                        "asset_type": "connection_request" if label != "followup" else "followup_message",
                        "exemplar_count": len(exemplars),
                        "angle": angle["key"],
                    },
                ),
                source_run_id=ctx.run_id,
            )
            draft_ids.append(str(draft.id))
            ctx.result.records_inserted += 1

    usage = llm.usage_snapshot() if hasattr(llm, "usage_snapshot") else {}
    return ctx.result.build(
        draft_ids=draft_ids,
        rejected=rejected,
        angle=angle["key"],
        campaign_id=str(resolved_campaign_id) if resolved_campaign_id else None,
        llm_usage=usage,
    )
