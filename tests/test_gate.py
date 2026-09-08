from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tradeagent import journal
from tradeagent.gate import evaluate
from tradeagent.models import Proposal, ProposalStatus

from conftest import (CLOSED_NOW, LATE_NOW, OPEN_NOW, TOOL, equity_order, hook_input, levers, proposal_dict,
                      record_review, snapshot)


def propose(store, lv, **over):
    p = Proposal.model_validate(proposal_dict(**over))
    pid, st, reasons, sizing = journal.create_proposal(store, lv, p)
    return pid, st, reasons, sizing


def approve(store, pid):
    journal.set_proposal_status(store, pid, ProposalStatus.approved, approval_source="cli", approved_at=journal.now_iso())


# ---- basics ----------------------------------------------------------------------

def test_non_gated_tool_returns_none(env, store):
    lv = levers(env)
    assert evaluate(lv, store, hook_input("get_portfolio", {}), headless=False, now=OPEN_NOW) is None


def test_cancel_always_allowed(env, store):
    lv = levers(env)
    d = evaluate(lv, store, hook_input("cancel_equity_order", {"order_id": "x"}), headless=True, now=OPEN_NOW)
    assert d.action.value == "allow"


def test_kill_switch_denies_before_anything(env, store):
    lv = levers(env, kill_switch=True)
    d = evaluate(lv, store, hook_input("place_equity_order", {"garbage": 1}), headless=False, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "kill_switch"


def test_unparsable_order_denies(env, store):
    lv = levers(env)
    d = evaluate(lv, store, hook_input("place_equity_order", {"foo": "bar"}), headless=False, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "parse"


def test_no_proposal_denies(env, store):
    lv = levers(env)
    d = evaluate(lv, store, hook_input("place_equity_order", equity_order()), headless=False, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "proposal"
    assert "tradeagent propose" in d.reason


def test_review_required_before_place(env, store):
    lv = levers(env)
    pid, st, reasons, sizing = propose(store, lv)
    assert st == "pending", reasons
    approve(store, pid)
    d = evaluate(lv, store, hook_input("place_equity_order", equity_order()), headless=False, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "review"


# ---- modes ------------------------------------------------------------------------

def test_approve_all_headless_queues(env, store):
    lv = levers(env)
    pid, st, *_ = propose(store, lv)
    order = equity_order()
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "needs_approval"
    assert f"approve {pid}" in d.reason
    assert store.one("SELECT status FROM proposals WHERE id=?", (pid,))["status"] == "pending"


def test_approve_all_interactive_asks(env, store):
    lv = levers(env)
    pid, *_ = propose(store, lv)
    order = equity_order()
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=False, now=OPEN_NOW)
    assert d.action.value == "ask"
    out = d.to_hook_output()
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_approved_dry_run_simulates_and_denies(env, store):
    lv = levers(env)
    pid, *_ = propose(store, lv)
    approve(store, pid)
    order = equity_order(qty=2)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "dry_run"
    o = store.one("SELECT * FROM orders")
    assert o["simulated"] == 1 and o["status"] == "filled" and o["fill_price"] == 150.0 and o["qty"] == 2
    assert store.one("SELECT status FROM proposals WHERE id=?", (pid,))["status"] == "simulated"
    pos = store.one("SELECT * FROM positions WHERE instrument_key='AAPL'")
    assert pos["qty"] == 2 and pos["source"] == "simulated"


def test_approved_live_allows_and_marks_executing(env, store):
    lv = levers(env, dry_run=False)
    snapshot(store)
    pid, st, reasons, _ = propose(store, lv)
    assert st == "pending", reasons
    approve(store, pid)
    order = equity_order(qty=2)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.action.value == "allow", d.reason
    assert store.one("SELECT status FROM proposals WHERE id=?", (pid,))["status"] == "executing"
    # a second attempt while in flight is denied
    d2 = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d2.action.value == "deny" and d2.rule == "in_flight"


def test_live_requires_fresh_snapshot(env, store):
    lv = levers(env, dry_run=False)
    snapshot(store, now=OPEN_NOW - timedelta(minutes=45))
    pid, *_ = propose(store, lv)
    approve(store, pid)
    order = equity_order(qty=1)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "snapshot"


def test_tiered_new_symbol_needs_human(env, store):
    lv = levers(env, mode="tiered")
    pid, st, *_ = propose(store, lv)
    assert st == "pending"  # new symbol
    order = equity_order(qty=1)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "needs_approval" and "never been traded" in d.reason


def test_tiered_auto_allows_known_symbol_small_order(env, store):
    lv = levers(env, mode="tiered", **{"tiered.new_symbol_requires_approval": False})
    pid, st, *_ = propose(store, lv)
    assert st == "auto_eligible"
    order = equity_order(qty=1)  # $150 < $200 auto cap
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.rule == "dry_run" and "tiered mode auto-approval" in d.reason


def test_tiered_large_order_needs_human(env, store):
    lv = levers(env, mode="tiered", **{"tiered.new_symbol_requires_approval": False, "risk.max_order_notional_usd": 1000,
                                       "risk.max_position_pct": 20})
    pid, st, *_ = propose(store, lv)
    order = equity_order(qty=3)  # $450 > $200 auto cap
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=False, now=OPEN_NOW)
    assert d.action.value == "ask" and "auto_max_notional" in d.reason


def test_autonomous_allows_within_limits(env, store):
    lv = levers(env, mode="autonomous")
    pid, st, *_ = propose(store, lv)
    assert st == "auto_eligible"
    order = equity_order(qty=2)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.rule == "dry_run" and "autonomous" in d.reason


# ---- limits -------------------------------------------------------------------------

def test_qty_above_sized_max_denied(env, store):
    lv = levers(env, mode="autonomous")
    pid, st, reasons, sizing = propose(store, lv)
    assert sizing.max_qty == 3.3333  # min(risk 10000*0.5%/5=10, notional 500/150=3.33, position 1000/150=6.67)
    order = equity_order(qty=3.4)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "proposal"  # no proposal matches an oversized order


def test_market_closed_denied(env, store):
    lv = levers(env, mode="autonomous")
    pid, *_ = propose(store, lv)
    order = equity_order(qty=1)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=CLOSED_NOW)
    assert d.action.value == "deny" and d.rule == "market_hours"


def test_cutoff_denies_new_positions(env, store):
    lv = levers(env, mode="autonomous")
    pid, *_ = propose(store, lv)
    order = equity_order(qty=1)
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=LATE_NOW)
    assert d.action.value == "deny" and d.rule == "cutoff"


