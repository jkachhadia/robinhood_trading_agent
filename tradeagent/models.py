"""Pydantic models: proposals, parsed orders, gate decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Instrument(str, Enum):
    equity = "equity"
    option = "option"


class Side(str, Enum):
    buy = "buy"
    sell = "sell"


class OrderType(str, Enum):
    market = "market"
    limit = "limit"
    stop = "stop"
    stop_limit = "stop_limit"


class Horizon(str, Enum):
    intraday = "intraday"
    swing = "swing"


class ProposalStatus(str, Enum):
    pending = "pending"            # waiting for a human
    approved = "approved"          # human said yes
    auto_eligible = "auto_eligible"  # tiered/autonomous: gate may allow without a human
    rejected = "rejected"          # human said no, or propose-time checks failed
    executing = "executing"        # gate allowed a place_* call; awaiting PostToolUse
    placed = "placed"              # broker accepted the order
    simulated = "simulated"        # dry_run fill journaled
    failed = "failed"              # broker rejected / tool errored
    expired = "expired"
    cancelled = "cancelled"


class OptionLeg(BaseModel):
    expiry: date
    strike: float
    option_type: Literal["call", "put"]

    def key(self) -> str:
        return f"{self.expiry.isoformat()}:{self.strike:g}:{self.option_type}"


class Catalyst(BaseModel):
    description: str
    date: Optional[date] = None


class Liquidity(BaseModel):
    avg_dollar_volume: Optional[float] = None
    spread_pct: Optional[float] = None
    open_interest: Optional[int] = None
    avg_volume: Optional[float] = None


class Proposal(BaseModel):
    """What the model must produce before any order. Validated at propose time."""

    symbol: str
    instrument: Instrument = Instrument.equity
    option: Optional[OptionLeg] = None
    side: Side
    is_exit: bool = False              # closing / reducing an existing position
    order_type: OrderType = OrderType.limit
    limit_price: Optional[float] = None

    entry_price: float                 # expected fill (per share / per contract premium)
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    invalidation_price: Optional[float] = None
    invalidation_reason: Optional[str] = None

    horizon: Horizon = Horizon.swing
    time_stop_hours: Optional[float] = None

    thesis: str = Field(min_length=20)
    bull: list[str] = Field(default_factory=list)
    bear: list[str] = Field(default_factory=list)
    catalysts: list[Catalyst] = Field(default_factory=list)
    liquidity: Liquidity = Field(default_factory=Liquidity)
    earnings_date: Optional[date] = None
    confidence: float = Field(ge=0, le=1)
    tags: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    requested_qty: Optional[float] = None
    notes: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v or len(v) > 12:
            raise ValueError("symbol must be 1-12 chars")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return [t.strip().lower() for t in v if t.strip()]

    @model_validator(mode="after")
    def _consistency(self) -> "Proposal":
        if self.instrument == Instrument.option and self.option is None:
            raise ValueError("option proposals need an `option` leg (expiry, strike, option_type)")
        if self.instrument == Instrument.equity and self.option is not None:
            raise ValueError("equity proposals must not carry an option leg")
        if self.order_type in (OrderType.limit, OrderType.stop_limit) and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} orders need limit_price")
        if self.entry_price <= 0:
            raise ValueError("entry_price must be > 0")
        if not self.is_exit:
            # Opening trades (long only on Robinhood's MCP): need a full plan.
            if self.side != Side.buy:
                raise ValueError("opening trades must be buys (Robinhood MCP is long-only); set is_exit for sells")
            if self.stop_price is None or self.target_price is None:
                raise ValueError("opening trades need stop_price and target_price")
            if not (self.stop_price < self.entry_price < self.target_price):
                raise ValueError("need stop_price < entry_price < target_price for a long entry")
            if len(self.bull) < 3 or len(self.bear) < 3:
                raise ValueError("opening trades need at least 3 bull and 3 bear points")
            if self.invalidation_price is None:
                self.invalidation_price = self.stop_price
        return self

    @property
    def risk_per_unit(self) -> Optional[float]:
        if self.stop_price is None:
            return None
        return max(self.entry_price - self.stop_price, 0.0)

    @property
    def reward_risk(self) -> Optional[float]:
        if self.stop_price is None or self.target_price is None:
            return None
        r = self.entry_price - self.stop_price
        if r <= 0:
            return None
        return (self.target_price - self.entry_price) / r

    @property
    def multiplier(self) -> int:
        return 100 if self.instrument == Instrument.option else 1

    def instrument_key(self) -> str:
        if self.instrument == Instrument.option and self.option:
            return f"{self.symbol}:{self.option.key()}"
        return self.symbol

    def summary(self) -> str:
        leg = f" {self.option.expiry} {self.option.strike:g}{self.option.option_type[0].upper()}" if self.option else ""
        px = f" @{self.limit_price:g}" if self.limit_price else " mkt"
        plan = ""
        if self.stop_price and self.target_price:
            plan = f" stop {self.stop_price:g} tgt {self.target_price:g} R:R {self.reward_risk:.1f}"
        kind = "EXIT" if self.is_exit else "ENTRY"
        return f"{kind} {self.side.value.upper()} {self.symbol}{leg}{px}{plan} conf {self.confidence:.2f} [{self.horizon.value}]"


class ParsedOrder(BaseModel):
    """Normalized view of a place_*/review_* tool_input."""

    tool_name: str
    symbol: str
    instrument: Instrument
    side: Side
    qty: float
    order_type: OrderType = OrderType.market
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    option: Optional[OptionLeg] = None
    raw_keys: list[str] = Field(default_factory=list)

    @property
    def multiplier(self) -> int:
        return 100 if self.instrument == Instrument.option else 1

    def instrument_key(self) -> str:
        if self.instrument == Instrument.option and self.option:
            return f"{self.symbol}:{self.option.key()}"
        return self.symbol

    def notional(self, reference_price: Optional[float] = None) -> Optional[float]:
        px = self.limit_price or reference_price
        if px is None:
            return None
        return px * self.qty * self.multiplier

    def match_hash(self) -> str:
        """Hash of the economically relevant fields; review_* and place_* must agree."""
        payload = {
            "symbol": self.symbol,
            "instrument": self.instrument.value,
            "side": self.side.value,
            "qty": round(self.qty, 6),
            "order_type": self.order_type.value,
            "limit_price": None if self.limit_price is None else round(self.limit_price, 4),
            "stop_price": None if self.stop_price is None else round(self.stop_price, 4),
            "option": self.option.key() if self.option else None,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def summary(self) -> str:
        leg = f" {self.option.expiry} {self.option.strike:g}{self.option.option_type[0].upper()}" if self.option else ""
        px = f" @{self.limit_price:g}" if self.limit_price else " mkt"
        return f"{self.side.value.upper()} {self.qty:g} {self.symbol}{leg}{px} ({self.order_type.value})"


class DecisionAction(str, Enum):
    allow = "allow"
    deny = "deny"
    ask = "ask"


class Decision(BaseModel):
    action: DecisionAction
    reason: str
    rule: Optional[str] = None
    proposal_id: Optional[int] = None
    context_for_model: Optional[str] = None

    def to_hook_output(self) -> dict:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": self.action.value,
                "permissionDecisionReason": self.reason,
            }
        }
        if self.context_for_model:
            out["hookSpecificOutput"]["additionalContext"] = self.context_for_model
        return out


def utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
