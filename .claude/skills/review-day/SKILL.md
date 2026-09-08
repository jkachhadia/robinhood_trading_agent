---
name: review-day
description: End-of-day review - reconcile broker orders with the journal, compute P&L and stats, write post-mortems for closed trades, and record lessons.
disable-model-invocation: true
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Write(data/reviews/**)
---

## Journal

```!
./bin/tradeagent status
```

```!
./bin/tradeagent orders --limit 30
```

```!
./bin/tradeagent stats
```

## Procedure

1. **Reconcile**: `get_equity_orders` and `get_option_orders` for today, `get_realized_pnl`, `get_portfolio`,
   `get_equity_positions`, `get_option_positions`. Compare with the journal above. Any order at the broker
   that the journal does not know about, or vice versa, is a finding: list it with ids. (In dry run, compare
   simulated fills against what the market actually did instead.)
2. **P&L**: realized today, unrealized on open positions, day and week drawdown against the limits. If a limit
   was breached, say when and which orders were denied because of it (`./bin/tradeagent decisions --limit 30`).
3. **Post-mortems**: for each trade closed today (from stats/orders), 3 lines: what the plan was, what
   happened (fill quality, whether the stop/target/time-stop was respected), and the one thing to change.
   Grade the *process*, not the outcome: a good trade can lose.
4. **Denials and friction**: list gate denials today and whether each was correct. If the gate blocked
   something that should have gone through, say which lever or parser to adjust (do not change it yourself).
5. **Symbol notes**: durable per-name facts learned today go to `./bin/tradeagent note SYMBOL "..."`
   (not into lessons).
6. **Lessons**: at most 5, each a single specific sentence that would change a future decision. Record each with
   `./bin/tradeagent lesson "..."`. Skip generic advice.
7. **Write** `data/reviews/<YYYY-MM-DD>.md` with sections: P&L, trades, post-mortems, denials, lessons,
   tomorrow's watch (positions near levels, catalysts, proposals still pending).

## Output

The same content as the review file, compressed to fit one screen, ending with what the user must do (approve,
reject, or fund) before the next session.
