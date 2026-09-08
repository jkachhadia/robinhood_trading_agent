"""All writes to the store, plus the StateView the gate reads."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from . import fieldmap as fm
from . import marketclock as mc
from .config import Levers
from .db import Store, dumps, now_iso
from .limits import Position, StateView
from .models import Instrument, ParsedOrder, Proposal, ProposalStatus, Side
from .pnl import Fill, build_book
from .sizing import SizingResult, size_entry
from .snapshot import parse_order_response, parse_positions, parse_quotes, parse_snapshot


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


# ---- raw + decisions ---------------------------------------------------------

def record_raw(store: Store, session_id: Optional[str], tool_name: str, tool_use_id: Optional[str],
               tool_input: Any, tool_response: Any, ok: bool = True) -> int:
    return store.insert("raw_responses", {
        "ts": now_iso(), "session_id": session_id, "tool_name": tool_name, "tool_use_id": tool_use_id,
        "ok": 1 if ok else 0, "input_json": dumps(tool_input), "response_json": dumps(tool_response),
    })


def record_decision(store: Store, levers: Levers, session_id: Optional[str], headless: bool, tool_name: str,
                    tool_use_id: Optional[str], action: str, rule: Optional[str], reason: str,
                    proposal_id: Optional[int], input_hash: Optional[str], tool_input: Any) -> int:
    return store.insert("decisions", {
        "ts": now_iso(), "session_id": session_id, "headless": 1 if headless else 0, "tool_name": tool_name,
        "tool_use_id": tool_use_id, "action": action, "rule": rule, "reason": reason, "proposal_id": proposal_id,
        "input_hash": input_hash, "input_json": dumps(tool_input), "mode": levers.mode.value,
        "dry_run": 1 if levers.dry_run else 0,
    })


# ---- snapshots / positions / quotes -------------------------------------------

CASHFLOW_MIN_ABS = 20.0        # ignore cash/equity co-movements smaller than this
CASHFLOW_MIN_PCT = 0.05        # ...or smaller than 5% of equity
CASHFLOW_MATCH_TOL = 0.10      # |Δequity - Δcash| must be within 10% of |Δcash| (or $5)


def record_snapshot(store: Store, session_id: Optional[str], kind: str, decoded: Any, now: Optional[datetime] = None) -> dict:
    snap = parse_snapshot(decoded)
    ts = _utc(now)
    ts_iso = ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    tdate = mc.trading_date(ts).isoformat()
    detected = None
    if snap.equity is not None and snap.cash is not None:
        detected = _detect_cashflow(store, snap.equity, snap.cash, ts, tdate)
    store.insert("snapshots", {
        "ts": ts_iso, "trading_date": tdate,
        "session_id": session_id, "kind": kind, "equity": snap.equity, "buying_power": snap.buying_power,
        "cash": snap.cash, "pending_deposits": snap.pending_deposits, "payload": dumps(decoded),
    })
    if snap.equity is not None:
        ensure_daily(store, tdate, snap.equity, ts)
    return {"equity": snap.equity, "buying_power": snap.buying_power, "cash": snap.cash,
            "pending_deposits": snap.pending_deposits, "cashflow_detected": detected}


def _detect_cashflow(store: Store, equity: float, cash: float, ts: datetime, tdate: str) -> Optional[float]:
    """A deposit/withdrawal moves cash and equity by the same amount with no fills in between.
    A trade moves cash but not equity; a price move moves equity but not cash."""
    prev = store.one("SELECT ts, equity, cash FROM snapshots WHERE equity IS NOT NULL AND cash IS NOT NULL ORDER BY id DESC LIMIT 1")
    if prev is None:
        return None
    d_eq = equity - float(prev["equity"])
    d_cash = cash - float(prev["cash"])
    threshold = max(CASHFLOW_MIN_ABS, CASHFLOW_MIN_PCT * float(prev["equity"]))
    if abs(d_cash) < threshold:
        return None
    if abs(d_eq - d_cash) > max(5.0, CASHFLOW_MATCH_TOL * abs(d_cash)):
        return None
    fills = store.one("SELECT COUNT(*) AS n FROM orders WHERE simulated=0 AND status IN ('placed','filled') AND ts > ? AND ts <= ?",
                      (prev["ts"], ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")))["n"]
    if fills:
        return None
    apply_cashflow(store, d_cash, source="auto", note=f"equity {prev['equity']}->{equity}, cash {prev['cash']}->{cash}", ts=ts)
    return d_cash


def apply_cashflow(store: Store, amount: float, source: str = "manual", note: Optional[str] = None,
                   ts: Optional[datetime] = None) -> None:
    """Shift today's and this week's P&L baselines so a deposit/withdrawal is not read as P&L."""
    ts = _utc(ts)
    tdate = mc.trading_date(ts).isoformat()
    wk = mc.week_start(mc.trading_date(ts)).isoformat()
    store.insert("cashflows", {"ts": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "trading_date": tdate,
                               "amount": amount, "source": source, "note": note})
    row = store.one("SELECT day_open_equity FROM daily WHERE trading_date=?", (tdate,))
    if row is not None and row["day_open_equity"] is not None:
        store.conn.execute("UPDATE daily SET day_open_equity=day_open_equity+?, updated_at=? WHERE trading_date=?",
                           (amount, now_iso(), tdate))
    wo = store.kv_get(f"week_open:{wk}")
    if wo is not None:
        store.kv_set(f"week_open:{wk}", float(wo) + amount)
    store.conn.execute("UPDATE daily SET week_open_equity=week_open_equity+? WHERE trading_date=? AND week_open_equity IS NOT NULL",
                       (amount, tdate))


def cashflows_today(store: Store, tdate: Optional[str] = None) -> list:
    tdate = tdate or mc.trading_date().isoformat()
    return store.all("SELECT * FROM cashflows WHERE trading_date=? ORDER BY id", (tdate,))


def ensure_daily(store: Store, tdate: str, equity: float, ts: datetime) -> None:
    row = store.one("SELECT * FROM daily WHERE trading_date=?", (tdate,))
    wk = mc.week_start(datetime.fromisoformat(tdate).date()).isoformat()
    week_open = store.kv_get(f"week_open:{wk}")
    if week_open is None:
        store.kv_set(f"week_open:{wk}", equity)
        week_open = equity
    if row is None:
        store.insert("daily", {"trading_date": tdate, "day_open_equity": equity, "week_open_equity": week_open,
                               "halted_reason": None, "updated_at": now_iso()})
    elif row["day_open_equity"] is None:
        store.conn.execute("UPDATE daily SET day_open_equity=?, week_open_equity=?, updated_at=? WHERE trading_date=?",
                           (equity, week_open, now_iso(), tdate))


def record_positions(store: Store, decoded: Any, instrument: Instrument, source: str = "broker") -> int:
    rows = parse_positions(decoded, instrument)
    with store.tx():
        store.conn.execute("DELETE FROM positions WHERE instrument=? AND source=?", (instrument.value, source))
        for p in rows:
            store.upsert("positions", {
                "instrument_key": p.instrument_key, "symbol": p.symbol, "instrument": p.instrument.value,
                "qty": p.qty, "avg_cost": p.avg_cost, "market_value": p.market_value, "updated_at": now_iso(),
                "source": source, "payload": dumps(p.raw),
            }, "instrument_key")
    return len(rows)


def record_quotes(store: Store, decoded: Any, instrument: Instrument) -> int:
    rows = parse_quotes(decoded, instrument)
    for q in rows:
        store.upsert("quotes", {
            "instrument_key": q.instrument_key, "symbol": q.symbol, "price": q.price, "bid": q.bid, "ask": q.ask,
            "ts": now_iso(), "payload": dumps(q.raw),
        }, "instrument_key")
    return len(rows)


def latest_quote(store: Store, instrument_key: str) -> Optional[float]:
    r = store.one("SELECT price FROM quotes WHERE instrument_key=?", (instrument_key,))
    return float(r["price"]) if r and r["price"] is not None else None


# ---- reviews ------------------------------------------------------------------

def record_review(store: Store, session_id: Optional[str], tool_name: str, match_hash: Optional[str],
                  tool_input: Any, response: Any) -> int:
    return store.insert("reviews", {
        "ts": now_iso(), "session_id": session_id, "tool_name": tool_name, "match_hash": match_hash,
        "input_json": dumps(tool_input), "response_json": dumps(response),
    })


def recent_review(store: Store, match_hash: str, within_minutes: int = 30, session_id: Optional[str] = None):
    rows = store.all("SELECT * FROM reviews WHERE match_hash=? ORDER BY id DESC LIMIT 5", (match_hash,))
    now = datetime.now(timezone.utc)
    for r in rows:
        ts = _parse_ts(r["ts"])
        if ts and (now - ts).total_seconds() <= within_minutes * 60:
            if session_id is None or r["session_id"] == session_id or r["session_id"] is None:
                return r
    return None


# ---- proposals ------------------------------------------------------------------

def _state_for_sizing(store: Store, levers: Levers) -> StateView:
    return state_view(store, levers)


def create_proposal(store: Store, levers: Levers, p: Proposal, session_id: Optional[str] = None) -> tuple[int, str, list[str], Optional[SizingResult]]:
    """Validate at propose time, size it, store it. Returns (id, status, reasons, sizing)."""
    reasons: list[str] = []
    sizing: Optional[SizingResult] = None
    state = _state_for_sizing(store, levers)
    equity = state.equity if state.equity is not None else (levers.paper_equity_usd if levers.dry_run else None)

    if not p.is_exit:
        rr = p.reward_risk
        if rr is None or rr < levers.risk.min_reward_risk:
            reasons.append(f"reward:risk {rr if rr is None else round(rr, 2)} < min {levers.risk.min_reward_risk}")
        if equity is None:
            reasons.append("no equity known: call get_portfolio first (or enable dry_run)")
        else:
            sizing = size_entry(
                levers, p, equity,
                existing_symbol_value=state.symbol_value(p.symbol),
                existing_options_value=state.options_value(),
                buying_power=state.buying_power if not levers.dry_run else None,
            )
            if sizing.max_qty <= 0:
                reasons.append(f"sized to 0 ({sizing.binding} cap); {sizing.note()}")
        if p.instrument == Instrument.option and not levers.options.enabled:
            reasons.append("options disabled")
        u = levers.universe
        if u.blocklist and p.symbol in u.blocklist:
            reasons.append(f"{p.symbol} on blocklist")
        if u.allowlist and p.symbol not in u.allowlist:
            reasons.append(f"{p.symbol} not on allowlist")
        if p.instrument == Instrument.equity and p.entry_price < u.min_price:
            reasons.append(f"price {p.entry_price:g} < min_price {u.min_price}")
        if p.earnings_date and "earnings_play" not in p.tags:
            d = (p.earnings_date - datetime.now(timezone.utc).date()).days
            if 0 <= d <= u.exclude_earnings_within_days:
                reasons.append(f"earnings in {d} days; tag earnings_play to override")
        if p.instrument == Instrument.option and p.option:
            dte = (p.option.expiry - datetime.now(timezone.utc).date()).days
            if dte < levers.options.dte_min or dte > levers.options.dte_max:
                reasons.append(f"DTE {dte} outside [{levers.options.dte_min}, {levers.options.dte_max}]")
    else:
        held = state.position_qty(p.instrument_key())
        if p.requested_qty and held and p.requested_qty > held:
            reasons.append(f"exit qty {p.requested_qty} > held {held:g}")

    status = ProposalStatus.rejected if reasons else ProposalStatus.pending
    if not reasons:
        if p.is_exit and levers.mode != levers.mode.approve_all and levers.tiered.exits_auto_allowed:
            status = ProposalStatus.auto_eligible
        elif levers.mode == levers.mode.autonomous:
            status = ProposalStatus.auto_eligible
        elif levers.mode == levers.mode.tiered and _tiered_auto_ok(levers, p, sizing, state):
            status = ProposalStatus.auto_eligible

    ts = now_iso()
    ttl = levers.session.proposal_ttl_hours
    expires = (datetime.now(timezone.utc).replace(microsecond=0) + __import__("datetime").timedelta(hours=ttl)).isoformat().replace("+00:00", "Z")
    max_qty = None
    max_notional = None
    if p.is_exit:
        held = state.position_qty(p.instrument_key())
        max_qty = float(p.requested_qty) if p.requested_qty else (held or None)
    elif sizing:
        max_qty = float(sizing.max_qty)
        max_notional = sizing.max_notional
    pid = store.insert("proposals", {
        "created_at": ts, "updated_at": ts, "expires_at": expires, "status": status.value,
        "symbol": p.symbol, "instrument": p.instrument.value, "instrument_key": p.instrument_key(),
        "side": p.side.value, "is_exit": 1 if p.is_exit else 0, "horizon": p.horizon.value,
        "confidence": p.confidence, "reward_risk": p.reward_risk, "max_qty": max_qty, "max_notional": max_notional,
        "sizing_note": sizing.note() if sizing else None, "approval_source": None, "approved_at": None,
        "rejection_reasons": json.dumps(reasons) if reasons else None, "session_id": session_id,
        "mode_at_creation": levers.mode.value, "payload": p.model_dump_json(),
    })
    return pid, status.value, reasons, sizing


def _tiered_auto_ok(levers: Levers, p: Proposal, sizing: Optional[SizingResult], state: StateView) -> bool:
    t = levers.tiered
    if p.confidence < t.auto_min_confidence:
        return False
    if p.instrument == Instrument.option and t.options_require_approval:
        return False
    if t.new_symbol_requires_approval and p.symbol not in state.traded_symbols:
        return False
    if sizing and sizing.max_notional > t.auto_max_notional_usd:
        # still auto-eligible if the model sizes down; the gate re-checks actual notional
        pass
    return True


def load_proposal(store: Store, pid: int) -> tuple[Optional[Any], Optional[Proposal]]:
    row = store.one("SELECT * FROM proposals WHERE id=?", (pid,))
    if row is None:
        return None, None
    return row, Proposal.model_validate_json(row["payload"])


def set_proposal_status(store: Store, pid: int, status: ProposalStatus, **extra) -> None:
    store.update("proposals", pid, status=status.value, updated_at=now_iso(), **extra)


def expire_proposals(store: Store) -> int:
    now = now_iso()
    cur = store.conn.execute(
        "UPDATE proposals SET status=?, updated_at=? WHERE status IN (?, ?, ?) AND expires_at IS NOT NULL AND expires_at < ?",
        (ProposalStatus.expired.value, now, ProposalStatus.pending.value, ProposalStatus.approved.value,
         ProposalStatus.auto_eligible.value, now),
    )
    return cur.rowcount


def candidate_proposals(store: Store, instrument_key: str, side: Side):
    expire_proposals(store)
    return store.all(
        "SELECT * FROM proposals WHERE instrument_key=? AND side=? AND status IN (?, ?, ?, ?) "
        "ORDER BY CASE status WHEN 'approved' THEN 0 WHEN 'auto_eligible' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END, id DESC",
        (instrument_key, side.value, ProposalStatus.approved.value, ProposalStatus.auto_eligible.value,
         ProposalStatus.pending.value, ProposalStatus.executing.value),
    )


# ---- orders ---------------------------------------------------------------------

def record_order(store: Store, session_id: Optional[str], tool_name: str, tool_use_id: Optional[str],
                 order: ParsedOrder, tool_input: Any, response: Any, decoded: Any,
                 proposal_id: Optional[int], is_exit: bool, now: Optional[datetime] = None) -> tuple[int, str]:
    parsed = parse_order_response(decoded, response)
    status = "failed" if parsed.is_error else ("filled" if (parsed.state or "").lower() == "filled" else "placed")
    ts = _utc(now)
    oid = store.insert("orders", {
        "ts": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "trading_date": mc.trading_date(ts).isoformat(),
        "session_id": session_id, "proposal_id": proposal_id, "tool_name": tool_name, "tool_use_id": tool_use_id,
        "symbol": order.symbol, "instrument": order.instrument.value, "instrument_key": order.instrument_key(),
        "side": order.side.value, "is_exit": 1 if is_exit else 0, "qty": order.qty, "order_type": order.order_type.value,
        "limit_price": order.limit_price, "fill_price": parsed.fill_price, "notional": order.notional(parsed.fill_price),
        "broker_order_id": parsed.broker_order_id, "status": status, "simulated": 0,
        "input_json": dumps(tool_input), "response_json": dumps(response),
    })
    if proposal_id:
        row = store.one("SELECT status, approval_source FROM proposals WHERE id=?", (proposal_id,))
        extra = {}
        if row is not None and row["status"] == ProposalStatus.pending.value and not row["approval_source"]:
            extra = {"approval_source": "prompt", "approved_at": now_iso()}
        set_proposal_status(store, proposal_id, ProposalStatus.failed if parsed.is_error else ProposalStatus.placed, **extra)
    return oid, status


def record_order_failure(store: Store, session_id: Optional[str], tool_name: str, tool_use_id: Optional[str],
                         order: Optional[ParsedOrder], tool_input: Any, error: Any, proposal_id: Optional[int]) -> None:
    if order is not None:
        ts = _utc()
        store.insert("orders", {
            "ts": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "trading_date": mc.trading_date(ts).isoformat(),
            "session_id": session_id, "proposal_id": proposal_id, "tool_name": tool_name, "tool_use_id": tool_use_id,
            "symbol": order.symbol, "instrument": order.instrument.value, "instrument_key": order.instrument_key(),
            "side": order.side.value, "is_exit": 0, "qty": order.qty, "order_type": order.order_type.value,
            "limit_price": order.limit_price, "fill_price": None, "notional": None, "broker_order_id": None,
            "status": "failed", "simulated": 0, "input_json": dumps(tool_input), "response_json": dumps(error),
        })
    if proposal_id:
        row = store.one("SELECT status FROM proposals WHERE id=?", (proposal_id,))
        if row and row["status"] == ProposalStatus.executing.value:
            set_proposal_status(store, proposal_id, ProposalStatus.approved)


def record_simulated_fill(store: Store, session_id: Optional[str], tool_name: str, tool_use_id: Optional[str],
                          order: ParsedOrder, tool_input: Any, proposal_id: Optional[int], is_exit: bool,
                          fill_price: float, now: Optional[datetime] = None) -> int:
    ts = _utc(now)
    oid = store.insert("orders", {
        "ts": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"), "trading_date": mc.trading_date(ts).isoformat(),
        "session_id": session_id, "proposal_id": proposal_id, "tool_name": tool_name, "tool_use_id": tool_use_id,
        "symbol": order.symbol, "instrument": order.instrument.value, "instrument_key": order.instrument_key(),
        "side": order.side.value, "is_exit": 1 if is_exit else 0, "qty": order.qty, "order_type": order.order_type.value,
        "limit_price": order.limit_price, "fill_price": fill_price, "notional": fill_price * order.qty * order.multiplier,
        "broker_order_id": f"SIM-{ts.strftime('%Y%m%d%H%M%S')}", "status": "filled", "simulated": 1,
        "input_json": dumps(tool_input), "response_json": None,
    })
    rebuild_simulated_positions(store)
    if proposal_id:
        set_proposal_status(store, proposal_id, ProposalStatus.simulated)
    return oid


def simulated_fills(store: Store) -> list[Fill]:
    rows = store.all("SELECT * FROM orders WHERE simulated=1 AND status='filled' ORDER BY ts, id")
    return [_fill_from_row(r) for r in rows]


def real_fills(store: Store) -> list[Fill]:
    rows = store.all("SELECT * FROM orders WHERE simulated=0 AND status IN ('placed','filled') ORDER BY ts, id")
    out = []
    for r in rows:
        f = _fill_from_row(r)
        if f.price is not None:
            out.append(f)
    return out


def _fill_from_row(r) -> Fill:
    price = r["fill_price"] if r["fill_price"] is not None else r["limit_price"]
    return Fill(
        order_id=r["id"], ts=r["ts"], trading_date=r["trading_date"], instrument_key=r["instrument_key"],
        symbol=r["symbol"], instrument=Instrument(r["instrument"]), side=Side(r["side"]), qty=float(r["qty"]),
        price=float(price) if price is not None else None, proposal_id=r["proposal_id"], is_exit=bool(r["is_exit"]),
    )


def rebuild_simulated_positions(store: Store) -> None:
    book = build_book(simulated_fills(store))
    with store.tx():
        store.conn.execute("DELETE FROM positions WHERE source='simulated'")
        for key, (qty, avg) in book.positions().items():
            symbol = key.split(":")[0]
            instrument = Instrument.option if ":" in key else Instrument.equity
            px = latest_quote(store, key) or avg
            mult = 100 if instrument == Instrument.option else 1
            store.upsert("positions", {
                "instrument_key": key, "symbol": symbol, "instrument": instrument.value, "qty": qty, "avg_cost": avg,
                "market_value": px * qty * mult, "updated_at": now_iso(), "source": "simulated", "payload": None,
            }, "instrument_key")


# ---- runs -------------------------------------------------------------------------

def start_run(store: Store, session_id: Optional[str], kind: Optional[str], headless: bool, mode: str) -> None:
    if not session_id:
        return
    store.upsert("runs", {"session_id": session_id, "started_at": now_iso(), "ended_at": None, "kind": kind,
                          "headless": 1 if headless else 0, "mode": mode, "summary": None}, "session_id")


def end_run(store: Store, session_id: Optional[str], summary: Optional[str] = None) -> None:
    if not session_id:
        return
    store.conn.execute("UPDATE runs SET ended_at=?, summary=COALESCE(?, summary) WHERE session_id=?",
                       (now_iso(), summary, session_id))


# ---- state view -------------------------------------------------------------------

def state_view(store: Store, levers: Levers, now: Optional[datetime] = None) -> StateView:
    now = _utc(now)
    tdate = mc.trading_date(now).isoformat()
    wk = mc.week_start(mc.trading_date(now)).isoformat()

    snap = store.one("SELECT * FROM snapshots WHERE equity IS NOT NULL ORDER BY id DESC LIMIT 1")
    equity = float(snap["equity"]) if snap else None
    buying_power = float(snap["buying_power"]) if snap and snap["buying_power"] is not None else None
    snapshot_ts = _parse_ts(snap["ts"]) if snap else None

    daily = store.one("SELECT * FROM daily WHERE trading_date=?", (tdate,))
    day_open = float(daily["day_open_equity"]) if daily and daily["day_open_equity"] is not None else None
    week_open = store.kv_get(f"week_open:{wk}")
    halted = daily["halted_reason"] if daily else None

    quotes = {r["instrument_key"]: float(r["price"]) for r in store.all("SELECT instrument_key, price FROM quotes WHERE price IS NOT NULL")}

    source = "simulated" if levers.dry_run else "broker"
    positions: dict[str, Position] = {}
    for r in store.all("SELECT * FROM positions WHERE source=?", (source,)):
        positions[r["instrument_key"]] = Position(
            instrument_key=r["instrument_key"], symbol=r["symbol"], instrument=Instrument(r["instrument"]),
            qty=float(r["qty"] or 0), avg_cost=r["avg_cost"], market_value=r["market_value"],
        )

    sim_flag = 1 if levers.dry_run else 0
    trades_today = store.one(
        "SELECT COUNT(*) AS n FROM orders WHERE trading_date=? AND is_exit=0 AND simulated=? AND status IN ('placed','filled')",
        (tdate, sim_flag),
    )["n"]
    last_order_ts: dict[str, datetime] = {}
    for r in store.all("SELECT symbol, MAX(ts) AS ts FROM orders WHERE simulated=? AND status IN ('placed','filled') GROUP BY symbol", (sim_flag,)):
        t = _parse_ts(r["ts"])
        if t:
            last_order_ts[r["symbol"]] = t
    traded = {r["symbol"] for r in store.all("SELECT DISTINCT symbol FROM orders WHERE status IN ('placed','filled')")}
    traded |= {p.symbol for p in positions.values()}

    if levers.dry_run:
        book = build_book(simulated_fills(store))
        pnl_today = book.realized(on_date=tdate) + book.unrealized(quotes)
        pnl_week = book.realized(since=wk) + book.unrealized(quotes)
        if equity is None:
            equity = levers.paper_equity_usd
        if day_open is None:
            day_open = equity
        if week_open is None:
            week_open = equity
    else:
        pnl_today = (equity - day_open) if (equity is not None and day_open) else None
        pnl_week = (equity - float(week_open)) if (equity is not None and week_open) else None

    return StateView(
        now=now, equity=equity, buying_power=buying_power, snapshot_ts=snapshot_ts,
        day_open_equity=day_open, week_open_equity=float(week_open) if week_open else None,
        positions=positions, trades_today=int(trades_today), last_order_ts=last_order_ts,
        traded_symbols=traded, quotes=quotes, pnl_today=pnl_today, pnl_week=pnl_week,
        halted_reason=halted, simulated=levers.dry_run,
    )


# ---- symbol notes ------------------------------------------------------------------

def add_note(store: Store, symbol: str, note: str, source: str = "agent") -> int:
    return store.insert("symbol_notes", {"ts": now_iso(), "symbol": symbol.strip().upper(), "note": note.strip(), "source": source})


def notes_for(store: Store, symbol: str, limit: int = 10):
    return store.all("SELECT * FROM symbol_notes WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol.strip().upper(), limit))


# ---- in-flight summary (survives context compaction because it comes from the DB) ----

def inflight_lines(store: Store, levers: Levers) -> list[str]:
    out: list[str] = []
    expire_proposals(store)
    for r in store.all("SELECT id, status, symbol, side, instrument_key, max_qty FROM proposals "
                       "WHERE status IN ('executing','approved','auto_eligible') ORDER BY id"):
        mq = f" max_qty {float(r['max_qty']):g}" if r["max_qty"] is not None else ""
        out.append(f"proposal #{r['id']} {r['status']}: {r['side']} {r['instrument_key']}{mq}")
    td = mc.trading_date().isoformat()
    sim = 1 if levers.dry_run else 0
    for r in store.all("SELECT id, side, qty, instrument_key, status, broker_order_id FROM orders "
                       "WHERE trading_date=? AND simulated=? ORDER BY id DESC LIMIT 8", (td, sim)):
        out.append(f"order #{r['id']} {r['side']} {float(r['qty']):g} {r['instrument_key']} -> {r['status']} ({r['broker_order_id'] or 'no id'})")
    done = store.kv_get(f"cycle:{td}", {}) or {}
    if done:
        out.append("cycle steps done today: " + ", ".join(sorted(done.keys())))
    return out
