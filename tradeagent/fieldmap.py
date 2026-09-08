"""Best-effort mapping from Robinhood MCP tool inputs/outputs to normalized fields.

Robinhood does not document the request/response shapes of its Trading MCP.
Everything here is alias-driven so it can be tuned from captured samples
(`uv run tradeagent inspect <tool>`) without touching the gate logic.
When a required order field cannot be found the gate fails closed.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Iterable, Optional

from .models import Instrument, OptionLeg, OrderType, ParsedOrder, Side

# ---- tool name classification -------------------------------------------

PLACE_TOOLS = ("place_equity_order", "place_option_order", "place_crypto_order")
CANCEL_TOOLS = ("cancel_equity_order", "cancel_option_order", "cancel_crypto_order")
REVIEW_TOOLS = ("review_equity_order", "review_option_order", "preview_crypto_order")
PORTFOLIO_TOOLS = ("get_portfolio", "get_accounts")
POSITION_TOOLS = ("get_equity_positions", "get_option_positions", "get_crypto_positions")
QUOTE_TOOLS = ("get_equity_quotes", "get_option_quotes", "get_crypto_quotes")
ORDER_QUERY_TOOLS = ("get_equity_orders", "get_option_orders", "get_crypto_orders")
PNL_TOOLS = ("get_realized_pnl", "get_pnl_trade_history")


def short_tool_name(tool_name: str, prefix: str) -> str:
    return tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name


def is_place(short: str) -> bool:
    return short in PLACE_TOOLS


def is_cancel(short: str) -> bool:
    return short in CANCEL_TOOLS


def is_review(short: str) -> bool:
    return short in REVIEW_TOOLS


def instrument_for_tool(short: str) -> Instrument:
    if "option" in short:
        return Instrument.option
    return Instrument.equity


# ---- aliases --------------------------------------------------------------

SYMBOL_KEYS = ("symbol", "ticker", "instrument_symbol", "underlying_symbol", "underlying", "chain_symbol", "asset_code")
SIDE_KEYS = ("side", "direction", "order_side", "action", "transaction_type")
QTY_KEYS = ("quantity", "qty", "shares", "contracts", "units", "num_shares", "num_contracts")
ORDER_TYPE_KEYS = ("order_type", "type", "orderType", "kind")
LIMIT_KEYS = ("limit_price", "price", "limitPrice", "limit")
STOP_KEYS = ("stop_price", "stopPrice", "stop", "trigger_price")
EXPIRY_KEYS = ("expiration_date", "expiry", "expiration", "exp_date", "expirationDate")
STRIKE_KEYS = ("strike_price", "strike", "strikePrice")
OPTION_TYPE_KEYS = ("option_type", "contract_type", "right", "put_call", "optionType")
NESTED_ORDER_KEYS = ("order", "order_request", "request", "params", "input")
LEG_KEYS = ("legs", "leg", "instruments")

# Robinhood get_portfolio: total_value is the whole account; equity_value is stocks only (never use it as equity).
EQUITY_KEYS_LIST = ("total_value", "total_equity", "portfolio_value", "equity", "account_value",
                    "net_liquidation", "portfolio_equity", "extended_hours_equity")
PENDING_DEPOSIT_KEYS = ("pending_deposits", "pending_deposit")
DOLLAR_KEYS = ("dollar_based_amount", "amount_in_dollars", "dollar_amount", "notional")
OPTION_ID_KEYS = ("option_id", "option_instrument_id", "instrument_id")
BUYING_POWER_KEYS = ("buying_power", "buyingPower", "cash_available_for_trading", "available_to_trade", "cash_available")
CASH_KEYS = ("cash", "cash_balance", "uninvested_cash", "cash_held")
PRICE_KEYS = ("last_trade_price", "last_price", "price", "mark_price", "mark", "last", "adjusted_mark_price")
BID_KEYS = ("bid_price", "bid", "best_bid")
ASK_KEYS = ("ask_price", "ask", "best_ask")
POS_QTY_KEYS = ("quantity", "qty", "shares", "contracts", "quantity_held", "units")
AVG_COST_KEYS = ("average_buy_price", "avg_cost", "average_cost", "average_price", "avg_price", "cost_basis_per_share")
MKT_VALUE_KEYS = ("market_value", "equity", "current_value", "value")
ORDER_ID_KEYS = ("id", "order_id", "orderId", "ref_id")
ORDER_STATE_KEYS = ("state", "status", "order_state")
FILL_PRICE_KEYS = ("average_price", "fill_price", "executed_price", "avg_fill_price", "price")


def _first(d: dict, keys: Iterable[str]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    # case-insensitive fallback
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("amount", "value", "price"):
            if k in v:
                return _to_float(v[k])
        return None
    s = str(v).strip().replace(",", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return None


def _to_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 2 if "T" in fmt else len(s)], fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _flatten_order_input(tool_input: dict) -> dict:
    """Merge nested order objects / first leg into one flat dict (outer keys win)."""
    merged: dict = {}
    for nk in NESTED_ORDER_KEYS:
        inner = tool_input.get(nk)
        if isinstance(inner, dict):
            merged.update(_flatten_order_input(inner))
    for lk in LEG_KEYS:
        legs = tool_input.get(lk)
        if isinstance(legs, list) and legs and isinstance(legs[0], dict):
            merged.update(legs[0])
        elif isinstance(legs, dict):
            merged.update(legs)
    merged.update({k: v for k, v in tool_input.items() if not isinstance(v, (dict, list))})
    return merged


def parse_side(v) -> Optional[Side]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("buy", "b", "long", "buy_to_open", "buy_to_close", "bto", "btc", "purchase"):
        return Side.buy
    if s in ("sell", "s", "short", "sell_to_close", "sell_to_open", "stc", "sto"):
        return Side.sell
    return None


def parse_order_type(v) -> OrderType:
    if v is None:
        return OrderType.market
    s = str(v).strip().lower().replace("-", "_").replace(" ", "_")
    if s in ("limit", "lmt"):
        return OrderType.limit
    if s in ("stop_limit", "stoplimit"):
        return OrderType.stop_limit
    if s in ("stop", "stop_loss", "stp"):
        return OrderType.stop
    return OrderType.market


def parse_option_type(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("call", "c", "calls"):
        return "call"
    if s in ("put", "p", "puts"):
        return "put"
    return None


def parse_order(tool_name: str, short: str, tool_input: dict) -> ParsedOrder:
    """Raise ValueError with a helpful message when required fields are missing."""
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input is not an object")
    flat = _flatten_order_input(tool_input)
    instrument = instrument_for_tool(short)

    symbol = _first(flat, SYMBOL_KEYS)
    side = parse_side(_first(flat, SIDE_KEYS))
    qty = _to_float(_first(flat, QTY_KEYS))
    dollar_amount = _to_float(_first(flat, DOLLAR_KEYS))
    limit_for_dollars = _to_float(_first(flat, LIMIT_KEYS))
    if qty is None and dollar_amount is not None:
        if limit_for_dollars is None or limit_for_dollars <= 0:
            raise ValueError("dollar-based order without a limit price cannot be sized by the gate; "
                             "use a share quantity, or a dollar amount with a limit price")
        qty = round(dollar_amount / limit_for_dollars, 6)
    missing = [n for n, v in (("symbol", symbol), ("side", side), ("quantity", qty)) if v is None]
    if missing:
        raise ValueError(
            f"could not parse {', '.join(missing)} from tool_input keys {sorted(flat.keys())}; "
            "add the field name to tradeagent/fieldmap.py aliases"
        )
    if qty <= 0:
        raise ValueError("quantity must be > 0")

    order_type_raw = _first(flat, ORDER_TYPE_KEYS)
    # `type` may hold call/put for options; only treat it as order type if it looks like one.
    if order_type_raw is not None and parse_option_type(order_type_raw) and instrument == Instrument.option:
        order_type_raw = _first(flat, ("order_type", "orderType"))
    order_type = parse_order_type(order_type_raw)
    limit_price = _to_float(_first(flat, LIMIT_KEYS))
    stop_price = _to_float(_first(flat, STOP_KEYS))
    if limit_price is not None and order_type == OrderType.market:
        order_type = OrderType.limit

    option = None
    if instrument == Instrument.option:
        expiry = _to_date(_first(flat, EXPIRY_KEYS))
        strike = _to_float(_first(flat, STRIKE_KEYS))
        otype = parse_option_type(_first(flat, OPTION_TYPE_KEYS)) or parse_option_type(flat.get("type"))
        if expiry is None or strike is None or otype is None:
            raise ValueError(
                f"option order missing expiry/strike/type; keys seen: {sorted(flat.keys())}"
            )
        option = OptionLeg(expiry=expiry, strike=strike, option_type=otype)

    return ParsedOrder(
        tool_name=tool_name,
        symbol=str(symbol).strip().upper(),
        instrument=instrument,
        side=side,
        qty=float(qty),
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        option=option,
        dollar_amount=dollar_amount,
        raw_keys=sorted(str(k) for k in flat.keys()),
    )


# ---- response decoding ---------------------------------------------------


def decode_tool_response(resp: Any) -> Any:
    """Claude Code hands MCP results as text blocks; unwrap JSON where possible."""
    if resp is None:
        return None
    if isinstance(resp, str):
        return _maybe_json(resp)
    if isinstance(resp, dict):
        if "content" in resp and isinstance(resp["content"], list):
            return decode_tool_response(resp["content"])
        if resp.get("type") == "text" and "text" in resp:
            return _maybe_json(resp["text"])
        return resp
    if isinstance(resp, list):
        texts = []
        structured = []
        for item in resp:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text", "")))
            elif isinstance(item, (dict, list)):
                structured.append(item)
        if texts:
            joined = "\n".join(texts)
            parsed = _maybe_json(joined)
            if parsed is not joined:
                return parsed
            if len(texts) == 1:
                return _maybe_json(texts[0])
            return joined
        return structured or resp
    return resp


def _maybe_json(s: str):
    t = s.strip()
    if not t:
        return s
    if t[0] in "[{":
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
    return s


def walk(obj: Any, depth: int = 0):
    """Yield every dict in a nested structure (breadth-first-ish, capped depth)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, depth + 1)


