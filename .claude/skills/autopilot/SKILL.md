---
name: autopilot
description: Hands-free trading inside this session - runs /cycle now and then every 30 minutes until the session ends or /kill.
disable-model-invocation: true
argument-hint: "[interval, default 30m]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

Interval: `$ARGUMENTS` (default `30m`).

```!
./bin/tradeagent doctor || true
```

```!
./bin/tradeagent config --brief
```

1. If the doctor output lists any `problems`, stop and show them to the user; do not start the loop.
2. If `mode` above is `approve_all`, warn in one line that every order will queue for approval and the loop
   will keep proposing without executing; suggest `/mode tiered` or `/mode autonomous`. Do not change it yourself.
3. State in one line whether `dry_run` is on (simulated) or off (real money).
4. Run the `/cycle` procedure once now.
5. Then start the loop with `CronCreate`: recurring, prompt `/cycle`, cron `3,33 8-16 * * 1-5` when the
   interval is the default 30m (weekdays 08:03 to 16:33 local time, minutes chosen to avoid the :00/:30
   jitter), or the equivalent for a custom interval. Confirm the job id. If your local timezone is not
   US/Eastern, widen the hour range so 08:00 to 16:30 ET is covered. `/cycle` renews this task weekly
   because Claude Code expires recurring tasks after 7 days.
6. Tell the user how to stop, and that the loop is designed to run for weeks: research runs in forked
   contexts, only summaries stay in this session, and the state block re-injects the journal after any
   context compaction. Stop: `/kill` blocks orders immediately; closing the session ends the loop.
