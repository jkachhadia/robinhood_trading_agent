---
name: review-week
description: Weekly consolidation - turns the week's stats, daily reviews and raw lessons into a bounded playbook (data/playbook.md) the agent reads every prompt; on the last week of a month also drafts a lever review for the user.
disable-model-invocation: true
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Write(data/playbook.md) Write(data/reviews/**) Read
---

## Inputs

```!
./bin/tradeagent clock
```

```!
./bin/tradeagent stats
```

```!
./bin/tradeagent lessons --days 7
```

```!
./bin/tradeagent reviews --days 7
```

```!
./bin/tradeagent config --brief
```

## Current playbook

Read `data/playbook.md` if it exists (it may not, the first week).

## Procedure

1. **Rewrite `data/playbook.md`** as a complete replacement, at most 40 non-empty lines, in this shape:
   - `# Playbook (updated YYYY-MM-DD, N closed trades)`
   - `## What is working` — setups/tags/horizons/confidence buckets with positive expectancy and enough
     samples (n ≥ 5), each with the number (win rate, avg R, n).
   - `## What is not` — the opposite, with the number, and the rule that follows ("no X until n ≥ 10 and
     avg R > 0").
   - `## Process rules` — at most 10 durable, specific rules distilled from the lessons and reviews
     (merge duplicates, drop anything contradicted by the stats, keep rules that changed a decision).
   - `## Watch` — open questions to test next week.
   Keep older rules that still hold; this is a rolling document, not a weekly diary. Only the injected
   first 40 lines reach the agent, so put the most decision-relevant lines first.
2. **Symbol notes**: for any symbol traded this week with a durable behavioral fact (gaps on news, wide
   pre-market spread, fills poorly at the open), record it with `./bin/tradeagent note SYMBOL "..."`.
3. **Monthly lever review** (only when `last_trading_day_of_month` is true, or the user asked): write
   `data/reviews/levers-YYYY-MM.md` proposing changes to `config/levers.yaml` with the statistic behind each
   (e.g. "risk_per_trade_pct 3 → 2: avg R +0.4 but 30% of trades hit daily loss halt"). Never edit the config
   yourself; the user applies it.
4. Finish with `./bin/tradeagent mark review_week` if you were invoked from the cycle.

## Output

The new playbook's "What is working" and "What is not" lines, the rules that changed, and the lever
proposals if any. Under 20 lines.
