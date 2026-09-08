from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Tuesday 2026-09-08 10:00 ET (14:00 UTC): market open, not a holiday.
OPEN_NOW = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
# Same day 15:45 ET: past the 15:30 new-position cutoff, market still open.
LATE_NOW = datetime(2026, 9, 8, 19, 45, tzinfo=timezone.utc)
# Sunday
CLOSED_NOW = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)

TOOL = "mcp__robinhood-trading__"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config" / "levers.yaml"
    cfg.parent.mkdir()
    from tradeagent.config import default_yaml
    cfg.write_text(default_yaml())
    monkeypatch.setenv("TRADEAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("TRADEAGENT_DATA", str(data))
    monkeypatch.setenv("TRADEAGENT_CONFIG", str(cfg))
    monkeypatch.delenv("TRADEAGENT_HEADLESS", raising=False)
    return tmp_path


@pytest.fixture()
def store(env):
    from tradeagent.db import Store
    s = Store()
    yield s
    s.close()


def set_levers(env: Path, **top):
    """Rewrite levers.yaml with overrides of nested keys via dotted names."""
    import yaml
    cfg = env / "config" / "levers.yaml"
    raw = yaml.safe_load(cfg.read_text())
    for k, v in top.items():
        parts = k.split(".")
        d = raw
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = v
    cfg.write_text(yaml.safe_dump(raw))


def levers(env: Path, **top):
    set_levers(env, **top)
    from tradeagent.config import load_levers
    return load_levers()


def proposal_dict(**over) -> dict:
    d = {
        "symbol": "AAPL",
        "instrument": "equity",
        "side": "buy",
        "order_type": "limit",
        "limit_price": 150.0,
        "entry_price": 150.0,
        "stop_price": 145.0,
        "target_price": 162.0,
        "horizon": "swing",
        "thesis": "Breakout above 20-day range on rising volume with sector strength confirming.",
        "bull": ["breakout", "volume", "sector strength"],
        "bear": ["macro risk", "extended from 50dma", "earnings in 5 weeks"],
        "confidence": 0.75,
        "tags": ["breakout"],
        "evidence": ["get_equity_historicals 2026-09-08"],
    }
    d.update(over)
    return d


def hook_input(tool: str, tool_input: dict, session_id: str = "sess-1", tool_use_id: str = "tu-1") -> dict:
    return {
        "session_id": session_id,
        "tool_use_id": tool_use_id,
        "hook_event_name": "PreToolUse",
        "tool_name": TOOL + tool,
        "tool_input": tool_input,
        "cwd": os.getcwd(),
    }


def equity_order(qty=2, limit=150.0, side="buy", symbol="AAPL", order_type="limit") -> dict:
    d = {"symbol": symbol, "side": side, "quantity": qty, "order_type": order_type}
    if limit is not None:
        d["limit_price"] = limit
    return d


def record_review(store, tool_input: dict, tool="review_equity_order"):
    from tradeagent import fieldmap as fm, journal
    name = TOOL + tool
    mh = fm.parse_order(name, tool, tool_input).match_hash()
    journal.record_review(store, "sess-1", name, mh, tool_input, {"ok": True})


def snapshot(store, equity=10000.0, buying_power=8000.0, now=OPEN_NOW):
    from tradeagent import journal
    journal.record_snapshot(store, "sess-1", "get_portfolio", {"equity": equity, "buying_power": buying_power}, now=now)
