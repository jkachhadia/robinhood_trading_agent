---
name: cycle-research
description: Forked research step for the autopilot - runs scan+analyze, an intraday scan, the daily review, or the weekly review in an isolated context and returns a short summary. Invoked by /cycle; can be run by hand with a phase argument.
argument-hint: "scan | intraday | review | review-week"
context: fork
agent: researcher
background: false
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Write(proposals/**) Write(data/reviews/**) Write(data/playbook.md)
---

Phase: `$ARGUMENTS`

```!
./bin/tradeagent clock
```

```!
./bin/tradeagent status --no-full
```

Do exactly one of the following, then stop with the summary the researcher agent prescribes.

- **scan**: run the `/scan` procedure (swing). Then run the `/analyze` procedure for the top candidates,
  at most `free_slots` from the clock output and never more than 2. For each analyzed symbol run
  `./bin/tradeagent mark analyze:<SYMBOL>`; when finished run `./bin/tradeagent mark scan`. If the scan
  yields nothing worth analyzing, say so and still mark `scan`.
- **intraday**: run `/scan intraday`, then `/analyze <best> intraday` for at most 1 candidate, then
  `./bin/tradeagent mark scan_intraday`.
- **review**: run the `/review-day` procedure, then `./bin/tradeagent mark review`.
- **review-week**: run the `/review-week` procedure, then `./bin/tradeagent mark review_week`.

Never place or cancel orders here. Do not run `/manage` or `/execute`; the main session does those.
