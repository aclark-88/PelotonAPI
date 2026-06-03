"""Daily digest: full Tier 1+2 queue rendered as an email-friendly HTML report.

`build_digest` assembles the current Tier 1/2 managers and flags any that are new
since the previous digest (tracked in a small JSON state file). `render_html`
produces a self-contained, inline-styled document that survives email clients
(no <style>/external CSS/JS), and is also written to disk for the desktop/file route.
"""
from __future__ import annotations

import datetime as dt
import html
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import config, personas, signals as sig
from .models import Manager

STATE_FILE = "digest_state.json"

TIER_COLORS = {1: "#c0392b", 2: "#c77f1a", 3: "#2e8b57", 4: "#5b6673"}


@dataclass
class DigestRow:
    manager_id: int
    name: str
    tier: int
    score: float
    tags: list[str]
    hq: str
    last_signal_date: str
    signal_labels: list[str]
    reason: str
    persona: str
    is_new: bool


@dataclass
class Digest:
    generated_at: dt.datetime
    rows: list[DigestRow] = field(default_factory=list)
    first_run: bool = False

    @property
    def tier1(self) -> int:
        return sum(1 for r in self.rows if r.tier == 1)

    @property
    def tier2(self) -> int:
        return sum(1 for r in self.rows if r.tier == 2)

    @property
    def new_count(self) -> int:
        return sum(1 for r in self.rows if r.is_new)


def _state_path() -> Path:
    return config.export_dir() / STATE_FILE


def _load_prior_ids() -> set[int] | None:
    p = _state_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return set(data.get("manager_ids", []))
    except (json.JSONDecodeError, OSError):
        return None


def _persona_for(manager: Manager) -> str:
    for c in manager.contacts:
        if c.persona:
            return personas.label_for(c.persona)
    return personas.label_for(personas.DEFAULT_PERSONA_ORDER[0])


def build_digest(session: Session, min_tier: int = 2) -> Digest:
    """Build the digest over the current Tier ≤ min_tier queue."""
    managers = session.scalars(
        select(Manager)
        .where(Manager.tier <= min_tier)
        .order_by(Manager.total_score.desc(), Manager.last_signal_date.desc())
        .options(selectinload(Manager.signals), selectinload(Manager.contacts))
    ).all()

    prior = _load_prior_ids()
    first_run = prior is None
    prior = prior or set()

    rows: list[DigestRow] = []
    for m in managers:
        reason = max((s.reason for s in m.signals), key=len, default="")
        rows.append(
            DigestRow(
                manager_id=m.id,
                name=m.legal_name,
                tier=m.tier,
                score=round(m.total_score, 0),
                tags=list(m.strategy_tags or []),
                hq=", ".join(p for p in (m.hq_city, m.hq_state) if p),
                last_signal_date=m.last_signal_date.isoformat() if m.last_signal_date else "",
                signal_labels=[sig.LABELS.get(s.signal_type, s.signal_type) for s in m.signals],
                reason=reason,
                persona=_persona_for(m),
                # First run is the baseline: nothing flagged "new".
                is_new=(not first_run and m.id not in prior),
            )
        )
    return Digest(generated_at=dt.datetime.now(), rows=rows, first_run=first_run)


def save_state(session: Session, min_tier: int = 2) -> None:
    """Persist the current Tier ≤ min_tier manager-id set as the new baseline."""
    ids = session.scalars(select(Manager.id).where(Manager.tier <= min_tier)).all()
    _state_path().write_text(
        json.dumps({"generated_at": dt.datetime.now().isoformat(), "manager_ids": list(ids)})
    )


# --- Rendering ----------------------------------------------------------------
def subject(d: Digest) -> str:
    bits = f"{d.tier1} Tier 1 · {d.tier2} Tier 2"
    if d.new_count and not d.first_run:
        bits += f" · {d.new_count} new"
    return f"Coremont Signal Engine — {d.generated_at:%a %b %-d}: {bits}"


def _esc(s: str) -> str:
    return html.escape(s or "")


def _chip(text: str, bg: str, fg: str = "#ffffff") -> str:
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'border-radius:10px;padding:1px 8px;font-size:11px;font-weight:700;'
        f'margin:1px 3px 1px 0;white-space:nowrap">{_esc(text)}</span>'
    )


