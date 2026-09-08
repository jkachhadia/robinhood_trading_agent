---
name: kill
description: Emergency stop - flips the kill switch so no order can be placed, then offers to cancel working orders.
disable-model-invocation: true
argument-hint: "[off]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

Argument: `$ARGUMENTS`

If the argument is `off`: run `./bin/tradeagent kill off`, confirm, and stop.

Otherwise:
1. Run `./bin/tradeagent kill on` immediately. Do this before anything else.
2. Fetch working orders with `get_equity_orders` and `get_option_orders`. List any that are open, pending,
   queued or partially filled (id, symbol, side, qty, price).
3. Ask the user whether to cancel them. If they say yes, cancel each with the matching `cancel_*_order` tool
   (cancels are always allowed by the gate) and confirm each cancellation by re-fetching orders.
4. Report: kill switch ON, orders cancelled (ids), open positions still held (from `get_equity_positions` and
   `get_option_positions`), and that `/kill off` re-enables trading.

Do not open or close positions. Do not touch the mode or dry_run.