def find_number(obj: Any, keys: Iterable[str]) -> Optional[float]:
    keys = tuple(keys)
    for d in walk(obj):
        v = _first(d, keys)
        f = _to_float(v)
        if f is not None:
            return f
    return None


def find_str(obj: Any, keys: Iterable[str]) -> Optional[str]:
    keys = tuple(keys)
    for d in walk(obj):
        v = _first(d, keys)
        if v is not None and not isinstance(v, (dict, list)):
            return str(v)
    return None


def find_position_rows(obj: Any) -> list[dict]:
    """Dicts that look like positions: have a symbol-ish key and a quantity-ish key."""
    rows = []
    for d in walk(obj):
        sym = _first(d, SYMBOL_KEYS)
        q = _to_float(_first(d, POS_QTY_KEYS))
        if sym and q is not None and not isinstance(sym, (dict, list)):
            rows.append(d)
    return rows


def find_quote_rows(obj: Any) -> list[dict]:
    rows = []
    for d in walk(obj):
        sym = _first(d, SYMBOL_KEYS)
        px = _to_float(_first(d, PRICE_KEYS)) or _to_float(_first(d, BID_KEYS))
        if sym and px is not None and not isinstance(sym, (dict, list)):
            rows.append(d)
    return rows


def response_indicates_error(decoded: Any, raw: Any) -> bool:
    if isinstance(raw, dict) and (raw.get("is_error") or raw.get("isError")):
        return True
    if isinstance(decoded, dict):
        if decoded.get("error") or decoded.get("errors"):
            return True
        state = str(_first(decoded, ORDER_STATE_KEYS) or "").lower()
        if state in ("rejected", "failed", "error", "cancelled", "canceled"):
            return True
    if isinstance(decoded, str):
        low = decoded.lower()
        if low.startswith("error") or "\"error\"" in low or "rejected" in low or "insufficient" in low:
            return True
    return False
