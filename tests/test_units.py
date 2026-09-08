from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone

import pytest

from tradeagent import fieldmap as fm
from tradeagent import marketclock as mc
from tradeagent.models import Instrument, Proposal
from tradeagent.sizing import size_entry
from tradeagent.snapshot import parse_positions, parse_quotes, parse_snapshot

from conftest import TOOL, levers, proposal_dict


# ---- market clock -------------------------------------------------------------------

def test_labor_day_closed():
    assert not mc.is_trading_day(date(2026, 9, 7))
    assert mc.is_trading_day(date(2026, 9, 8))


def test_regular_hours():
    ok, _ = mc.is_open(datetime(2026, 9, 8, 13, 29, tzinfo=timezone.utc))  # 09:29 ET
    assert not ok
    ok, _ = mc.is_open(datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc))
    assert ok
    ok, _ = mc.is_open(datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc))  # 16:00 ET
    assert not ok
    ok, _ = mc.is_open(datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc), "extended")
    assert ok


def test_early_close():
    w = mc.session_window(date(2026, 11, 27))
    assert w.early_close and w.close.hour == 13


# ---- field mapping -------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"symbol": "AAPL", "side": "buy", "quantity": 2, "order_type": "limit", "limit_price": 150},
    {"ticker": "aapl", "direction": "BUY", "qty": "2", "type": "limit", "price": "150.00"},
    {"order": {"symbol": "AAPL", "side": "buy", "shares": 2, "limit_price": 150}},
    {"symbol": "AAPL", "side": "buy_to_open", "quantity": 2.0, "limit_price": 150},
])
def test_parse_equity_variants(payload):
    o = fm.parse_order(TOOL + "place_equity_order", "place_equity_order", payload)
    assert o.symbol == "AAPL" and o.side.value == "buy" and o.qty == 2 and o.limit_price == 150 and o.order_type.value == "limit"


def test_parse_option_leg():
    payload = {"symbol": "AAPL", "side": "buy", "quantity": 1, "expiration_date": "2026-10-16", "strike_price": 150,
               "option_type": "call", "limit_price": 2.5}
    o = fm.parse_order(TOOL + "place_option_order", "place_option_order", payload)
    assert o.instrument == Instrument.option and o.option.key() == "2026-10-16:150:call"
    assert o.notional() == 250.0


def test_parse_option_with_type_meaning_call():
    payload = {"symbol": "AAPL", "side": "buy", "quantity": 1, "expiry": "2026-10-16", "strike": 150, "type": "call",
               "limit_price": 2.5}
    o = fm.parse_order(TOOL + "place_option_order", "place_option_order", payload)
    assert o.option.option_type == "call" and o.order_type.value == "limit"


def test_parse_missing_fields():
    with pytest.raises(ValueError):
        fm.parse_order(TOOL + "place_equity_order", "place_equity_order", {"symbol": "AAPL"})


def test_match_hash_stable_between_review_and_place():
    a = fm.parse_order(TOOL + "review_equity_order", "review_equity_order", {"symbol": "AAPL", "side": "buy", "quantity": 2, "limit_price": 150})
    b = fm.parse_order(TOOL + "place_equity_order", "place_equity_order", {"symbol": "AAPL", "side": "buy", "quantity": 2, "limit_price": 150.0})
    assert a.match_hash() == b.match_hash()


def test_decode_tool_response_text_block_json():
    raw = [{"type": "text", "text": json.dumps({"equity": "12345.67", "buying_power": 1000})}]
    d = fm.decode_tool_response(raw)
    s = parse_snapshot(d)
    assert s.equity == 12345.67 and s.buying_power == 1000


def test_parse_positions_nested():
    d = {"results": [{"symbol": "AAPL", "quantity": "3", "average_buy_price": "150.5", "market_value": 460},
                     {"symbol": "MSFT", "quantity": 1}]}
    rows = parse_positions(d, Instrument.equity)
    assert {r.instrument_key for r in rows} == {"AAPL", "MSFT"}
    assert rows[0].avg_cost == 150.5


def test_parse_quotes_mid():
    d = [{"symbol": "AAPL", "bid_price": "149", "ask_price": "151"}]
    q = parse_quotes(d, Instrument.equity)
    assert q[0].price == 150


def test_response_error_detection():
    assert fm.response_indicates_error({"error": "insufficient buying power"}, None)
    assert fm.response_indicates_error("Error: rejected", None)
    assert not fm.response_indicates_error({"id": "abc", "state": "queued"}, None)


# ---- sizing ------------------------------------------------------------------------------

