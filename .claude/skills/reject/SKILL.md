---
name: reject
description: Reject a pending proposal by id with a reason.
disable-model-invocation: true
argument-hint: "<proposal-id> [reason]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

Run `./bin/tradeagent reject $ARGUMENTS` (the first token is the id; the rest, if any, is the reason - quote
it). Confirm in one line. If the user gave a reason that sounds like a lesson about the process (not just
this trade), offer to record it with `./bin/tradeagent lesson "..."`.
