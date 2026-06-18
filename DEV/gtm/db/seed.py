"""Seed the signal_types lookup table.

Idempotent: upserts on key, so re-running after adding new types is safe.

    py -m gtm.db.seed
"""

from __future__ import annotations

from gtm.db.client import get_client

# (key, display_name, default_urgency, default_score_weight, description)
SIGNAL_TYPES: list[tuple[str, str, str, float, str]] = [
    ("new_fund_launch", "New fund launch", "immediate", 10,
     "A new pooled hedge-fund vehicle observed (e.g. Form D first filing)."),
    ("capital_raise_form_d", "Capital raise (Form D)", "this_week", 9,
     "New or amended Form D indicating an active raise."),
    ("new_coo", "New COO", "this_week", 9,
     "A fund hired or announced a new Chief Operating Officer."),
    ("new_head_risk", "New Head of Risk", "this_week", 8,
     "A fund hired or announced a new Head of Risk / CRO."),
    ("new_head_tech", "New Head of Technology", "this_week", 8,
     "A fund hired or announced a new Head of Technology."),
    ("new_cto", "New CTO", "this_week", 8,
     "A fund hired or announced a new Chief Technology Officer."),
    ("new_head_ops", "New Head of Operations", "this_week", 7,
     "A fund hired or announced a new Head of Operations."),
    ("new_cfo", "New CFO", "this_week", 7,
     "A fund hired or announced a new Chief Financial Officer."),
    ("spinout_detected", "Spinout detected", "immediate", 10,
     "A team spinning out of a mothership fund into a new vehicle."),
    ("displacement_inferred_job_post", "Displacement inferred (job post)", "this_week", 6,
     "Job postings implying incumbent OMS/PMS pain or replacement."),
    ("displacement_inferred_adv", "Displacement inferred (ADV)", "this_month", 5,
     "Form ADV changes implying ops/system stress (service provider churn)."),
    ("conference_attendance", "Conference attendance", "this_month", 3,
     "Target fund or buyer attending a relevant industry conference."),
    ("regulatory_event_wells", "Regulatory event (Wells notice)", "this_month", 4,
     "Wells notice or similar regulatory escalation at the fund."),
    ("regulatory_event_settlement", "Regulatory event (settlement)", "this_month", 4,
     "Regulatory settlement; often precedes ops/controls rebuilds."),
    ("pb_change", "Prime broker change", "this_month", 5,
     "A change in prime broker relationships."),
    ("admin_change", "Administrator change", "this_month", 5,
     "A change of fund administrator."),
    ("strategy_launch", "New strategy launch", "this_week", 7,
     "An existing manager launching a new strategy/vehicle."),
    ("anchor_lp_announcement", "Anchor LP announcement", "this_week", 6,
     "A named anchor allocation into a fund (seed/acceleration capital)."),
    ("manual_flag", "Manual flag", "this_week", 5,
     "Human-entered signal from research, a call, or off-system intel."),
    ("new_role", "Champion changed role", "this_week", 8,
     "A tracked person moved to a new fund/role. Emitted by fn_observe_job_change."),
    ("fit_score_changed", "Fit score changed materially", "this_week", 6,
     "adv_fit_scorer moved a fund's fit score past the configured delta threshold."),
    ("derivatives_intensity_high", "High derivatives intensity (13F)", "this_week", 7,
     "13F analysis shows options/complexity above threshold — Clarion quant/risk fit."),
    ("hiring_velocity_high", "High ops/tech hiring velocity", "this_month", 5,
     "3+ tech/ops/risk roles posted in 60 days (web-search proxy)."),
    ("contact_gap", "Buying-committee contact gap", "this_month", 3,
     "Apollo could not fill a target role at a fund — human research queue."),
    ("opp_at_risk", "Open opportunity at risk", "this_week", 6,
     "pipeline_hygiene_auditor flagged a stale/decayed open deal."),
    ("referral_delivered", "Referral delivered to network", "this_month", 5,
     "We introduced a fund (typically an L/S equity launch) to a network "
     "partner. fund_id = the fund delivered; payload.partner = who received it. "
     "One side of the relationship-currency ledger."),
    ("referral_received", "Referral received from network", "immediate", 9,
     "A network partner sent us a fund (typically macro/multi-strat/RV/credit "
     "— a Clarion buyer). fund_id = the fund received; payload.partner = who "
     "sent it. The payback side of the ledger; treat like a warm inbound."),
]


def seed() -> int:
    client = get_client()
    rows = [
        {
            "key": key,
            "display_name": name,
            "default_urgency": urgency,
            "default_score_weight": weight,
            "description": description,
            "active": True,
        }
        for key, name, urgency, weight, description in SIGNAL_TYPES
    ]
    client.table("signal_types").upsert(rows, on_conflict="key").execute()
    return len(rows)


if __name__ == "__main__":
    count = seed()
    print(f"seeded {count} signal types")