def test_sizing_equity_binding_notional(env):
    lv = levers(env, **{"risk.fractional_shares": False})
    p = Proposal.model_validate(proposal_dict())
    r = size_entry(lv, p, equity=10000)
    assert r.max_qty == 3 and r.binding == "max_order_notional"


def test_sizing_fractional(env):
    lv = levers(env)  # fractional on by default
    p = Proposal.model_validate(proposal_dict())
    r = size_entry(lv, p, equity=10000)
    assert r.max_qty == 3.3333 and r.binding == "max_order_notional"
    # expensive stock on a tiny account still gets a fraction
    p2 = Proposal.model_validate(proposal_dict(limit_price=1000.0, entry_price=1000.0, stop_price=970.0, target_price=1090.0))
    r2 = size_entry(lv, p2, equity=500)
    assert 0 < r2.max_qty < 1
    # below Robinhood's $1 minimum -> 0
    r3 = size_entry(lv, p2, equity=5)  # 10% position cap = $0.50 < $1 minimum
    assert r3.max_qty == 0 and r3.binding == "min_order_notional"


def test_options_never_fractional(env):
    lv = levers(env, **{"risk.risk_per_trade_pct": 2.0})
    p = Proposal.model_validate(proposal_dict(instrument="option", option={"expiry": "2026-10-16", "strike": 150, "option_type": "call"},
                                              limit_price=2.5, entry_price=2.5, stop_price=1.5, target_price=5.0))
    r = size_entry(lv, p, equity=10000)
    assert r.max_qty == 1 and isinstance(r.max_qty, int)


def test_sizing_risk_binding(env):
    lv = levers(env, **{"risk.max_order_notional_usd": 100000, "risk.max_position_pct": 100})
    p = Proposal.model_validate(proposal_dict())  # risk/share = 5; budget 50 -> 10
    r = size_entry(lv, p, equity=10000)
    assert r.max_qty == 10 and r.binding == "risk_per_trade"


def test_sizing_respects_existing_exposure(env):
    lv = levers(env, **{"risk.max_order_notional_usd": 100000, "risk.fractional_shares": False})
    p = Proposal.model_validate(proposal_dict())
    r = size_entry(lv, p, equity=10000, existing_symbol_value=900)  # room 100 -> 0 shares
    assert r.max_qty == 0 and r.binding == "max_position_pct"


def test_proposal_validation_rules():
    with pytest.raises(Exception):
        Proposal.model_validate(proposal_dict(stop_price=155))  # stop above entry
    with pytest.raises(Exception):
        Proposal.model_validate(proposal_dict(side="sell"))  # opening sell not allowed
    Proposal.model_validate(proposal_dict(side="sell", is_exit=True, stop_price=None, target_price=None, bull=[], bear=[]))


# ---- hook entrypoint (subprocess) ---------------------------------------------------------

def _run_hook(event: str, payload, env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, "-m", "tradeagent.hooks", event], input=data, text=True,
                          capture_output=True, env=env, timeout=60)


def test_hook_malformed_input_blocks(env):
    r = _run_hook("pre-tool-use", "{not json", {})
    assert r.returncode == 2


def test_hook_denies_unknown_order_shape(env):
    payload = {"session_id": "s", "tool_name": TOOL + "place_equity_order", "tool_input": {"x": 1}}
    r = _run_hook("pre-tool-use", payload, {})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_ignores_other_tools(env):
    payload = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    r = _run_hook("pre-tool-use", payload, {})
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_hook_session_start_emits_context(env):
    r = _run_hook("session-start", {"session_id": "s"}, {})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "tradeagent-state" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_post_tool_use_records_snapshot_and_sample(env):
    payload = {"session_id": "s", "tool_use_id": "t", "tool_name": TOOL + "get_portfolio", "tool_input": {},
               "tool_response": [{"type": "text", "text": json.dumps({"equity": 5000, "buying_power": 4000})}]}
    r = _run_hook("post-tool-use", payload, {})
    assert r.returncode == 0
    from tradeagent.db import Store
    s = Store()
    row = s.one("SELECT equity FROM snapshots ORDER BY id DESC LIMIT 1")
    assert row["equity"] == 5000
    assert (env / "data" / "samples" / "get_portfolio.json").exists()


def test_options_own_risk_budget(env):
    lv = levers(env, **{"risk.risk_per_trade_pct": 3.0, "options.risk_per_trade_pct": 6.0, "options.max_premium_per_trade_usd": 50})
    p = Proposal.model_validate(proposal_dict(instrument="option", option={"expiry": "2026-10-16", "strike": 10, "option_type": "call"},
                                              limit_price=0.35, entry_price=0.35, stop_price=0.18, target_price=0.80))
    r = size_entry(lv, p, equity=500)
    assert r.max_qty == 1 and r.binding in ("risk_per_trade", "max_premium_per_trade")
    lv0 = levers(env, **{"risk.risk_per_trade_pct": 3.0, "options.risk_per_trade_pct": 0})
    assert size_entry(lv0, p, equity=500).max_qty == 0


