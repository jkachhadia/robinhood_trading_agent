---
name: cycle
description: One hands-free autopilot tick - manage and execute in this session, and delegate scan/analyze/review to a forked research context so the session stays small for weeks. Meant to be run on a loop (/autopilot).
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Write(proposals/**)
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

Read `phase`, `done_today`, `free_slots`, `trades_today`, `halted`, `kill_switch`, `last_trading_day_of_week`.

- **kill_switch true or halted** → report in one line and stop.
- **phase = closed** (weekend/holiday) → "market closed, nothing to do" and stop.
- **phase = pre_market**:
  - if `scan` not in `done_today`: invoke the `cycle-research` skill with argument `scan` and wait for it.
  - otherwise stop; entries wait for the open.
- **phase = open**:
  1. Run the `/manage` procedure here (positions vs plan, exits, stale orders). Keep it lean: quotes only for
     held names, one WebSearch per position at most.
  2. Run the `/execute` procedure here for approved or auto-eligible proposals.
  3. If `free_slots` > 0 and `trades_today` < `max_trades_per_day`:
     - if `scan` not in `done_today`: invoke `cycle-research scan`, then run `/execute` again.
     - else if the ET time is after 13:00 and `scan_intraday` not in `done_today`: invoke
       `cycle-research intraday`, then `/execute` again.
- **phase = open_after_cutoff**: run the `/manage` procedure only.
- **phase = after_close**:
  - if `review` not in `done_today`: invoke `cycle-research review`.
  - then, if `last_trading_day_of_week` is true and `review_week` not in `done_today`: invoke
    `cycle-research review-week`, and **renew the loop**: use `CronList` to find the recurring `/cycle`
    task, create an identical new one with `CronCreate` (same cron expression, recurring), then
    `CronDelete` the old id. Claude Code expires recurring tasks after 7 days; this weekly renewal keeps
    the autopilot alive indefinitely.
  - otherwise stop.

Rules for this tick: the research skill runs in its own context and returns a summary; do not repeat its
work here. Never ask the user anything; if something needs a human (a queued proposal, an unparseable
response) put it in the summary and continue. Keep this session's part of the tick under ~25 tool calls.

## Output

Five lines max: phase, what ran here and what was delegated, orders placed/simulated (ids), proposals
created (ids, status), anything waiting on the user.
