"""The gate: decides whether an order-placing MCP call may proceed.

evaluate() is the only entry point. It never raises for business reasons; any
unexpected exception is the caller's signal to fail closed (deny).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from . import fieldmap as fm
from . import journal
from .config import Levers, Mode
from .db import Store
from .limits import GateContext, evaluate_limits
from .models import Decision, DecisionAction, Instrument, ParsedOrder, Proposal, ProposalStatus, Side

REVIEW_WINDOW_MINUTES = 30


def evaluate(levers: Levers, store: Store, hook_input: dict, headless: bool,
             now: Optional[datetime] = None) -> Optional[Decision]:
    """Return a Decision for gated tools, None for tools the gate does not govern."""
    tool_name = str(hook_input.get("tool_name", ""))
    short = fm.short_tool_name(tool_name, levers.tool_prefix)
    tool_input = hook_input.get("tool_input") or {}
    session_id = hook_input.get("session_id")
    tool_use_id = hook_input.get("tool_use_id")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if fm.is_cancel(short):
        d = Decision(action=DecisionAction.allow, reason="cancel requests are always allowed", rule="cancel")
        _log(store, levers, session_id, headless, tool_name, tool_use_id, d, None, tool_input)
        return d
    if not fm.is_place(short):
        return None

    # 0. hard stops that need no parsing
    if hook_input.get("agent_id") or hook_input.get("agent_type"):
        d = Decision(action=DecisionAction.deny, rule="subagent",
                     reason="subagents are research-only; only the main session may place orders")
        _log(store, levers, session_id, headless, tool_name, tool_use_id, d, None, tool_input)
        return d
    if levers.kill_switch:
        d = Decision(action=DecisionAction.deny, rule="kill_switch",
                     reason="KILL SWITCH is on. No orders may be placed. Stop trading and tell the user.")
        _log(store, levers, session_id, headless, tool_name, tool_use_id, d, None, tool_input)
        return d

    # 1. parse (fail closed)
    try:
        order = fm.parse_order(tool_name, short, tool_input)
    except ValueError as e:
        d = Decision(action=DecisionAction.deny, rule="parse", reason=f"gate could not parse the order: {e}")
        _log(store, levers, session_id, headless, tool_name, tool_use_id, d, None, tool_input)
        return d

    if short == "place_crypto_order":
        d = Decision(action=DecisionAction.deny, rule="instrument", reason="crypto is not enabled in this build")
        _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
        return d

    state = journal.state_view(store, levers, now)
    reference_price = state.quotes.get(order.instrument_key())

    # 2. proposal match
    proposal_row = None
    proposal: Optional[Proposal] = None
    is_exit = order.side == Side.sell
    if levers.session.require_proposal_for_place:
        proposal_row, proposal, why = _match_proposal(store, levers, order)
        if proposal_row is None:
            d = Decision(action=DecisionAction.deny, rule="proposal", reason=why)
            _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
            return d
        is_exit = bool(proposal.is_exit)
        if proposal_row["status"] == ProposalStatus.executing.value:
            d = Decision(action=DecisionAction.deny, rule="in_flight", proposal_id=proposal_row["id"],
                         reason=f"proposal #{proposal_row['id']} already has an order in flight; check get_*_orders. "
                                f"If it failed, run `uv run tradeagent reset {proposal_row['id']}`.")
            _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
            return d
        if reference_price is None and proposal is not None:
            reference_price = proposal.entry_price

    # 3. review-before-place
    if levers.session.require_review_before_place:
        rv = journal.recent_review(store, order.match_hash(), REVIEW_WINDOW_MINUTES)
        if rv is None:
            d = Decision(action=DecisionAction.deny, rule="review",
                         proposal_id=proposal_row["id"] if proposal_row else None,
                         reason=f"call the matching review_*_order tool with identical parameters ({order.summary()}) "
                                f"within {REVIEW_WINDOW_MINUTES} min before placing")
            _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
            return d

    # 4. hard limits
    ctx = GateContext(
        levers=levers, order=order, state=state, proposal=proposal,
        proposal_max_qty=float(proposal_row["max_qty"]) if proposal_row and proposal_row["max_qty"] is not None else None,
        is_exit=is_exit, reference_price=reference_price,
    )
    violations = evaluate_limits(ctx)
    if violations:
        msg = "; ".join(f"[{v.rule}] {v.message}" for v in violations)
        d = Decision(action=DecisionAction.deny, rule=violations[0].rule,
                     proposal_id=proposal_row["id"] if proposal_row else None,
                     reason=f"order violates hard limits: {msg}. Do not retry with the same parameters.")
        _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
        return d

    # 5. mode / approval
    pid = proposal_row["id"] if proposal_row else None
    status = proposal_row["status"] if proposal_row else None
    approved = status == ProposalStatus.approved.value
    allow_reason: Optional[str] = None
    if approved:
        allow_reason = f"proposal #{pid} approved by human ({proposal_row['approval_source'] or 'cli'})"
    elif levers.mode == Mode.autonomous:
        allow_reason = "autonomous mode: within all hard limits"
        if pid:
            journal.set_proposal_status(store, pid, ProposalStatus.auto_eligible, approval_source="autonomous")
    elif levers.mode == Mode.tiered:
        ok, why = _tiered_auto(levers, ctx, proposal_row, state)
        if ok:
            allow_reason = f"tiered mode auto-approval: {why}"
            if pid:
                journal.set_proposal_status(store, pid, ProposalStatus.auto_eligible, approval_source="tiered_auto")
        else:
            return _needs_human(store, levers, session_id, headless, tool_name, tool_use_id, order, proposal_row, proposal,
                                tool_input, f"tiered mode requires approval: {why}")
    else:  # approve_all
        return _needs_human(store, levers, session_id, headless, tool_name, tool_use_id, order, proposal_row, proposal,
                            tool_input, "approve_all mode: every order needs a human")

    # 6. dry run: simulate instead of allowing
    if levers.dry_run:
        fill = order.limit_price or reference_price
        if fill is None:
            d = Decision(action=DecisionAction.deny, rule="dry_run", proposal_id=pid,
                         reason="DRY RUN: no price known to simulate a fill; fetch a quote (get_*_quotes) first")
            _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
            return d
        oid = journal.record_simulated_fill(store, session_id, tool_name, tool_use_id, order, tool_input, pid, is_exit, fill, now)
        d = Decision(action=DecisionAction.deny, rule="dry_run", proposal_id=pid,
                     reason=f"DRY RUN: order NOT sent to Robinhood. Simulated fill #{oid} recorded at {fill:g} "
                            f"({order.summary()}). Would have been allowed because: {allow_reason}. "
                            f"Treat this as filled for the rest of the session; do not retry.")
        _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
        return d

    if pid:
        journal.set_proposal_status(store, pid, ProposalStatus.executing)
    d = Decision(action=DecisionAction.allow, rule="allow", proposal_id=pid, reason=allow_reason)
    _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
    return d



def _needs_human(store, levers, session_id, headless, tool_name, tool_use_id, order: ParsedOrder, proposal_row, proposal,
                 tool_input, why: str) -> Decision:
    pid = proposal_row["id"] if proposal_row else None
    if pid and proposal_row["status"] != ProposalStatus.pending.value:
        journal.set_proposal_status(store, pid, ProposalStatus.pending)
    summary = proposal.summary() if proposal else order.summary()
    if headless:
        d = Decision(
            action=DecisionAction.deny, rule="needs_approval", proposal_id=pid,
            reason=(f"{why}. Proposal #{pid} is queued for human approval ({summary}). "
                    f"The user runs `uv run tradeagent approve {pid}` and then `/execute`. "
                    f"Do NOT retry this order in this session; move on."),
        )
    else:
        d = Decision(
            action=DecisionAction.ask, rule="needs_approval", proposal_id=pid,
            reason=(f"{why}. Approve this order? #{pid} {summary} | {order.summary()}"),
        )
    _log(store, levers, session_id, headless, tool_name, tool_use_id, d, order.match_hash(), tool_input)
    return d


def _tiered_auto(levers: Levers, ctx: GateContext, proposal_row, state) -> tuple[bool, str]:
    t = levers.tiered
    if ctx.is_exit:
        if t.exits_auto_allowed:
            return True, "risk-reducing exit"
        return False, "exits require approval (tiered.exits_auto_allowed=false)"
    n = ctx.notional or 0.0
    if n > t.auto_max_notional_usd:
        return False, f"notional ${n:,.0f} > auto_max_notional_usd ${t.auto_max_notional_usd:,.0f}"
    conf = float(proposal_row["confidence"]) if proposal_row and proposal_row["confidence"] is not None else 0.0
    if conf < t.auto_min_confidence:
        return False, f"confidence {conf:.2f} < auto_min_confidence {t.auto_min_confidence}"
    if ctx.order.instrument == Instrument.option and t.options_require_approval:
        return False, "options require approval"
    if t.new_symbol_requires_approval and ctx.order.symbol not in state.traded_symbols:
        return False, f"{ctx.order.symbol} has never been traded in this account"
    return True, f"notional ${n:,.0f} ≤ ${t.auto_max_notional_usd:,.0f}, confidence {conf:.2f}"


def _match_proposal(store: Store, levers: Levers, order: ParsedOrder):
    rows = journal.candidate_proposals(store, order.instrument_key(), order.side)
    if not rows:
        return None, None, (f"no open proposal for {order.summary()}. Write a proposal JSON and run "
                            f"`uv run tradeagent propose <file>` first (see /propose).")
    tol = levers.session.limit_price_tolerance_pct / 100.0
    problems = []
    for row in rows:
        p = Proposal.model_validate_json(row["payload"])
        if not p.is_exit and row["max_qty"] is not None and order.qty > float(row["max_qty"]) + 1e-9:
            problems.append(f"#{row['id']}: qty {order.qty:g} > max_qty {float(row['max_qty']):g}")
            continue
        if p.is_exit and row["max_qty"] is not None and order.qty > float(row["max_qty"]) + 1e-9:
            problems.append(f"#{row['id']}: exit qty {order.qty:g} > {float(row['max_qty']):g}")
            continue
        if order.limit_price is not None and p.limit_price is not None:
            if abs(order.limit_price - p.limit_price) > tol * p.limit_price:
                problems.append(f"#{row['id']}: limit {order.limit_price:g} differs from proposal {p.limit_price:g} by > {levers.session.limit_price_tolerance_pct}%")
                continue
        if order.order_type.value != p.order_type.value and not (order.limit_price is None and p.limit_price is None):
            # allow market vs limit mismatch only when neither carries a price
            if order.order_type.value in ("limit", "stop_limit") and p.limit_price is None:
                problems.append(f"#{row['id']}: proposal has no limit price but order is {order.order_type.value}")
                continue
        return row, p, ""
    return None, None, "proposals exist but none match this order: " + "; ".join(problems)


def _log(store, levers, session_id, headless, tool_name, tool_use_id, d: Decision, input_hash, tool_input) -> None:
    try:
        journal.record_decision(store, levers, session_id, headless, tool_name, tool_use_id, d.action.value, d.rule,
                                d.reason, d.proposal_id, input_hash, tool_input)
    except Exception:
        pass