# ---- cash flows ----------------------------------------------------------------------------

def _snap(store, eq, cash, now):
    from tradeagent import journal
    return journal.record_snapshot(store, "s", "get_portfolio", {"total_value": str(eq), "cash": str(cash)}, now=now)


def test_deposit_detected_and_baselines_shift(env, store):
    from datetime import timedelta
    from tradeagent import journal
    from conftest import OPEN_NOW
    lv = levers(env, dry_run=False)
    _snap(store, 500, 500, OPEN_NOW)
    r = _snap(store, 1000, 1000, OPEN_NOW + timedelta(minutes=30))
    assert r["cashflow_detected"] == 500
    s = journal.state_view(store, lv, OPEN_NOW + timedelta(minutes=31))
    assert s.day_open_equity == 1000 and s.week_open_equity == 1000 and s.pnl_today == 0
    _snap(store, 980, 980, OPEN_NOW + timedelta(minutes=60))
    s = journal.state_view(store, lv, OPEN_NOW + timedelta(minutes=61))
    assert s.pnl_today == -20


def test_withdrawal_not_read_as_loss(env, store):
    from datetime import timedelta
    from tradeagent import journal
    from conftest import OPEN_NOW
    lv = levers(env, dry_run=False)
    _snap(store, 1000, 1000, OPEN_NOW)
    _snap(store, 700, 700, OPEN_NOW + timedelta(minutes=30))
    s = journal.state_view(store, lv, OPEN_NOW + timedelta(minutes=31))
    assert s.day_open_equity == 700 and s.pnl_today == 0


def test_trade_or_price_move_is_not_cashflow(env, store):
    from datetime import timedelta
    from tradeagent import journal
    from conftest import OPEN_NOW
    lv = levers(env, dry_run=False)
    _snap(store, 500, 500, OPEN_NOW)
    r = _snap(store, 500, 200, OPEN_NOW + timedelta(minutes=10))   # bought $300 of stock
    assert r["cashflow_detected"] is None
    r = _snap(store, 440, 200, OPEN_NOW + timedelta(minutes=20))   # stock fell
    assert r["cashflow_detected"] is None
    s = journal.state_view(store, lv, OPEN_NOW + timedelta(minutes=21))
    assert s.pnl_today == -60


def test_manual_cashflow(env, store):
    from datetime import timedelta
    from tradeagent import journal
    from conftest import OPEN_NOW
    lv = levers(env, dry_run=False)
    _snap(store, 500, 500, OPEN_NOW)
    journal.apply_cashflow(store, 250, source="manual", ts=OPEN_NOW + timedelta(minutes=5))
    s = journal.state_view(store, lv, OPEN_NOW + timedelta(minutes=6))
    assert s.day_open_equity == 750


def test_real_portfolio_shape_parses():
    from tradeagent.snapshot import parse_snapshot
    d = {"data": {"total_value": "500", "equity_value": "0", "cash": "500", "pending_deposits": "0",
                  "buying_power": {"buying_power": "500.0000", "unleveraged_buying_power": "500.0000"}}, "guide": "..."}
    s = parse_snapshot(d)
    assert s.equity == 500 and s.cash == 500 and s.buying_power == 500 and s.pending_deposits == 0


def test_dollar_based_order_parses_with_limit():
    o = fm.parse_order(TOOL + "place_equity_order", "place_equity_order",
                       {"symbol": "AAPL", "side": "buy", "dollar_based_amount": 150, "type": "limit", "price": 300})
    assert o.qty == 0.5 and o.dollar_amount == 150
    with pytest.raises(ValueError):
        fm.parse_order(TOOL + "place_equity_order", "place_equity_order",
                       {"symbol": "AAPL", "side": "buy", "dollar_based_amount": 150, "type": "market"})


def test_option_position_without_strike_kept():
    d = {"data": {"positions": [{"chain_symbol": "SOFI", "type": "long", "quantity": "1", "average_price": "35.00",
                                 "expiration_date": "2026-10-16", "option_id": "abc-123"}]}}
    rows = parse_positions(d, Instrument.option)
    assert len(rows) == 1 and rows[0].instrument_key == "SOFI:opt:abc-123" and rows[0].market_value == 3500.0
