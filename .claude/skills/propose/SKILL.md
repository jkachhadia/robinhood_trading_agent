---
name: propose
description: Validate, size and register a proposal JSON file with the gate.
disable-model-invocation: true
argument-hint: "<path/to/proposal.json>"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Read
---

Run `./bin/tradeagent propose $ARGUMENTS`. Interpret the JSON result for the user: id, status, max quantity
and which constraint bound it, notional, and any rejection reasons with what would fix each. If the file is
invalid, show the validation error and the field that needs fixing. Do not edit the proposal's stop or target
to force acceptance; if reward:risk is too low the answer is "no trade", not a wider target.
