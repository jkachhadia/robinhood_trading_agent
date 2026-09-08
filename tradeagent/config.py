"""Levers: every risk and permission knob, loaded from config/levers.yaml.

Runtime toggles (mode, kill_switch, dry_run) can also be flipped from the CLI.
Those are stored in data/overrides.json so the YAML file keeps its comments.
Overrides take precedence over the YAML values.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from . import paths


class Mode(str, Enum):
    approve_all = "approve_all"
    tiered = "tiered"
    autonomous = "autonomous"


class AllowedHours(str, Enum):
    regular = "regular"
    extended = "extended"
    any = "any"


class RiskLevers(BaseModel):
    max_order_notional_usd: float = 500
    max_position_pct: float = 10
    max_open_positions: int = 8
    max_daily_loss_pct: float = 2
    max_weekly_loss_pct: float = 5
    risk_per_trade_pct: float = 0.5
    min_reward_risk: float = 2.0
    max_trades_per_day: int = 6
    cooldown_minutes_same_symbol: int = 60
    equity_floor_usd: float = 0
    fractional_shares: bool = True       # size equities in fractional shares (options are always whole contracts)
    fractional_decimals: int = 4         # Robinhood accepts up to 6; 4 keeps orders readable
    min_order_notional_usd: float = 1.0  # Robinhood's minimum for fractional orders


class TieredLevers(BaseModel):
    auto_max_notional_usd: float = 200
    auto_min_confidence: float = 0.7
    new_symbol_requires_approval: bool = True
    options_require_approval: bool = True
    exits_auto_allowed: bool = True


class UniverseLevers(BaseModel):
    allowlist: list[str] = Field(default_factory=list)
    blocklist: list[str] = Field(default_factory=list)
    min_price: float = 5
    min_avg_dollar_volume: float = 5_000_000
    exclude_earnings_within_days: int = 3

    @field_validator("allowlist", "blocklist", mode="before")
    @classmethod
    def _upper(cls, v):
        return [str(s).upper() for s in (v or [])]


class OptionsLevers(BaseModel):
    enabled: bool = True
    max_premium_per_trade_usd: float = 300
    max_options_exposure_pct: float = 10
    dte_min: int = 14
    dte_max: int = 90
    max_contracts: int = 5
    min_open_interest: int = 500
    max_bid_ask_spread_pct: float = 10


class SessionLevers(BaseModel):
    allowed_hours: AllowedHours = AllowedHours.regular
    no_new_positions_after: str = "15:30"  # ET, HH:MM
    require_fresh_snapshot_minutes: int = 10
    require_review_before_place: bool = True
    require_proposal_for_place: bool = True
    proposal_ttl_hours: int = 24
    limit_price_tolerance_pct: float = 1.0


class MarketLevers(BaseModel):
    extra_holidays: list[str] = Field(default_factory=list)  # YYYY-MM-DD
    intraday_manage_every_minutes: int = 30


class Levers(BaseModel):
    mode: Mode = Mode.approve_all
    kill_switch: bool = False
    dry_run: bool = True
    paper_equity_usd: float = 10_000   # equity assumed in dry_run when no portfolio snapshot exists
    mcp_server_name: str = "robinhood-trading"
    risk: RiskLevers = Field(default_factory=RiskLevers)
    tiered: TieredLevers = Field(default_factory=TieredLevers)
    universe: UniverseLevers = Field(default_factory=UniverseLevers)
    options: OptionsLevers = Field(default_factory=OptionsLevers)
    session: SessionLevers = Field(default_factory=SessionLevers)
    market: MarketLevers = Field(default_factory=MarketLevers)

    @property
    def tool_prefix(self) -> str:
        return f"mcp__{self.mcp_server_name}__"


OVERRIDABLE = ("mode", "kill_switch", "dry_run")


def read_overrides(path: Optional[Path] = None) -> dict:
    p = path or paths.overrides_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def write_override(key: str, value, path: Optional[Path] = None) -> dict:
    if key not in OVERRIDABLE:
        raise ValueError(f"{key} is not a runtime-overridable lever; edit config/levers.yaml")
    p = path or paths.overrides_path()
    data = read_overrides(p)
    if isinstance(value, Enum):
        value = value.value
    data[key] = value
    p.write_text(json.dumps(data, indent=2))
    return data


def clear_override(key: str, path: Optional[Path] = None) -> dict:
    p = path or paths.overrides_path()
    data = read_overrides(p)
    data.pop(key, None)
    p.write_text(json.dumps(data, indent=2))
    return data


def load_levers(config: Optional[Path] = None, overrides: Optional[Path] = None) -> Levers:
    cfg_path = config or paths.config_path()
    raw: dict = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    for k, v in read_overrides(overrides).items():
        if k in OVERRIDABLE:
            raw[k] = v
    return Levers.model_validate(raw)


def default_yaml() -> str:
    """The documented default levers.yaml content."""
    return DEFAULT_LEVERS_YAML


DEFAULT_LEVERS_YAML = """# tradeagent levers. Every order the agent tries to place is checked against these
# by a Claude Code PreToolUse hook. The model cannot change this file's effect on a
# running gate: it is re-read on every tool call.
#
# Runtime toggles below (mode, kill_switch, dry_run) can also be flipped with
#   uv run tradeagent mode <approve_all|tiered|autonomous>
#   uv run tradeagent kill on|off
#   uv run tradeagent dry-run on|off
# which write data/overrides.json (overrides win over this file).

