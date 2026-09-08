"""Position sizing. Code decides the maximum; the model may only go smaller."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Levers
from .models import Instrument, Proposal


@dataclass
class SizingResult:
    max_qty: float
    binding: str
    max_notional: float
    details: dict = field(default_factory=dict)

    def note(self) -> str:
        parts = [f"{k}={v}" for k, v in self.details.items()]
        return f"max_qty={self.max_qty} (binding: {self.binding}); " + ", ".join(parts)


def _floor(x: float, decimals: int = 0) -> float:
    if x is None or x <= 0 or math.isinf(x) or math.isnan(x):
        return 0
    scale = 10 ** decimals
    v = math.floor(x * scale + 1e-9) / scale
    return int(v) if decimals == 0 else v


def size_entry(
    levers: Levers,
    p: Proposal,
    equity: float,
    existing_symbol_value: float = 0.0,
    existing_options_value: float = 0.0,
    buying_power: float | None = None,
) -> SizingResult:
    """Max quantity for an opening trade given every cap that applies."""
    r = levers.risk
    unit_cost = p.entry_price * p.multiplier
    caps: dict[str, float] = {}
    dec = r.fractional_decimals if (r.fractional_shares and p.instrument == Instrument.equity) else 0
    fl = lambda x: _floor(x, dec)  # noqa: E731

    # 1. risk per trade from stop distance (or full premium for options without a stop)
    risk_budget = equity * r.risk_per_trade_pct / 100.0
    if p.risk_per_unit and p.risk_per_unit > 0:
        risk_per_unit = p.risk_per_unit * p.multiplier
    elif p.instrument == Instrument.option:
        risk_per_unit = unit_cost  # long option: max loss is the premium
    else:
        risk_per_unit = None
    if risk_per_unit:
        caps["risk_per_trade"] = fl(risk_budget / risk_per_unit)

    # 2. per-order notional
    caps["max_order_notional"] = fl(r.max_order_notional_usd / unit_cost)

    # 3. per-symbol exposure
    room = equity * r.max_position_pct / 100.0 - existing_symbol_value
    caps["max_position_pct"] = fl(room / unit_cost)

    # 4. buying power (if known)
    if buying_power is not None:
        caps["buying_power"] = fl(buying_power / unit_cost)

    # 5. options-specific
    if p.instrument == Instrument.option:
        o = levers.options
        caps["max_premium_per_trade"] = _floor(o.max_premium_per_trade_usd / unit_cost)
        caps["max_contracts"] = o.max_contracts
        room_o = equity * o.max_options_exposure_pct / 100.0 - existing_options_value
        caps["max_options_exposure_pct"] = _floor(room_o / unit_cost)

    # 6. what the model asked for
    if p.requested_qty is not None:
        caps["requested_qty"] = max(fl(float(p.requested_qty)), 0)

    binding = min(caps, key=lambda k: caps[k])
    max_qty = caps[binding]
    if max_qty * unit_cost < r.min_order_notional_usd:
        max_qty, binding = 0, (binding if max_qty == 0 else "min_order_notional")
    return SizingResult(
        max_qty=max_qty,
        binding=binding,
        max_notional=max_qty * unit_cost,
        details={**caps, "unit_cost": round(unit_cost, 4), "equity": round(equity, 2)},
    )
