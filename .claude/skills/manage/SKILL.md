---
name: manage
description: Manage open positions and working orders against their plans - stops, targets, invalidation, time stops, stale orders - and execute exits through the gate.
disable-model-invocation: true
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Write(proposals/**)
---

## State

```!
./bin/tradeagent status --no-full
```

## Procedure

1. **Refresh**: `get_portfolio`, `get_equity_positions`, `get_option_positions`, then `get_equity_quotes` for
   every equity position and `get_option_quotes` for every option position (the plan check below uses the
   quotes the gate has cached, so fetch them first). Also `get_equity_orders` and `get_option_orders`.
2. **Plans**: run `./bin/tradeagent plan`. It prints each open position with its entry proposal's stop, target,
   invalidation, time stop, hours open, computed flags, and any `symbol_notes` recorded for the name.
3. **Decide per position**, in this order of precedence:
   - `AT_OR_BELOW_STOP` or `INVALIDATED` → full exit now.
   - `AT_OR_ABOVE_TARGET` → exit at least half; keep the rest only if the technical picture has improved (one
     quick `get_equity_historicals` check) and set a note with the new plan. Do not widen anything.
   - `TIME_STOP` → exit unless the position is within 1 ATR of target with the thesis intact.
   - `NO_PLAN_ON_FILE` → this position was not opened by the agent; report it and do not touch it unless the
     user asks.
   - Otherwise: one WebSearch for the symbol for news since the entry; if a catalyst has invalidated the
     thesis (guidance cut, regulatory action, the specific `invalidation_reason`), exit. Do not exit on noise.
   - Options: additionally exit if DTE has fallen below `options.dte_min` / 2 or the premium has lost 50%
     of its value with the underlying below invalidation.
4. **Exits**: write an exit proposal JSON to `proposals/<SYMBOL>-exit-<YYYY-MM-DD>-<n>.json`:
   `{"symbol": ..., "instrument": ..., "option": <leg or null>, "side": "sell", "is_exit": true,
   "order_type": "limit", "limit_price": <at or slightly below bid>, "entry_price": <same>, "requested_qty": <qty>,
   "thesis": "<why exiting, which flag>", "confidence": 0.9, "tags": ["exit", "<flag>"], "evidence": [...]}`
   then `./bin/tradeagent propose <file>`, then `review_*_order` and `place_*_order` with identical params.
   Exits are auto-eligible in tiered and autonomous modes; in approve_all they queue, so say so clearly.
5. **Working orders**: cancel any entry limit order older than 2 hours that is more than 1% away from the
   market (cancels are always allowed), and any exit order that no longer matches the plan; re-place through
   the gate if still wanted.
6. Finish with `./bin/tradeagent orders --limit 10`.

## Output

Per position: instrument, qty, price vs stop/target, flag, action taken (held / exited / queued / cancelled
order), with order ids. Then any position needing the user's decision and why.

Never add to a position from this skill, never move a stop down, never re-enter a name that just stopped out.
