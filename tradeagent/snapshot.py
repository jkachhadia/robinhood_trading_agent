"""Pure parsers for MCP responses (portfolio, positions, quotes, orders)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import fieldmap as fm
from .models import Instrument, OptionLeg


@dataclass
class ParsedSnapshot:
    equity: Optional[float]
    buying_power: Optional[float]
    cash: Optional[float]
    pending_deposits: Optional[float] = None


@dataclass
class ParsedPosition:
    symbol: str
    instrument: Instrument
    instrument_key: str
    qty: float
    avg_cost: Optional[float]
    market_value: Optional[float]
    raw: dict


@dataclass
class ParsedQuote:
    symbol: str
    instrument_key: str
    price: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    raw: dict


@dataclass
class ParsedOrderResponse:
    broker_order_id: Optional[str]
    state: Optional[str]
    fill_price: Optional[float]
    is_error: bool


def parse_snapshot(decoded: Any) -> ParsedSnapshot:
    return ParsedSnapshot(
        equity=fm.find_number(decoded, fm.EQUITY_KEYS_LIST),
        buying_power=fm.find_number(decoded, fm.BUYING_POWER_KEYS),
        cash=fm.find_number(decoded, fm.CASH_KEYS),
        pending_deposits=fm.find_number(decoded, fm.PENDING_DEPOSIT_KEYS),
    )


def _option_leg_from_row(row: dict) -> Optional[OptionLeg]:
    expiry = fm._to_date(fm._first(row, fm.EXPIRY_KEYS))
    strike = fm._to_float(fm._first(row, fm.STRIKE_KEYS))
    otype = fm.parse_option_type(fm._first(row, fm.OPTION_TYPE_KEYS)) or fm.parse_option_type(row.get("type"))
    if expiry and strike is not None and otype:
        return OptionLeg(expiry=expiry, strike=strike, option_type=otype)
    return None


def parse_positions(decoded: Any, instrument: Instrument) -> list[ParsedPosition]:
    out: list[ParsedPosition] = []
    seen: set[str] = set()
    for row in fm.find_position_rows(decoded):
        symbol = str(fm._first(row, fm.SYMBOL_KEYS)).strip().upper()
        qty = fm._to_float(fm._first(row, fm.POS_QTY_KEYS)) or 0.0
        key = symbol
        if instrument == Instrument.option:
            leg = _option_leg_from_row(row)
            if leg is not None:
                key = f"{symbol}:{leg.key()}"
            else:
                # Robinhood option positions carry option_id + expiration but no strike; keep them under a
                # fallback key so exposure and exit checks still see them.
                oid = fm._first(row, fm.OPTION_ID_KEYS)
                if oid is None:
                    continue
                key = f"{symbol}:opt:{oid}"
        if key in seen:
            continue
        seen.add(key)
        avg = fm._to_float(fm._first(row, fm.AVG_COST_KEYS))
        mv = fm._to_float(fm._first(row, fm.MKT_VALUE_KEYS))
        if mv is None and avg is not None and instrument == Instrument.option:
            mv = avg * qty * 100
        out.append(
            ParsedPosition(
                symbol=symbol,
                instrument=instrument,
                instrument_key=key,
                qty=qty,
                avg_cost=avg,
                market_value=mv,
                raw=row,
            )
        )
    return out


def parse_quotes(decoded: Any, instrument: Instrument) -> list[ParsedQuote]:
    out: list[ParsedQuote] = []
    for row in fm.find_quote_rows(decoded):
        symbol = str(fm._first(row, fm.SYMBOL_KEYS)).strip().upper()
        key = symbol
        if instrument == Instrument.option:
            leg = _option_leg_from_row(row)
            if leg is not None:
                key = f"{symbol}:{leg.key()}"
        bid = fm._to_float(fm._first(row, fm.BID_KEYS))
        ask = fm._to_float(fm._first(row, fm.ASK_KEYS))
        price = fm._to_float(fm._first(row, fm.PRICE_KEYS))
        if price is None and bid is not None and ask is not None:
            price = (bid + ask) / 2
        out.append(ParsedQuote(symbol=symbol, instrument_key=key, price=price, bid=bid, ask=ask, raw=row))
    return out


def parse_order_response(decoded: Any, raw: Any) -> ParsedOrderResponse:
    is_err = fm.response_indicates_error(decoded, raw)
    oid = fm.find_str(decoded, fm.ORDER_ID_KEYS) if isinstance(decoded, (dict, list)) else None
    state = fm.find_str(decoded, fm.ORDER_STATE_KEYS) if isinstance(decoded, (dict, list)) else None
    fill = fm.find_number(decoded, fm.FILL_PRICE_KEYS) if isinstance(decoded, (dict, list)) else None
    return ParsedOrderResponse(broker_order_id=oid, state=state, fill_price=fill, is_error=is_err)
