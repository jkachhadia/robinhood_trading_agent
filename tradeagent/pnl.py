"""FIFO P&L over journaled fills. Used for dry-run accounting and for stats."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import Instrument, Side


@dataclass
class Fill:
    order_id: int
    ts: str
    trading_date: str
    instrument_key: str
    symbol: str
    instrument: Instrument
    side: Side
    qty: float
    price: float
    proposal_id: Optional[int] = None
    is_exit: bool = False

    @property
    def mult(self) -> int:
        return 100 if self.instrument == Instrument.option else 1


@dataclass
class Lot:
    qty: float
    price: float
    ts: str
    trading_date: str
    proposal_id: Optional[int]
    order_id: int


@dataclass
class ClosedTrade:
    instrument_key: str
    symbol: str
    instrument: Instrument
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: str
    exit_ts: str
    exit_date: str
    pnl: float
    entry_proposal_id: Optional[int]
    exit_proposal_id: Optional[int]
    entry_order_id: int
    exit_order_id: int


@dataclass
class Book:
    open_lots: dict[str, list[Lot]] = field(default_factory=dict)
    closed: list[ClosedTrade] = field(default_factory=list)

    def apply(self, f: Fill) -> None:
        lots = self.open_lots.setdefault(f.instrument_key, [])
        if f.side == Side.buy:
            lots.append(Lot(f.qty, f.price, f.ts, f.trading_date, f.proposal_id, f.order_id))
            return
        remaining = f.qty
        while remaining > 1e-9 and lots:
            lot = lots[0]
            take = min(lot.qty, remaining)
            pnl = (f.price - lot.price) * take * f.mult
            self.closed.append(
                ClosedTrade(
                    instrument_key=f.instrument_key,
                    symbol=f.symbol,
                    instrument=f.instrument,
                    qty=take,
                    entry_price=lot.price,
                    exit_price=f.price,
                    entry_ts=lot.ts,
                    exit_ts=f.ts,
                    exit_date=f.trading_date,
                    pnl=pnl,
                    entry_proposal_id=lot.proposal_id,
                    exit_proposal_id=f.proposal_id,
                    entry_order_id=lot.order_id,
                    exit_order_id=f.order_id,
                )
            )
            lot.qty -= take
            remaining -= take
            if lot.qty <= 1e-9:
                lots.pop(0)
        # Selling more than held (should be blocked upstream) is ignored here.

    def realized(self, on_date: Optional[str] = None, since: Optional[str] = None) -> float:
        total = 0.0
        for t in self.closed:
            if on_date and t.exit_date != on_date:
                continue
            if since and t.exit_date < since:
                continue
            total += t.pnl
        return total

    def unrealized(self, quotes: dict[str, float]) -> float:
        total = 0.0
        for key, lots in self.open_lots.items():
            px = quotes.get(key)
            if px is None:
                continue
            mult = 100 if ":" in key else 1
            for lot in lots:
                total += (px - lot.price) * lot.qty * mult
        return total

    def positions(self) -> dict[str, tuple[float, float]]:
        """instrument_key -> (qty, avg_cost)"""
        out = {}
        for key, lots in self.open_lots.items():
            q = sum(l.qty for l in lots)
            if q <= 1e-9:
                continue
            avg = sum(l.qty * l.price for l in lots) / q
            out[key] = (q, avg)
        return out


def build_book(fills: list[Fill]) -> Book:
    book = Book()
    for f in sorted(fills, key=lambda x: (x.ts, x.order_id)):
        book.apply(f)
    return book
