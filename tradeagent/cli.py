"""`tradeagent` CLI: status, approvals, proposals, levers, inspection."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError
from rich import print as rprint
from rich.console import Console

from . import config as cfg
from . import journal, paths
from .db import Store
from .models import Proposal, ProposalStatus
from .report import actionable_rows, pending_rows, proposal_line, status_text

app = typer.Typer(help="Enforcement + journaling layer for the Robinhood agentic trading agent.", no_args_is_help=True)
console = Console()


def _store() -> Store:
    return Store()


# ---- status / levers ------------------------------------------------------------

@app.command()
def status(full: bool = typer.Option(True, help="Include positions, queues and lessons")):
    """Mode, P&L, limits headroom, positions, pending approvals."""
    levers = cfg.load_levers()
    print(status_text(_store(), levers, compact=not full))


@app.command()
def mode(value: Optional[str] = typer.Argument(None, help="approve_all | tiered | autonomous")):
    """Show or set the autonomy mode."""
    if value is None:
        print(cfg.load_levers().mode.value)
        return
    try:
        m = cfg.Mode(value)
    except ValueError:
        raise typer.BadParameter("mode must be approve_all, tiered or autonomous")
    cfg.write_override("mode", m)
    rprint(f"[bold]mode[/bold] set to [green]{m.value}[/green]")


@app.command()
def kill(value: str = typer.Argument(..., help="on | off")):
    """Kill switch: 'on' blocks every order-placing tool call immediately."""
    on = value.lower() in ("on", "true", "1", "yes")
    cfg.write_override("kill_switch", on)
    rprint("[red bold]KILL SWITCH ON[/red bold] — all orders blocked" if on else "[green]kill switch off[/green]")


@app.command("dry-run")
def dry_run(value: str = typer.Argument(..., help="on | off")):
    """Paper mode toggle. 'off' means real orders can reach Robinhood."""
    on = value.lower() in ("on", "true", "1", "yes")
    if not on:
        typer.confirm("dry_run OFF means REAL orders can be placed. Continue?", abort=True)
    cfg.write_override("dry_run", on)
    rprint("[yellow]dry_run ON (paper)[/yellow]" if on else "[red bold]dry_run OFF — LIVE ORDERS ENABLED[/red bold]")


@app.command("config")
def show_config(brief: bool = typer.Option(False, help="Only the sections that matter for a trade decision")):
    """Print the effective levers (yaml + overrides)."""
    levers = cfg.load_levers()
    data = levers.model_dump(mode="json")
    if brief:
        data = {k: data[k] for k in ("mode", "kill_switch", "dry_run", "risk", "tiered", "universe", "options", "session")}
    print(json.dumps(data, indent=2))
    ov = cfg.read_overrides()
    if ov:
        print(f"\noverrides ({paths.overrides_path()}): {json.dumps(ov)}")


@app.command()
def init(force: bool = typer.Option(False, help="Overwrite an existing levers.yaml")):
    """Create config/levers.yaml with documented defaults and initialise the database."""
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not force:
        rprint(f"[yellow]{p} exists; use --force to overwrite[/yellow]")
    else:
        p.write_text(cfg.default_yaml())
        rprint(f"wrote {p}")
    Store()
    rprint(f"database at {paths.db_path()}")
    lp = paths.lessons_path()
    if not lp.exists():
        lp.write_text("# Lessons (appended by /review-day; newest last)\n")


# ---- proposals -------------------------------------------------------------------

@app.command()
def propose(file: str = typer.Argument(..., help="Path to a proposal JSON file, or '-' for stdin")):
    """Validate, size and store a proposal. Prints its id, status and the max quantity allowed."""
    raw = sys.stdin.read() if file == "-" else Path(file).read_text()
    try:
        data = json.loads(raw)
        p = Proposal.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        rprint("[red]invalid proposal[/red]")
        print(str(e))
        raise typer.Exit(code=1)
    levers = cfg.load_levers()
    store = _store()
    pid, st, reasons, sizing = journal.create_proposal(store, levers, p)
    out = {
        "id": pid,
        "status": st,
        "summary": p.summary(),
        "max_qty": sizing.max_qty if sizing else (p.requested_qty if p.is_exit else None),
        "max_notional": round(sizing.max_notional, 2) if sizing else None,
        "binding_constraint": sizing.binding if sizing else None,
        "sizing_details": sizing.details if sizing else None,
        "rejection_reasons": reasons,
        "next": _next_step(levers, st, pid),
    }
    print(json.dumps(out, indent=2, default=str))
    if st == "rejected":
        raise typer.Exit(code=2)


def _next_step(levers, st: str, pid: int) -> str:
    if st == "rejected":
        return "fix the proposal and propose again"
    if st == "pending":
        return f"needs human approval: `uv run tradeagent approve {pid}` then /execute"
    if st == "auto_eligible":
        return "may execute now via /execute (review_* then place_*); the gate re-checks limits"
    return st


@app.command()
def pending():
    """List proposals waiting for a human."""
    rows = pending_rows(_store())
    if not rows:
        print("no pending proposals")
        return
    for r in rows:
        print(proposal_line(r))
        p = Proposal.model_validate_json(r["payload"])
        print(f"   thesis: {p.thesis[:300]}")
        if p.bear:
            print(f"   bear:   {p.bear[0][:200]}")
        if r["sizing_note"]:
            print(f"   sizing: {r['sizing_note']}")


@app.command()
def approve(id: int, note: Optional[str] = typer.Option(None, help="Optional note stored with the approval")):
    """Approve a pending proposal so the next /execute may place it."""
    store = _store()
    row, p = journal.load_proposal(store, id)
    if row is None:
        rprint(f"[red]no proposal #{id}[/red]")
        raise typer.Exit(1)
    if row["status"] not in (ProposalStatus.pending.value, ProposalStatus.auto_eligible.value, ProposalStatus.approved.value):
        rprint(f"[red]#{id} is {row['status']}; only pending/auto_eligible proposals can be approved[/red]")
        raise typer.Exit(1)
    journal.set_proposal_status(store, id, ProposalStatus.approved, approval_source="cli",
                                approved_at=journal.now_iso())
    rprint(f"[green]approved[/green] #{id} {p.summary()}" + (f" — {note}" if note else ""))


@app.command()
def reject(id: int, reason: str = typer.Argument("rejected by user")):
    """Reject a proposal."""
    store = _store()
    row, p = journal.load_proposal(store, id)
    if row is None:
        rprint(f"[red]no proposal #{id}[/red]")
        raise typer.Exit(1)
    journal.set_proposal_status(store, id, ProposalStatus.rejected, rejection_reasons=json.dumps([reason]))
    rprint(f"[yellow]rejected[/yellow] #{id} {p.summary()}")


@app.command()
def reset(id: int):
    """Clear an 'executing' flag left by a failed/interrupted order so it can be retried."""
    store = _store()
    row, p = journal.load_proposal(store, id)
    if row is None:
        rprint(f"[red]no proposal #{id}[/red]")
        raise typer.Exit(1)
    if row["status"] != ProposalStatus.executing.value:
        rprint(f"#{id} is {row['status']}; nothing to reset")
        return
    new = ProposalStatus.approved if row["approval_source"] else ProposalStatus.pending
    journal.set_proposal_status(store, id, new)
    rprint(f"#{id} -> {new.value}")


@app.command()
def proposals(limit: int = 20, status: Optional[str] = None):
    """List recent proposals."""
    store = _store()
    journal.expire_proposals(store)
    if status:
        rows = store.all("SELECT * FROM proposals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        rows = store.all("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        print(proposal_line(r))


@app.command()
def show(id: int):
    """Print a proposal in full."""
    row, p = journal.load_proposal(_store(), id)
    if row is None:
        rprint(f"[red]no proposal #{id}[/red]")
        raise typer.Exit(1)
    meta = {k: row[k] for k in row.keys() if k != "payload"}
    print(json.dumps({"meta": meta, "proposal": json.loads(row["payload"])}, indent=2, default=str))


# ---- journal views ----------------------------------------------------------------

@app.command()
def orders(limit: int = 20):
    """Recent orders (real and simulated)."""
    rows = _store().all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
    if not rows:
        print("no orders")
        return
    for r in rows:
        lim = "" if r["limit_price"] is None else f" limit {r['limit_price']:g}"
        fill = "" if r["fill_price"] is None else f" fill {r['fill_price']:g}"
        sim = " SIM" if r["simulated"] else ""
        print(f"#{r['id']} {r['ts']}{sim} {r['side'].upper()} {r['qty']:g} {r['instrument_key']} {r['order_type'] or ''}{lim}{fill} "
              f"-> {r['status']} proposal={r['proposal_id'] or '-'} broker_id={r['broker_order_id'] or '-'}")


@app.command()
def decisions(limit: int = 20):
    """Recent gate decisions."""
    rows = _store().all("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        print(f"{r['ts']} {r['action']:<5} [{r['rule']}] {r['tool_name'].split('__')[-1]} p={r['proposal_id'] or '-'} :: {r['reason'][:160]}")


@app.command()
def positions():
    """Known positions (broker snapshot, or simulated when dry_run)."""
    levers = cfg.load_levers()
    src = "simulated" if levers.dry_run else "broker"
    rows = _store().all("SELECT * FROM positions WHERE source=? ORDER BY instrument_key", (src,))
    if not rows:
        print(f"no {src} positions")
        return
    for r in rows:
        print(f"{r['instrument_key']}: qty {r['qty']:g} avg {r['avg_cost']} value {r['market_value']} (updated {r['updated_at']})")


@app.command()
def plan():
    """Open positions with the stop/target/invalidation/time-stop from their entry proposal (for /manage)."""
    from datetime import datetime, timezone
    levers = cfg.load_levers()
    store = _store()
    src = "simulated" if levers.dry_run else "broker"
    rows = store.all("SELECT * FROM positions WHERE source=? AND qty > 0 ORDER BY instrument_key", (src,))
    if not rows:
        print(f"no open {src} positions")
        return
    out = []
    for r in rows:
        key = r["instrument_key"]
        q = store.one("SELECT price, ts FROM quotes WHERE instrument_key=?", (key,))
        entry = store.one(
            "SELECT p.*, o.ts AS opened_at FROM proposals p JOIN orders o ON o.proposal_id=p.id "
            "WHERE p.instrument_key=? AND p.side='buy' AND o.status IN ('placed','filled') ORDER BY o.id DESC LIMIT 1",
            (key,),
        )
        item = {"instrument_key": key, "qty": r["qty"], "avg_cost": r["avg_cost"],
                "last_price": q["price"] if q else None, "quote_ts": q["ts"] if q else None}
        if entry:
            p = json.loads(entry["payload"])
            opened = entry["opened_at"]
            hours_open = None
            try:
                hours_open = round((datetime.now(timezone.utc) - datetime.fromisoformat(opened.replace("Z", "+00:00"))).total_seconds() / 3600, 1)
            except Exception:
                pass
            item.update({
                "entry_proposal": entry["id"], "entry_price": p.get("entry_price"), "stop_price": p.get("stop_price"),
                "target_price": p.get("target_price"), "invalidation_price": p.get("invalidation_price"),
                "invalidation_reason": p.get("invalidation_reason"), "horizon": p.get("horizon"),
                "time_stop_hours": p.get("time_stop_hours"), "opened_at": opened, "hours_open": hours_open,
                "thesis": (p.get("thesis") or "")[:240], "tags": p.get("tags"),
            })
            px = item["last_price"]
            flags = []
            if px is not None:
                if p.get("stop_price") and px <= p["stop_price"]:
                    flags.append("AT_OR_BELOW_STOP")
                if p.get("invalidation_price") and px <= p["invalidation_price"]:
                    flags.append("INVALIDATED")
                if p.get("target_price") and px >= p["target_price"]:
                    flags.append("AT_OR_ABOVE_TARGET")
            if p.get("time_stop_hours") and hours_open is not None and hours_open >= p["time_stop_hours"]:
                flags.append("TIME_STOP")
            item["flags"] = flags
        else:
            item["entry_proposal"] = None
            item["flags"] = ["NO_PLAN_ON_FILE"]
        item["symbol_notes"] = [n["note"] for n in journal.notes_for(store, r["symbol"], 5)]
        out.append(item)
    print(json.dumps(out, indent=2, default=str))


@app.command()
def stats(live: bool = typer.Option(False, help="Use real orders instead of simulated")):
    """Expectancy, win rate, profit factor, avg R by tag/horizon/instrument/confidence."""
    from .stats import render
    levers = cfg.load_levers()
    print(render(_store(), simulated=(not live) and levers.dry_run))


@app.command()
def inspect(tool: str, n: int = 1):
    """Print the last raw MCP response(s) for a tool (short name, e.g. get_portfolio)."""
    levers = cfg.load_levers()
    name = tool if tool.startswith("mcp__") else levers.tool_prefix + tool
    rows = _store().all("SELECT * FROM raw_responses WHERE tool_name=? ORDER BY id DESC LIMIT ?", (name, n))
    if not rows:
        print(f"no responses recorded for {name}")
        return
    for r in rows:
        print(f"--- {r['ts']} ok={r['ok']} input={r['input_json']}")
        try:
            print(json.dumps(json.loads(r["response_json"]), indent=2)[:6000])
        except (TypeError, json.JSONDecodeError):
            print(r["response_json"][:6000])


@app.command()
def lesson(text: str):
    """Append a lesson to data/lessons.md (used by /review-day)."""
    lp = paths.lessons_path()
    from datetime import date
    with lp.open("a") as f:
        f.write(f"- {date.today().isoformat()}: {text.strip()}\n")
    print("recorded")


@app.command()
def halt(reason: Optional[str] = typer.Argument(None), clear: bool = typer.Option(False)):
    """Halt new entries for today (or --clear)."""
    from . import marketclock as mc
    store = _store()
    td = mc.trading_date().isoformat()
    if clear:
        store.conn.execute("UPDATE daily SET halted_reason=NULL, updated_at=? WHERE trading_date=?", (journal.now_iso(), td))
        print("halt cleared")
        return
    row = store.one("SELECT * FROM daily WHERE trading_date=?", (td,))
    if row is None:
        store.insert("daily", {"trading_date": td, "day_open_equity": None, "week_open_equity": None,
                               "halted_reason": reason or "manual halt", "updated_at": journal.now_iso()})
    else:
        store.conn.execute("UPDATE daily SET halted_reason=?, updated_at=? WHERE trading_date=?",
                           (reason or "manual halt", journal.now_iso(), td))
    print(f"entries halted for {td}")


@app.command()
def clock():
    """Market phase in ET plus what /cycle has already done today (for the autopilot loop)."""
    from datetime import date
    from . import marketclock as mc
    levers = cfg.load_levers()
    store = _store()
    now = mc.now_et()
    td = mc.trading_date(now)
    extra = {date.fromisoformat(d) for d in levers.market.extra_holidays}
    trading = mc.is_trading_day(td, extra)
    last_of_week = mc.is_last_trading_day_of_week(td, extra)
    last_of_month = mc.is_last_trading_day_of_month(td, extra)
    phase = "closed"
    if trading:
        w = mc.session_window(td)
        if now < w.open:
            phase = "pre_market"
        elif now >= w.close:
            phase = "after_close"
        elif mc.past_cutoff(levers.session.no_new_positions_after, now):
            phase = "open_after_cutoff"
        else:
            phase = "open"
    done = store.kv_get(f"cycle:{td.isoformat()}", {}) or {}
    s = journal.state_view(store, levers)
    out = {
        "et_time": now.strftime("%Y-%m-%d %H:%M ET"), "trading_day": trading, "phase": phase,
        "weekday": now.strftime("%A"), "last_trading_day_of_week": last_of_week, "last_trading_day_of_month": last_of_month,
        "no_new_positions_after": levers.session.no_new_positions_after,
        "done_today": done,
        "open_positions": len(s.open_positions()), "max_open_positions": levers.risk.max_open_positions,
        "free_slots": max(levers.risk.max_open_positions - len(s.open_positions()), 0),
        "trades_today": s.trades_today, "max_trades_per_day": levers.risk.max_trades_per_day,
        "halted": s.halted_reason, "kill_switch": levers.kill_switch, "mode": levers.mode.value, "dry_run": levers.dry_run,
    }
    print(json.dumps(out, indent=2, default=str))


@app.command()
def mark(event: str = typer.Argument(..., help="scan | scan_intraday | review | analyze:<SYMBOL>")):
    """Record that /cycle completed a step today (scan, scan_intraday, review, analyze:SYMBOL)."""
    from . import marketclock as mc
    store = _store()
    td = mc.trading_date().isoformat()
    done = store.kv_get(f"cycle:{td}", {}) or {}
    done[event] = journal.now_iso()
    store.kv_set(f"cycle:{td}", done)
    print(json.dumps(done))


@app.command()
def cashflow(amount: float = typer.Argument(..., help="+deposit / -withdrawal in dollars"),
             note: Optional[str] = typer.Option(None)):
    """Record a deposit or withdrawal the detector missed, so it is not counted as P&L."""
    journal.apply_cashflow(_store(), amount, source="manual", note=note)
    sign = "deposit" if amount > 0 else "withdrawal"
    rprint(f"recorded {sign} of ${abs(amount):,.2f}; day/week P&L baselines shifted")


@app.command()
def note(symbol: str, text: str, source: str = typer.Option("agent")):
    """Record a durable fact about a symbol (behavior, liquidity quirks, past outcomes)."""
    nid = journal.add_note(_store(), symbol, text, source)
    print(f"note #{nid} recorded for {symbol.upper()}")


@app.command()
def notes(symbol: str, limit: int = 10):
    """Show notes for a symbol (newest first)."""
    rows = journal.notes_for(_store(), symbol, limit)
    if not rows:
        print(f"no notes for {symbol.upper()}")
        return
    for r in rows:
        print(f"{r['ts'][:10]} [{r['source']}] {r['note']}")


@app.command()
def lessons(days: int = typer.Option(7, help="Only lessons from the last N days")):
    """Print raw lessons from data/lessons.md within a window (input for /review-week)."""
    from datetime import date, timedelta
    lp = paths.lessons_path()
    if not lp.exists():
        print("no lessons yet")
        return
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    for line in lp.read_text().splitlines():
        if line.startswith("- ") and line[2:12] >= cutoff:
            print(line)


@app.command()
def reviews(days: int = typer.Option(7)):
    """Print the daily review files from the last N days (input for /review-week)."""
    from datetime import date, timedelta
    d = paths.data_dir() / "reviews"
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    for f in sorted(d.glob("*.md")):
        if f.stem >= cutoff:
            print(f"===== {f.name}")
            print(f.read_text())


@app.command()
def hook(event: str):
    """Hook entrypoint (used by .claude/settings.json). Reads Claude Code's JSON from stdin."""
    from .hooks import main
    raise typer.Exit(code=main([event]))


if __name__ == "__main__":
    app()
