---
name: autopilot
description: Hands-free trading inside this session - runs /cycle now and then every 30 minutes until the session ends or /kill.
disable-model-invocation: true
argument-hint: "[interval, default 30m]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

Interval: `$ARGUMENTS` (default `30m`).

```!
./bin/tradeagent config --brief
```

1. If `mode` above is `approve_all`, warn in one line that every order will queue for approval and the loop
   will keep proposing without executing; suggest `/mode tiered` or `/mode autonomous`. Do not change it yourself.
2. State in one line whether `dry_run` is on (simulated) or off (real money).
3. Run the `/cycle` procedure once now.
4. Then start the loop by invoking the `loop` skill with arguments `<interval> /cycle` (for example
   `30m /cycle`). This schedules `/cycle` to re-run in this session at that interval.
5. Tell the user how to stop, and that the loop is designed to run for weeks: research runs in forked
   contexts, only summaries stay in this session, and the state block re-injects the journal after any
   context compaction. Stop: `/kill` blocks orders immediately; closing the session ends the loop.
