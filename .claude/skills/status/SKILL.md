---
name: status
description: Show trading agent state - mode, dry run, P&L vs limits, positions, pending approvals, recent decisions.
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

## Current state

```!
./bin/tradeagent status
```

## Recent gate decisions

```!
./bin/tradeagent decisions --limit 8
```

## Instructions

Summarize the above in at most 8 lines for the user: mode and safety flags, P&L against the daily and weekly
limits, open positions with any flagged risk, anything pending approval (with the exact `uv run tradeagent
approve <id>` command), and any denial in recent decisions that needs attention. Do not call any MCP tool for
this; the numbers above are what the gate sees. If equity shows n/a or the snapshot is stale and the user is
about to trade, tell them to run `/execute` or `/manage`, which refresh it.
