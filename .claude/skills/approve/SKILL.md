---
name: approve
description: Approve a pending proposal by id so /execute may place it.
disable-model-invocation: true
argument-hint: "<proposal-id> [note]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

Run `./bin/tradeagent show $0` and restate the trade in one line (side, symbol, qty cap, limit, stop, target,
R:R, confidence). Then run `./bin/tradeagent approve $0`. Report the result and remind the user that
`/execute` places approved proposals. Do not place the order from this skill.
