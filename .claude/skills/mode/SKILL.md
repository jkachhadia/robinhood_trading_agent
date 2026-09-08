---
name: mode
description: Switch the autonomy mode (approve_all, tiered, autonomous) or show the current one.
disable-model-invocation: true
argument-hint: "[approve_all|tiered|autonomous]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

Argument: `$ARGUMENTS`

If no argument was given, run `./bin/tradeagent mode` and explain the current mode in one line.

Otherwise run `./bin/tradeagent mode $ARGUMENTS`, then confirm the new mode and state, in plain terms, what
now executes without a human:
- **approve_all**: nothing. Every order queues (headless) or prompts (interactive).
- **tiered**: orders up to `tiered.auto_max_notional_usd` with confidence ≥ `tiered.auto_min_confidence`, in
  symbols already traded (unless `new_symbol_requires_approval` is false), excluding options if
  `options_require_approval` is true; risk-reducing exits if `exits_auto_allowed`.
- **autonomous**: everything inside the hard limits (daily/weekly loss, notional, position %, count, hours,
  universe, options rules).

Also remind the user whether `dry_run` is on. Do not change any other lever.
