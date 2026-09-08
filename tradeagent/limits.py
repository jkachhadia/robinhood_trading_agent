"""Hard limits. Each check returns a Violation or None. Exits skip most of them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import marketclock as mc
from .config import Levers
from .models import Instrument, ParsedOrder, Proposal, Side


@dataclass
class Position:
    instrument_key: str
    symbol: str
    instrument: Instrument
    qty: float
    avg_cost: Optional[float]
    market_value: Optional[float]


@dataclass
class StateView:
    """Everything the gate knows about the account at decision time."""

    now: datetime
    equity: Optional[float]
    buying_power: Optional[float]
    snapshot_ts: Optional[datetime]
    day_open_equity: Optional[float]
    week_open_equity: Optional[float]
    positions: dict[str, Position] = field(default_factory=dict)
    trades_today: int = 0
    last_order_ts: dict[str, datetime] = field(default_factory=dict)   # by symbol
    traded_symbols: set[str] = field(default_factory=set)
    quotes: dict[str, float] = field(default_factory=dict)              # instrument_key -> price
    pnl_today: Optional[float] = None       # dollars, realized + unrealized
    pnl_week: Optional[float] = None
    halted_reason: Optional[str] = None
    simulated: bool = False

    @property
    def snapshot_age_minutes(self) -> Optional[float]:
        if self.snapshot_ts is None:
            return None
        return (self.now - self.snapshot_ts).total_seconds() / 60.0

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.qty and abs(p.qty) > 0]

    def symbol_value(self, symbol: str) -> float:
        total = 0.0
        for p in self.positions.values():
            if p.symbol == symbol and p.qty:
                total += self._pos_value(p)
        return total

    def options_value(self) -> float:
        return sum(self._pos_value(p) for p in self.positions.values() if p.instrument == Instrument.option and p.qty)

    def _pos_value(self, p: Position) -> float:
        if p.market_value is not None:
            return p.market_value
        px = self.quotes.get(p.instrument_key) or p.avg_cost or 0.0
        mult = 100 if p.instrument == Instrument.option else 1
        return px * p.qty * mult

    def position_qty(self, instrument_key: str) -> float:
        p = self.positions.get(instrument_key)
        return p.qty if p else 0.0


@dataclass
class Violation:
    rule: str
    message: str


@dataclass
class GateContext:
    levers: Levers
    order: ParsedOrder
    state: StateView
    proposal: Optional[Proposal] = None
    proposal_max_qty: Optional[float] = None
    is_exit: bool = False
    reference_price: Optional[float] = None   # best known price when the order has no limit

    @property
    def price(self) -> Optional[float]:
        if self.order.limit_price is not None:
            return self.order.limit_price
        if self.reference_price is not None:
            return self.reference_price
        if self.proposal is not None:
            return self.proposal.entry_price
        return None

    @property
    def notional(self) -> Optional[float]:
        px = self.price
        return None if px is None else px * self.order.qty * self.order.multiplier

    @property
    def equity(self) -> Optional[float]:
        if self.state.equity is not None:
            return self.state.equity
        if self.levers.dry_run:
            return self.levers.paper_equity_usd
        return None


Check = Callable[[GateContext], Optional[Violation]]


# ---- checks that apply to everything ---------------------------------------

def check_kill_switch(ctx: GateContext) -> Optional[Violation]:
    if ctx.levers.kill_switch:
        return Violation("kill_switch", "kill switch is ON; run `tradeagent kill off` to resume")
    return None


def check_market_hours(ctx: GateContext) -> Optional[Violation]:
    extra = {date.fromisoformat(d) for d in ctx.levers.market.extra_holidays}
    ok, why = mc.is_open(ctx.state.now, ctx.levers.session.allowed_hours.value, extra)
    if not ok:
        return Violation("market_hours", f"market closed: {why}")
    return None


def check_exit_sanity(ctx: GateContext) -> Optional[Violation]:
    if not ctx.is_exit:
        return None
    held = ctx.state.position_qty(ctx.order.instrument_key())
    if held and ctx.order.qty > held + 1e-9:
        return Violation("exit_qty", f"selling {ctx.order.qty:g} but only {held:g} held in {ctx.order.instrument_key()}")
    return None


# ---- entry-only checks ------------------------------------------------------

def check_halted(ctx: GateContext) -> Optional[Violation]:
    if ctx.state.halted_reason:
        return Violation("halted", f"entries halted for today: {ctx.state.halted_reason}")
    return None


def check_cutoff(ctx: GateContext) -> Optional[Violation]:
    cutoff = ctx.levers.session.no_new_positions_after
    if cutoff and mc.past_cutoff(cutoff, ctx.state.now):
        return Violation("cutoff", f"no new positions after {cutoff} ET")
    return None


def check_snapshot_fresh(ctx: GateContext) -> Optional[Violation]:
    if ctx.levers.dry_run:
        return None
    age = ctx.state.snapshot_age_minutes
    limit = ctx.levers.session.require_fresh_snapshot_minutes
    if age is None:
        return Violation("snapshot", "no portfolio snapshot recorded; call get_portfolio (and get_*_positions) first")
    if age > limit:
        return Violation("snapshot", f"portfolio snapshot is {age:.0f} min old (> {limit}); call get_portfolio again")
    return None


def check_equity_floor(ctx: GateContext) -> Optional[Violation]:
    floor = ctx.levers.risk.equity_floor_usd
    eq = ctx.equity
    if floor and eq is not None and eq < floor:
        return Violation("equity_floor", f"equity {eq:,.0f} below floor {floor:,.0f}")
    return None


def check_daily_loss(ctx: GateContext) -> Optional[Violation]:
    base = ctx.state.day_open_equity or ctx.equity
    pnl = ctx.state.pnl_today
    if base and pnl is not None:
        pct = pnl / base * 100
        if pct <= -ctx.levers.risk.max_daily_loss_pct:
            return Violation("daily_loss", f"day P&L {pct:.2f}% breaches -{ctx.levers.risk.max_daily_loss_pct}% limit")
    return None


def check_weekly_loss(ctx: GateContext) -> Optional[Violation]:
    base = ctx.state.week_open_equity or ctx.equity
    pnl = ctx.state.pnl_week
    if base and pnl is not None:
        pct = pnl / base * 100
        if pct <= -ctx.levers.risk.max_weekly_loss_pct:
            return Violation("weekly_loss", f"week P&L {pct:.2f}% breaches -{ctx.levers.risk.max_weekly_loss_pct}% limit")
    return None


def check_notional(ctx: GateContext) -> Optional[Violation]:
    n = ctx.notional
    if n is None:
        return Violation("notional", "cannot compute order notional (no limit price, quote, or proposal entry)")
    cap = ctx.levers.risk.max_order_notional_usd
    if n > cap + 1e-6:
        return Violation("notional", f"order notional ${n:,.0f} > max_order_notional_usd ${cap:,.0f}")
    return None


def check_position_pct(ctx: GateContext) -> Optional[Violation]:
    eq = ctx.equity
    n = ctx.notional
    if eq is None or n is None or eq <= 0:
        return None
    after = ctx.state.symbol_value(ctx.order.symbol) + n
    pct = after / eq * 100
    cap = ctx.levers.risk.max_position_pct
    if pct > cap + 1e-6:
        return Violation("position_pct", f"{ctx.order.symbol} exposure would be {pct:.1f}% of equity > {cap}%")
    return None


def check_open_positions(ctx: GateContext) -> Optional[Violation]:
    key = ctx.order.instrument_key()
    if ctx.state.position_qty(key):
        return None  # adding to an existing line does not open a new one
    n = len(ctx.state.open_positions())
    cap = ctx.levers.risk.max_open_positions
    if n >= cap:
        return Violation("open_positions", f"{n} open positions already (max {cap})")
    return None


def check_trades_per_day(ctx: GateContext) -> Optional[Violation]:
    cap = ctx.levers.risk.max_trades_per_day
    if ctx.state.trades_today >= cap:
        return Violation("trades_per_day", f"{ctx.state.trades_today} opening trades today (max {cap})")
    return None


def check_cooldown(ctx: GateContext) -> Optional[Violation]:
    mins = ctx.levers.risk.cooldown_minutes_same_symbol
    last = ctx.state.last_order_ts.get(ctx.order.symbol)
    if mins and last is not None:
        elapsed = (ctx.state.now - last).total_seconds() / 60
        if elapsed < mins:
            return Violation("cooldown", f"{ctx.order.symbol} traded {elapsed:.0f} min ago (cooldown {mins} min)")
    return None


def check_universe(ctx: GateContext) -> Optional[Violation]:
    u = ctx.levers.universe
    s = ctx.order.symbol
    if u.blocklist and s in u.blocklist:
        return Violation("universe", f"{s} is on the blocklist")
    if u.allowlist and s not in u.allowlist:
        return Violation("universe", f"{s} is not on the allowlist")
    px = ctx.reference_price or (ctx.proposal.entry_price if ctx.proposal and ctx.order.instrument == Instrument.equity else None)
    if ctx.order.instrument == Instrument.equity and px is not None and px < u.min_price:
        return Violation("universe", f"{s} price {px:g} < min_price {u.min_price}")
    return None


def check_liquidity(ctx: GateContext) -> Optional[Violation]:
    p = ctx.proposal
    if p is None:
        return None
    u = ctx.levers.universe
    liq = p.liquidity
    if liq.avg_dollar_volume is not None and liq.avg_dollar_volume < u.min_avg_dollar_volume:
        return Violation("liquidity", f"avg dollar volume {liq.avg_dollar_volume:,.0f} < {u.min_avg_dollar_volume:,.0f}")
    if p.instrument == Instrument.option:
        o = ctx.levers.options
        if liq.open_interest is not None and liq.open_interest < o.min_open_interest:
            return Violation("liquidity", f"open interest {liq.open_interest} < {o.min_open_interest}")
        if liq.spread_pct is not None and liq.spread_pct > o.max_bid_ask_spread_pct:
            return Violation("liquidity", f"bid/ask spread {liq.spread_pct:.1f}% > {o.max_bid_ask_spread_pct}%")
    return None


def check_earnings(ctx: GateContext) -> Optional[Violation]:
    p = ctx.proposal
    if p is None or p.earnings_date is None:
        return None
    if "earnings_play" in p.tags:
        return None
    days = ctx.levers.universe.exclude_earnings_within_days
    today = ctx.state.now.date()
    if 0 <= (p.earnings_date - today).days <= days:
        return Violation("earnings", f"earnings on {p.earnings_date} within {days} days; tag earnings_play to override")
    return None


def check_options(ctx: GateContext) -> Optional[Violation]:
    if ctx.order.instrument != Instrument.option:
        return None
    o = ctx.levers.options
    if not o.enabled:
        return Violation("options", "options trading disabled (options.enabled=false)")
    leg = ctx.order.option
    if leg is None:
        return Violation("options", "option order without a parsed leg")
    dte = (leg.expiry - ctx.state.now.date()).days
    if dte < o.dte_min or dte > o.dte_max:
        return Violation("options", f"DTE {dte} outside [{o.dte_min}, {o.dte_max}]")
    if ctx.order.qty > o.max_contracts:
        return Violation("options", f"{ctx.order.qty:g} contracts > max_contracts {o.max_contracts}")
    n = ctx.notional
    if n is not None and n > o.max_premium_per_trade_usd + 1e-6:
        return Violation("options", f"premium ${n:,.0f} > max_premium_per_trade_usd ${o.max_premium_per_trade_usd:,.0f}")
    eq = ctx.equity
    if eq and n is not None:
        after = (ctx.state.options_value() + n) / eq * 100
        if after > o.max_options_exposure_pct + 1e-6:
            return Violation("options", f"options exposure would be {after:.1f}% > {o.max_options_exposure_pct}%")
    return None


def check_sized(ctx: GateContext) -> Optional[Violation]:
    if ctx.proposal_max_qty is None:
        return None
    if ctx.order.qty > ctx.proposal_max_qty + 1e-9:
        return Violation("sizing", f"qty {ctx.order.qty:g} > proposal max_qty {ctx.proposal_max_qty:g}")
    return None


def check_buying_power(ctx: GateContext) -> Optional[Violation]:
    bp = ctx.state.buying_power
    n = ctx.notional
    if bp is not None and n is not None and n > bp + 1e-6 and not ctx.levers.dry_run:
        return Violation("buying_power", f"notional ${n:,.0f} > buying power ${bp:,.0f}")
    return None


ALWAYS: list[Check] = [check_kill_switch, check_market_hours, check_exit_sanity]
ENTRY_ONLY: list[Check] = [
    check_halted,
    check_cutoff,
    check_snapshot_fresh,
    check_equity_floor,
    check_daily_loss,
    check_weekly_loss,
    check_sized,
    check_notional,
    check_buying_power,
    check_position_pct,
    check_open_positions,
    check_trades_per_day,
    check_cooldown,
    check_universe,
    check_liquidity,
    check_earnings,
    check_options,
]


def evaluate_limits(ctx: GateContext) -> list[Violation]:
    checks = list(ALWAYS)
    if not ctx.is_exit:
        checks += ENTRY_ONLY
    out: list[Violation] = []
    for c in checks:
        v = c(ctx)
        if v is not None:
            out.append(v)
    return out
