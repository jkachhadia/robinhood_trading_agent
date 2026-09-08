"""Performance statistics over closed trades. This is the profit-optimization feedback loop."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .db import Store
from .journal import real_fills, simulated_fills
from .pnl import ClosedTrade, build_book


@dataclass
class Bucket:
    n: int = 0
    wins: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    pnl: float = 0.0
    r_sum: float = 0.0
    r_count: int = 0

    def add(self, t: ClosedTrade, r: Optional[float]) -> None:
        self.n += 1
        self.pnl += t.pnl
        if t.pnl > 0:
            self.wins += 1
            self.gross_win += t.pnl
        else:
            self.gross_loss += -t.pnl
        if r is not None:
            self.r_sum += r
            self.r_count += 1

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def expectancy(self) -> float:
        return self.pnl / self.n if self.n else 0.0

    @property
    def profit_factor(self) -> Optional[float]:
        if self.gross_loss <= 0:
            return None
        return self.gross_win / self.gross_loss

    @property
    def avg_r(self) -> Optional[float]:
        return self.r_sum / self.r_count if self.r_count else None

    def row(self, label: str) -> str:
        pf = "inf" if self.profit_factor is None and self.wins else ("n/a" if self.profit_factor is None else f"{self.profit_factor:.2f}")
        ar = "n/a" if self.avg_r is None else f"{self.avg_r:+.2f}"
        return f"{label:<22} n={self.n:<4} win={self.win_rate*100:5.1f}%  exp=${self.expectancy:8.2f}  pf={pf:<5} avgR={ar:<6} pnl=${self.pnl:,.2f}"


def _proposal_meta(store: Store, pid: Optional[int]) -> dict:
    if not pid:
        return {}
    r = store.one("SELECT payload, horizon, mode_at_creation, approval_source, confidence FROM proposals WHERE id=?", (pid,))
    if r is None:
        return {}
    try:
        p = json.loads(r["payload"])
    except (TypeError, json.JSONDecodeError):
        p = {}
    return {
        "tags": p.get("tags") or [],
        "horizon": r["horizon"] or p.get("horizon"),
        "mode": r["mode_at_creation"],
        "approval": r["approval_source"] or "human",
        "confidence": r["confidence"],
        "entry": p.get("entry_price"),
        "stop": p.get("stop_price"),
    }


def compute(store: Store, simulated: bool) -> dict[str, dict[str, Bucket]]:
    fills = simulated_fills(store) if simulated else real_fills(store)
    book = build_book(fills)
    groups: dict[str, dict[str, Bucket]] = {
        "all": defaultdict(Bucket), "tag": defaultdict(Bucket), "horizon": defaultdict(Bucket),
        "instrument": defaultdict(Bucket), "approval": defaultdict(Bucket), "confidence": defaultdict(Bucket),
        "symbol": defaultdict(Bucket),
    }
    for t in book.closed:
        meta = _proposal_meta(store, t.entry_proposal_id)
        r = None
        if meta.get("entry") and meta.get("stop"):
            risk = float(meta["entry"]) - float(meta["stop"])
            if risk > 0:
                r = (t.exit_price - t.entry_price) / risk
        groups["all"]["all"].add(t, r)
        for tag in meta.get("tags") or ["untagged"]:
            groups["tag"][tag].add(t, r)
        groups["horizon"][meta.get("horizon") or "unknown"].add(t, r)
        groups["instrument"][t.instrument.value].add(t, r)
        groups["approval"][meta.get("approval") or "unknown"].add(t, r)
        c = meta.get("confidence")
        cb = "unknown" if c is None else ("<0.6" if c < 0.6 else "0.6-0.8" if c < 0.8 else ">=0.8")
        groups["confidence"][cb].add(t, r)
        groups["symbol"][t.symbol].add(t, r)
    return groups


def render(store: Store, simulated: bool) -> str:
    groups = compute(store, simulated)
    title = "SIMULATED (dry run)" if simulated else "LIVE"
    out = [f"trade statistics — {title}"]
    if not groups["all"]:
        out.append("no closed trades yet")
        return "\n".join(out)
    out.append(groups["all"]["all"].row("all"))
    for name in ("horizon", "instrument", "tag", "confidence", "approval", "symbol"):
        g = groups[name]
        if not g:
            continue
        out.append(f"-- by {name}")
        for label, b in sorted(g.items(), key=lambda kv: -kv[1].pnl):
            out.append(b.row(label))
    return "\n".join(out)