mode: approve_all            # approve_all | tiered | autonomous
kill_switch: false           # true = deny every order-placing tool call
dry_run: true                # paper mode: orders are denied, simulated fills are journaled
paper_equity_usd: 10000      # equity assumed in dry_run until a real portfolio snapshot exists

mcp_server_name: robinhood-trading   # name used in `claude mcp add`; tools are mcp__<name>__<tool>

risk:
  max_order_notional_usd: 500
  max_position_pct: 10           # max single-symbol exposure as % of account equity
  max_open_positions: 8
  max_daily_loss_pct: 2          # equity drawdown vs day-open equity -> halt new entries for the day
  max_weekly_loss_pct: 5         # equity drawdown vs week-open equity -> halt new entries for the week
  risk_per_trade_pct: 0.5        # sizing: qty = equity * risk% / (entry - stop)
  min_reward_risk: 2.0           # proposals below this are rejected at propose time
  max_trades_per_day: 6          # opening orders per trading day
  cooldown_minutes_same_symbol: 60
  equity_floor_usd: 0            # halt all entries if equity drops below (0 = off)
  fractional_shares: true        # size equities in fractional shares (options are always whole contracts)
  fractional_decimals: 4         # decimals of a share; Robinhood accepts up to 6
  min_order_notional_usd: 1      # Robinhood's minimum order size for fractional shares

tiered:                          # only consulted when mode == tiered
  auto_max_notional_usd: 200
  auto_min_confidence: 0.7
  new_symbol_requires_approval: true   # first trade in a symbol needs a human
  options_require_approval: true
  exits_auto_allowed: true             # risk-reducing sells never wait for approval

universe:
  allowlist: []                  # empty = any symbol; otherwise only these
  blocklist: []                  # never trade these
  min_price: 5
  min_avg_dollar_volume: 5000000
  exclude_earnings_within_days: 3      # unless the proposal is tagged earnings_play

options:
  enabled: true
  max_premium_per_trade_usd: 300
  max_options_exposure_pct: 10
  dte_min: 14
  dte_max: 90
  max_contracts: 5
  min_open_interest: 500
  max_bid_ask_spread_pct: 10

session:
  allowed_hours: regular         # regular | extended | any   (ET, NYSE calendar)
  no_new_positions_after: "15:30"
  require_fresh_snapshot_minutes: 10   # get_portfolio must have been called this recently
  require_review_before_place: true    # review_*_order with identical params must precede place_*
  require_proposal_for_place: true     # every place_* must match a stored proposal
  proposal_ttl_hours: 24
  limit_price_tolerance_pct: 1.0       # placed limit may differ from proposal by this much

market:
  extra_holidays: []             # YYYY-MM-DD, added to the built-in NYSE holiday table
  intraday_manage_every_minutes: 30
"""