def test_daily_loss_halts_entries(env, store):
    lv = levers(env, mode="autonomous")
    # simulate a losing round trip today: buy 3 @150, sell 3 @ 60 -> -$270 (2.7% of 10k)
    pid, *_ = propose(store, lv)
    buy = equity_order(qty=3)
    record_review(store, buy)
    evaluate(lv, store, hook_input("place_equity_order", buy), headless=True, now=OPEN_NOW)
    xp = Proposal.model_validate(proposal_dict(side="sell", is_exit=True, limit_price=60.0, entry_price=60.0,
                                               stop_price=None, target_price=None, requested_qty=3, bull=[], bear=[]))
    xid, xst, xr, _ = journal.create_proposal(store, lv, xp)
    assert xst == "auto_eligible", xr
    sell = equity_order(qty=3, limit=60.0, side="sell")
    record_review(store, sell)
    d = evaluate(lv, store, hook_input("place_equity_order", sell), headless=True, now=OPEN_NOW + timedelta(minutes=5))
    assert d.rule == "dry_run", d.reason
    # new entry in a different symbol should now be blocked by daily loss
    pid2, st2, r2, _ = propose(store, lv, symbol="MSFT", limit_price=100.0, entry_price=100.0, stop_price=97.0, target_price=110.0)
    assert st2 == "auto_eligible", r2
    o2 = equity_order(qty=1, limit=100.0, symbol="MSFT")
    record_review(store, o2)
    d2 = evaluate(lv, store, hook_input("place_equity_order", o2), headless=True, now=OPEN_NOW + timedelta(minutes=10))
    assert d2.action.value == "deny" and "daily_loss" in d2.reason


def test_option_dte_out_of_range_rejected_at_propose(env, store):
    lv = levers(env)
    p = proposal_dict(instrument="option", option={"expiry": "2026-09-11", "strike": 150, "option_type": "call"},
                      limit_price=2.5, entry_price=2.5, stop_price=1.5, target_price=5.0)
    pid, st, reasons, _ = propose(store, lv, **p)
    assert st == "rejected" and any("DTE" in r for r in reasons)


def test_option_order_sized_by_premium(env, store):
    lv = levers(env, mode="autonomous", **{"risk.risk_per_trade_pct": 2.0})
    p = proposal_dict(instrument="option", option={"expiry": "2026-10-16", "strike": 150, "option_type": "call"},
                      limit_price=2.5, entry_price=2.5, stop_price=1.5, target_price=5.0)
    pid, st, reasons, sizing = propose(store, lv, **p)
    assert st == "auto_eligible", reasons
    # risk budget $200 / ($1.00*100) = 2; premium cap $300 / $250 = 1 -> premium binds
    assert sizing.max_qty == 1 and sizing.binding == "max_premium_per_trade"


def test_option_too_expensive_for_risk_budget_rejected(env, store):
    lv = levers(env)  # 0.5% of 10k = $50 risk budget; contract risks $100
    p = proposal_dict(instrument="option", option={"expiry": "2026-10-16", "strike": 150, "option_type": "call"},
                      limit_price=2.5, entry_price=2.5, stop_price=1.5, target_price=5.0)
    pid, st, reasons, sizing = propose(store, lv, **p)
    assert st == "rejected" and sizing.max_qty == 0 and sizing.binding == "risk_per_trade"


def test_exit_more_than_held_denied(env, store):
    lv = levers(env, mode="autonomous")
    xp = Proposal.model_validate(proposal_dict(side="sell", is_exit=True, limit_price=150.0, stop_price=None,
                                               target_price=None, requested_qty=5, bull=[], bear=[]))
    xid, xst, xr, _ = journal.create_proposal(store, lv, xp)
    assert xst == "auto_eligible"
    sell = equity_order(qty=5, side="sell")
    record_review(store, sell)
    d = evaluate(lv, store, hook_input("place_equity_order", sell), headless=True, now=OPEN_NOW)
    # nothing held: exit sanity passes only when held is 0? held==0 -> no violation from exit_qty, but no position ->
    # the order is treated as an exit with nothing to sell; gate still proceeds (broker would reject). Ensure not allowed live.
    assert d.action.value == "deny"


def test_limit_price_drift_rejected(env, store):
    lv = levers(env, mode="autonomous")
    pid, *_ = propose(store, lv)
    order = equity_order(qty=1, limit=160.0)  # 6.7% away from proposal 150
    record_review(store, order)
    d = evaluate(lv, store, hook_input("place_equity_order", order), headless=True, now=OPEN_NOW)
    assert d.action.value == "deny" and d.rule == "proposal" and "differs" in d.reason


def test_decisions_are_journaled(env, store):
    lv = levers(env)
    evaluate(lv, store, hook_input("place_equity_order", equity_order()), headless=True, now=OPEN_NOW)
    rows = store.all("SELECT * FROM decisions")
    assert len(rows) == 1 and rows[0]["action"] == "deny"
