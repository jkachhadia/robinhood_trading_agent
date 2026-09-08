"""Human/model-readable status text. Used by the CLI and by SessionStart/UserPromptSubmit hooks."""

from __future__ import annotations

import json
from typing import Optional

from . import paths
from .config import Levers
from .db import Store
from .journal import expire_proposals, state_view
from .models import Proposal


def _money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def _pct(pnl: Optional[float], base: Optional[float]) -> str:
    if pnl is None or not base:
        return "n/a"
    return f"{pnl / base * 100:+.2f}%"


def pending_rows(store: Store):
    expire_proposals(store)
    return store.all("SELECT * FROM proposals WHERE status='pending' ORDER BY id")


def actionable_rows(store: Store):
    expire_proposals(store)
    return store.all("SELECT * FROM proposals WHERE status IN ('approved','auto_eligible') ORDER BY id")


def proposal_line(row) -> str:
    p = Proposal.model_validate_json(row["payload"])
    mq = f" max_qty {float(row['max_qty']):g}" if row["max_qty"] is not None else ""
    return f"#{row['id']} [{row['status']}] {p.summary()}{mq} (created {row['created_at']})"


def status_text(store: Store, levers: Levers, compact: bool = False) -> str:
    s = state_view(store, levers)
    lines = []
    flags = []
    if levers.kill_switch:
        flags.append("KILL SWITCH ON")
    if levers.dry_run:
        flags.append("DRY RUN (paper)")
    head = f"tradeagent | mode={levers.mode.value}" + (" | " + " | ".join(flags) if flags else "")
    lines.append(head)
    age = s.snapshot_age_minutes
    age_s = "none" if age is None else f"{age:.0f} min old"
    lines.append(f"equity {_money(s.equity)} | buying power {_money(s.buying_power)} | snapshot {age_s}")
    lines.append(
        f"P&L today {_money(s.pnl_today)} ({_pct(s.pnl_today, s.day_open_equity)}, limit -{levers.risk.max_daily_loss_pct}%) | "
        f"week {_money(s.pnl_week)} ({_pct(s.pnl_week, s.week_open_equity)}, limit -{levers.risk.max_weekly_loss_pct}%)"
    )
    lines.append(
        f"open positions {len(s.open_positions())}/{levers.risk.max_open_positions} | "
        f"opening trades today {s.trades_today}/{levers.risk.max_trades_per_day} | "
        f"max order ${levers.risk.max_order_notional_usd:,.0f} | max position {levers.risk.max_position_pct}%"
    )
    if s.halted_reason:
        lines.append(f"HALTED: {s.halted_reason}")
    pos = s.open_positions()
    if pos:
        lines.append("positions:")
        for p in sorted(pos, key=lambda x: x.instrument_key):
            px = s.quotes.get(p.instrument_key)
            mv = p.market_value if p.market_value is not None else (px * p.qty * (100 if p.instrument.value == 'option' else 1) if px else None)
            lines.append(f"  {p.instrument_key}: qty {p.qty:g} avg {p.avg_cost if p.avg_cost is not None else 'n/a'} value {_money(mv)}")
    pend = pending_rows(store)
    act = actionable_rows(store)
    if pend:
        lines.append(f"pending approval ({len(pend)}):")
        for r in pend[: (5 if compact else 50)]:
            lines.append("  " + proposal_line(r))
    if act:
        lines.append(f"ready to execute ({len(act)}):")
        for r in act[: (5 if compact else 50)]:
            lines.append("  " + proposal_line(r))
    if not compact:
        lp = paths.lessons_path()
        if lp.exists():
            tail = lp.read_text().strip().splitlines()[-10:]
            if tail:
                lines.append("recent lessons:")
                lines.extend("  " + t for t in tail)
    return "\n".join(lines)


def context_text(store: Store, levers: Levers) -> str:
    """What gets injected into Claude's context at session start / each prompt."""
    body = status_text(store, levers, compact=True)
    rules = (
        "Gate rules in force: every place_* call must match a proposal created with `uv run tradeagent propose`, "
        "must be preceded by the matching review_* call with identical params, and is checked against hard limits. "
        f"Mode {levers.mode.value}: "
        + {
            "approve_all": "no order executes without a human approval (queued if headless).",
            "tiered": f"orders ≤ ${levers.tiered.auto_max_notional_usd:,.0f} with confidence ≥ {levers.tiered.auto_min_confidence} may auto-execute; others queue.",
            "autonomous": "orders within hard limits execute without approval.",
        }[levers.mode.value]
    )
    lp = paths.lessons_path()
    lessons = ""
    if lp.exists():
        tail = lp.read_text().strip().splitlines()[-8:]
        if tail:
            lessons = "\nRecent lessons (data/lessons.md):\n" + "\n".join("- " + t.lstrip("- ") for t in tail)
    return f"<tradeagent-state>\n{body}\n{rules}{lessons}\n</tradeagent-state>"