def render_html(d: Digest) -> str:
    head = f"""\
<div style="background:#0f1216;color:#e7ecf2;padding:18px 22px;border-radius:10px 10px 0 0;font-family:Arial,Helvetica,sans-serif">
  <div style="font-size:18px"><span style="color:#4ea1ff">◆</span> Coremont <b>Signal Engine</b> — Daily Digest</div>
  <div style="color:#8b97a6;font-size:13px;margin-top:4px">{d.generated_at:%A, %B %-d, %Y} · SEC Form D → Clarion PMS fit</div>
  <div style="margin-top:10px">
    {_chip(f"Tier 1: {d.tier1}", TIER_COLORS[1])}
    {_chip(f"Tier 2: {d.tier2}", TIER_COLORS[2])}
    {_chip(f"New since last digest: {d.new_count}", "#1f6feb") if not d.first_run else _chip("Baseline run", "#5b6673")}
  </div>
</div>"""

    if not d.rows:
        body = (
            '<div style="padding:28px;color:#5b6673;font-family:Arial,sans-serif">'
            "No Tier 1 or Tier 2 managers in the current queue.</div>"
        )
        return f'<div style="max-width:820px;margin:auto">{head}{body}</div>'

    rows_html = []
    for r in d.rows:
        tier_chip = _chip(f"Tier {r.tier}", TIER_COLORS.get(r.tier, "#5b6673"))
        new_chip = _chip("NEW", "#1f6feb") if r.is_new else ""
        tags = "".join(_chip(t, "#1e242d", "#cfe3ff") for t in r.tags[:5])
        sigs = "".join(_chip(s, "#13233a", "#aaccff") for s in r.signal_labels[:4])
        rows_html.append(
            f"""\
<tr style="border-bottom:1px solid #e3e8ee">
  <td style="padding:12px 10px;vertical-align:top;text-align:center;width:54px">
    <div style="font-size:22px;font-weight:800;color:#0f1216">{int(r.score)}</div>
    {tier_chip}
  </td>
  <td style="padding:12px 10px;vertical-align:top">
    <div style="font-size:15px;font-weight:700;color:#0f1216">{_esc(r.name)} {new_chip}</div>
    <div style="color:#5b6673;font-size:12px;margin:2px 0 6px">{_esc(r.hq)}{' · ' if r.hq else ''}fresh {_esc(r.last_signal_date)} · buyer: {_esc(r.persona)}</div>
    <div style="margin-bottom:6px">{tags}{sigs}</div>
    <div style="color:#33404f;font-size:13px;line-height:1.45">{_esc(r.reason)}</div>
  </td>
</tr>"""
        )

    table = (
        '<table cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;background:#ffffff;font-family:Arial,Helvetica,sans-serif">'
        + "".join(rows_html)
        + "</table>"
    )
    footer = (
        '<div style="padding:14px 22px;background:#f4f6f9;color:#8b97a6;font-size:11px;'
        'border-radius:0 0 10px 10px;font-family:Arial,sans-serif">'
        "Transparent rules-based scoring · Tier 1 = 75–100, Tier 2 = 55–74 · "
        "Open the full app for filters, score breakdowns, and CSV/CRM export.</div>"
    )
    return f'<div style="max-width:820px;margin:auto;border:1px solid #e3e8ee;border-radius:10px">{head}{table}{footer}</div>'


def render_text(d: Digest) -> str:
    """Plain-text fallback for email clients that block HTML."""
    lines = [
        f"Coremont Signal Engine — Daily Digest — {d.generated_at:%Y-%m-%d}",
        f"Tier 1: {d.tier1}  |  Tier 2: {d.tier2}"
        + ("" if d.first_run else f"  |  New: {d.new_count}"),
        "",
    ]
    for r in d.rows:
        flag = " [NEW]" if r.is_new else ""
        lines.append(f"[{int(r.score)}] Tier {r.tier} — {r.name}{flag}")
        if r.hq:
            lines.append(f"    {r.hq} · fresh {r.last_signal_date} · buyer: {r.persona}")
        if r.tags:
            lines.append(f"    tags: {', '.join(r.tags)}")
        lines.append(f"    {r.reason}")
        lines.append("")
    return "\n".join(lines)


def write_html_file(d: Digest, path: str | Path | None = None) -> str:
    path = Path(path or (config.export_dir() / "digest.html"))
    path.write_text(render_html(d), encoding="utf-8")
    return str(path)
