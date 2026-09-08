---
name: cycle
description: One hands-free autopilot tick - decides from the market clock whether to scan, analyze, execute, manage, or review, then does it. Meant to be run on a loop (/autopilot).
disable-model-invocation: true
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Write(proposals/**) Write(data/reviews/**)
---

## Clock and progress

```!
./bin/tradeagent clock
```

## State

```!
./bin/tradeagent status --no-full
```

## Decide, then act (one pass, no questions)

Read `phase`, `done_today`, `free_slots`, `trades_today`, `halted`, `kill_switch`. Then:

- **kill_switch true or halted** → report in one line and stop.
- **phase = closed** (weekend/holiday) → say "market closed, nothing to do" and stop.
- **phase = pre_market**:
  - if `scan` not in `done_today`: run the `/scan` procedure, then for the top candidates (at most
    `free_slots`, max 2) run the `/analyze` procedure for each, then `./bin/tradeagent mark scan` and
    `./bin/tradeagent mark analyze:<SYMBOL>` per symbol analyzed.
  - otherwise stop; entries wait for the open.
- **phase = open**:
  1. run the `/manage` procedure (positions vs plan, exits, stale orders).
  2. run the `/execute` procedure for any approved or auto-eligible proposals.
  3. if `free_slots` > 0 and `trades_today` < `max_trades_per_day`:
     - if `scan` not in `done_today`: `/scan` then `/analyze` up to 2 candidates, mark them; then `/execute` again.
     - else if it is after 13:00 ET and `scan_intraday` not in `done_today`: `/scan intraday`, `/analyze` at most
       1 candidate with horizon `intraday`, mark `scan_intraday`, then `/execute` again.
- **phase = open_after_cutoff**: run the `/manage` procedure only.
- **phase = after_close**: if `review` not in `done_today`: run the `/review-day` procedure, then
  `./bin/tradeagent mark review`. Otherwise stop.

Rules for this tick: follow the linked skills' procedures exactly (they are in `.claude/skills/*/SKILL.md`; read
the one you need if it is not already in context). Never ask the user anything; if something needs a human
(a queued proposal, an unparseable response) note it in the summary and continue. Keep the whole tick under
~60 tool calls; if you run out, finish `/manage` first, everything else can wait for the next tick.

## Output

Five lines max: phase, what ran, orders placed/simulated (ids), proposals created (ids, status), anything
waiting on the user.
