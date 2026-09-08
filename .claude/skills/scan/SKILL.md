---
name: scan
description: Market scan - refresh account state, run Robinhood scanners and watchlists, check the earnings and macro calendar, and produce a ranked shortlist of candidates to analyze. Never proposes or trades.
disable-model-invocation: true
argument-hint: "[intraday]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

Mode: `$ARGUMENTS` (empty = swing scan; `intraday` = intraday scan)

## State

```!
./bin/tradeagent status --no-full
```

## Procedure

1. **Refresh the account** (the gate needs a fresh snapshot): `get_portfolio`, `get_equity_positions`,
   `get_option_positions`, and `get_equity_orders` for open orders. If the state block above shows halted,
   kill switch, or a daily/weekly loss breach, stop after reporting: no scan is needed today.
2. **Tape**: `get_index_quotes` for the major indexes; one WebSearch for today's market drivers and the
   economic calendar for the next 3 trading days. Classify the regime in one line (risk-on / risk-off /
   choppy, trending up / down) and say what setups that regime favors.
3. **Candidates**:
   - `get_scans` then `run_scan` on the available scans that match the regime (momentum/breakout, relative
     volume, pullback-in-uptrend, gap-and-hold; for intraday: relative volume and gap scans). If no suitable
     saved scan exists, `get_scanner_filter_specs` and `create_scan` one that does, then run it.
   - `get_watchlists` / `get_watchlist_items` and `get_popular_watchlists` for names the user tracks.
   - Existing positions: note any that are near stop/target (from the plan in `./bin/tradeagent plan`).
4. **Filter** with the universe levers: price ≥ `universe.min_price`, average dollar volume ≥
   `universe.min_avg_dollar_volume`, not on the blocklist, and check `get_earnings_calendar` so anything with
   earnings inside `universe.exclude_earnings_within_days` is flagged (it can still be an `earnings_play`
   candidate, but say so).
5. **Rank** at most 8 names. For each: symbol, setup type, why now (one line with a number), catalyst and date,
   liquidity (avg $ volume), earnings date, and a risk flag. Order by expected edge, not by excitement.

## Output

- Regime line.
- Shortlist table.
- Top 1–3 to run `/analyze` on, with the horizon (`intraday` or `swing`) and whether to include `option`.
- Anything about existing positions that `/manage` should handle now.

Rules: do not create proposals here, do not place orders, do not spend more than ~15 tool calls. Numbers
come from tools; if a scan tool's output is unclear, say what you saw rather than inventing structure. For
`intraday`, only run before 15:00 ET; otherwise say the window has closed and suggest a swing scan.
