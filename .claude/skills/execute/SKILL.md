---
name: execute
description: Place every approved or auto-eligible proposal through review-then-place, respecting the gate, and confirm fills.
disable-model-invocation: true
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

## State and queue

```!
./bin/tradeagent status
```

```!
./bin/tradeagent proposals --status approved
```

```!
./bin/tradeagent proposals --status auto_eligible
```

## Procedure

If there is nothing approved or auto-eligible, say so and stop. If the state shows kill switch, halted, or a
loss-limit breach, stop and report.

1. **Refresh**: `get_portfolio`, `get_equity_positions`, `get_option_positions`. The gate requires a snapshot
   younger than `session.require_fresh_snapshot_minutes` for live orders.
2. For each proposal, in id order (`./bin/tradeagent show <id>` for the full record):
   a. Fetch a fresh quote (`get_equity_quotes` or `get_option_quotes` for the exact leg).
   b. If the ask (for buys) is more than `session.limit_price_tolerance_pct` above the proposal's limit,
      **skip** it and report "price moved"; do not chase, do not edit the proposal.
   c. Quantity = the proposal's `max_qty` (may be fractional for equities, e.g. `1.3333`; pass it exactly) unless
      you have a reason to go smaller (say why). Never larger.
   d. Call `review_equity_order` / `review_option_order` with the exact parameters you will place: symbol,
      side, quantity, order type `limit`, limit price = proposal limit, and the option leg if any. Read the
      review response for estimated cost, fees, warnings, or rejection reasons. If it warns about buying
      power or tradability, stop for that proposal and report.
   e. Call the matching `place_*_order` with **identical** parameters.
   f. Read the result. The gate may respond instead of the broker:
      - "DRY RUN ... simulated fill" → treat as filled; continue to the next proposal.
      - "queued for human approval" → continue to the next proposal; list it for the user at the end.
      - any other denial → do not retry; record the reason for the report.
   g. If the broker accepted: `get_equity_orders` / `get_option_orders` to confirm state and capture the
      broker order id. If it is a working limit that has not filled after your other proposals are done, leave
      it working and note it.
3. Finish with `./bin/tradeagent orders --limit 10`.

## Output

Per proposal: id, action taken (placed / simulated / queued / skipped / denied), quantity, limit, broker order
id and state. Then: what is still queued for the user, and any working orders `/manage` should watch.
