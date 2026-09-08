"""Claude Code hook entrypoint.

    python -m tradeagent.hooks <event>

Reads the hook payload from stdin, writes the hook response to stdout.
Events: pre-tool-use, post-tool-use, post-tool-use-failure, session-start,
user-prompt-submit, pre-compact, stop.

Fail-closed policy: if anything goes wrong while deciding on an order-placing
tool, exit 2 with the error on stderr, which makes Claude Code block the call.
For every other event, errors are logged and the hook exits 0 so a bookkeeping
bug never breaks the session.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from . import fieldmap as fm
from . import journal, paths
from .config import load_levers
from .db import Store
from .gate import evaluate
from .models import Instrument, ParsedOrder, ProposalStatus


def _log(msg: str) -> None:
    try:
        with paths.log_path().open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError:
        pass


def _headless() -> bool:
    return os.environ.get("TRADEAGENT_HEADLESS", "").strip() in ("1", "true", "yes")


def _read_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


# ---- events -------------------------------------------------------------------

def pre_tool_use(payload: dict) -> int:
    levers = load_levers()
    tool_name = str(payload.get("tool_name", ""))
    if not tool_name.startswith(levers.tool_prefix):
        return 0
    short = fm.short_tool_name(tool_name, levers.tool_prefix)
    gated = fm.is_place(short) or fm.is_cancel(short)
    try:
        store = Store()
        decision = evaluate(levers, store, payload, headless=_headless())
        if decision is None:
            return 0
        _log(f"PRE {tool_name} -> {decision.action.value} [{decision.rule}] {decision.reason}")
        _emit(decision.to_hook_output())
        return 0
    except Exception as e:  # fail closed for order tools
        _log(f"PRE {tool_name} EXCEPTION {e!r}\n{traceback.format_exc()}")
        if gated:
            sys.stderr.write(f"tradeagent gate error, blocking {short}: {e!r}\n")
            return 2
        return 0


def _save_sample(short: str, payload: dict) -> None:
    """Keep the first raw response per tool for parser tuning (data/samples/<tool>.json)."""
    try:
        d = paths.samples_dir()
        f = d / f"{short}.json"
        if not f.exists():
            f.write_text(json.dumps({"tool_input": payload.get("tool_input"),
                                     "tool_response": payload.get("tool_response")}, indent=2, default=str))
    except OSError:
        pass


def post_tool_use(payload: dict) -> int:
    levers = load_levers()
    tool_name = str(payload.get("tool_name", ""))
    if not tool_name.startswith(levers.tool_prefix):
        return 0
    short = fm.short_tool_name(tool_name, levers.tool_prefix)
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response")
    session_id = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    try:
        store = Store()
        decoded = fm.decode_tool_response(response)
        journal.record_raw(store, session_id, tool_name, tool_use_id, tool_input, response, ok=True)
        _save_sample(short, payload)

        if short in fm.PORTFOLIO_TOOLS:
            info = journal.record_snapshot(store, session_id, short, decoded)
            _log(f"POST {short} snapshot {info}")
        elif short in fm.POSITION_TOOLS:
            n = journal.record_positions(store, decoded, fm.instrument_for_tool(short), source="broker")
            _log(f"POST {short} positions={n}")
        elif short in fm.QUOTE_TOOLS:
            n = journal.record_quotes(store, decoded, fm.instrument_for_tool(short))
            if levers.dry_run:
                journal.rebuild_simulated_positions(store)
            _log(f"POST {short} quotes={n}")
        elif fm.is_review(short):
            mh = None
            try:
                mh = fm.parse_order(tool_name, short, tool_input).match_hash()
            except ValueError as e:
                _log(f"POST {short} unparsable review input: {e}")
            journal.record_review(store, session_id, tool_name, mh, tool_input, response)
            _log(f"POST {short} review hash={mh}")
        elif fm.is_place(short):
            order = fm.parse_order(tool_name, short, tool_input)
            pid, is_exit = _proposal_for_order(store, order)
            oid, status = journal.record_order(store, session_id, tool_name, tool_use_id, order, tool_input,
                                               response, decoded, pid, is_exit)
            _log(f"POST {short} order #{oid} {status} proposal={pid}")
            if status == "failed":
                _emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                              "additionalContext": f"tradeagent: broker response looks like a rejection for order #{oid}; verify with get_*_orders before retrying."}})
        elif fm.is_cancel(short):
            _log(f"POST {short} {tool_input}")
        return 0
    except Exception as e:
        _log(f"POST {tool_name} EXCEPTION {e!r}\n{traceback.format_exc()}")
        return 0


def _proposal_for_order(store: Store, order: ParsedOrder) -> tuple[Optional[int], bool]:
    row = store.one(
        "SELECT id, is_exit FROM proposals WHERE instrument_key=? AND side=? AND status IN ('executing','pending','approved','auto_eligible') "
        "ORDER BY CASE status WHEN 'executing' THEN 0 ELSE 1 END, id DESC LIMIT 1",
        (order.instrument_key(), order.side.value),
    )
    if row is None:
        return None, order.side.value == "sell"
    return int(row["id"]), bool(row["is_exit"])


def post_tool_use_failure(payload: dict) -> int:
    levers = load_levers()
    tool_name = str(payload.get("tool_name", ""))
    if not tool_name.startswith(levers.tool_prefix):
        return 0
    short = fm.short_tool_name(tool_name, levers.tool_prefix)
    try:
        store = Store()
        err = payload.get("error") or payload.get("tool_response")
        journal.record_raw(store, payload.get("session_id"), tool_name, payload.get("tool_use_id"),
                           payload.get("tool_input"), err, ok=False)
        if fm.is_place(short):
            order = None
            try:
                order = fm.parse_order(tool_name, short, payload.get("tool_input") or {})
            except ValueError:
                pass
            pid = None
            if order is not None:
                pid, _ = _proposal_for_order(store, order)
            journal.record_order_failure(store, payload.get("session_id"), tool_name, payload.get("tool_use_id"),
                                         order, payload.get("tool_input"), err, pid)
        _log(f"FAIL {short} {str(err)[:200]}")
        return 0
    except Exception as e:
        _log(f"FAIL {tool_name} EXCEPTION {e!r}")
        return 0


def session_start(payload: dict) -> int:
    from .report import context_text
    try:
        levers = load_levers()
        store = Store()
        journal.start_run(store, payload.get("session_id"), os.environ.get("TRADEAGENT_RUN_KIND"),
                          _headless(), levers.mode.value)
        _emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context_text(store, levers)}})
        return 0
    except Exception as e:
        _log(f"SESSION_START EXCEPTION {e!r}\n{traceback.format_exc()}")
        return 0


def user_prompt_submit(payload: dict) -> int:
    from .report import context_text
    try:
        levers = load_levers()
        store = Store()
        _emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context_text(store, levers)}})
        return 0
    except Exception as e:
        _log(f"PROMPT EXCEPTION {e!r}")
        return 0


def pre_compact(payload: dict) -> int:
    """Snapshot the in-flight state to data/inflight.md before Claude Code compacts the conversation.
    The DB is the source of truth; this file is for humans reading the logs, and the SessionStart hook
    (which also fires after compaction) re-injects the same facts from the DB."""
    try:
        levers = load_levers()
        store = Store()
        lines = journal.inflight_lines(store, levers)
        f = paths.data_dir() / "inflight.md"
        f.write_text(f"# in-flight at compaction {datetime.now(timezone.utc).isoformat()} (session {payload.get('session_id')})\n"
                     + "\n".join("- " + l for l in lines) + "\n")
        _log(f"PRECOMPACT wrote {len(lines)} in-flight lines")
        return 0
    except Exception as e:
        _log(f"PRECOMPACT EXCEPTION {e!r}")
        return 0


def stop(payload: dict) -> int:
    try:
        store = Store()
        journal.end_run(store, payload.get("session_id"))
        return 0
    except Exception as e:
        _log(f"STOP EXCEPTION {e!r}")
        return 0


HANDLERS = {
    "pre-tool-use": pre_tool_use,
    "post-tool-use": post_tool_use,
    "post-tool-use-failure": post_tool_use_failure,
    "session-start": session_start,
    "user-prompt-submit": user_prompt_submit,
    "pre-compact": pre_compact,
    "stop": stop,
}


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in HANDLERS:
        sys.stderr.write(f"usage: python -m tradeagent.hooks <{'|'.join(HANDLERS)}>\n")
        return 1
    event = argv[0]
    try:
        payload = _read_input()
    except json.JSONDecodeError as e:
        _log(f"{event}: bad JSON on stdin: {e}")
        if event == "pre-tool-use":
            sys.stderr.write("tradeagent: malformed hook input; blocking\n")
            return 2
        return 0
    return HANDLERS[event](payload)


if __name__ == "__main__":
    sys.exit(main())
