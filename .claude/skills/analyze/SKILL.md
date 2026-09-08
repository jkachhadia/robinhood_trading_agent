---
name: analyze
description: Deep-dive one symbol with parallel analyst subagents and an adversarial red-team, then write and register a proposal (or explicitly decline to trade).
disable-model-invocation: true
argument-hint: "TICKER [intraday|swing] [option]"
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *) Write(proposals/**)
---

Arguments: `$ARGUMENTS` (first token is the symbol; optional `intraday` or `swing` (default swing); optional
`option` to consider a single-leg option instead of stock).

## State

```!
./bin/tradeagent status --no-full
```

## Notes on this symbol from past trades

```!
./bin/tradeagent notes $0
```

## Levers that matter here

```!
./bin/tradeagent config --brief
```

## Procedure

1. **Refresh**: `get_equity_quotes` for the symbol; if the state block shows no recent snapshot, also
   `get_portfolio` and `get_equity_positions`. Note whether we already hold it.
2. **Parallel research** (launch all in one message as separate subagents; wait for all):
   - `technical-analyst`: symbol + horizon.
   - `fundamental-analyst`: symbol + horizon.
   - `catalyst-researcher`: symbol + horizon.
   - `options-strategist` only if `option` was requested: symbol, the intended direction, target, horizon.
3. **Synthesize** a draft: direction (long only), entry, structural stop, target, invalidation condition,
   reward:risk, horizon and time stop, three bull and three bear points drawn from *different* analysts,
   catalysts with dates, liquidity numbers, earnings date, and your preliminary confidence. If the analysts
   disagree on direction or the reward:risk is below `risk.min_reward_risk`, stop here and report "no trade"
   with the reason. That is a good outcome.
4. **Red team**: pass the full draft to `red-team`. Apply its required changes. Your final confidence must not
   exceed its "maximum defensible confidence". If its verdict is DO NOT TRADE and you cannot refute the
   objection with evidence from this session, report "no trade".
5. **Write the proposal** to `proposals/<SYMBOL>-<YYYY-MM-DD>-<n>.json` using the schema below, then run
   `./bin/tradeagent propose proposals/<file>`. Read the JSON it prints: status, `max_qty`,
   `binding_constraint`, `rejection_reasons`, `next`. If rejected, fix what is fixable (never by loosening the
   stop or inflating the target) and propose once more at most.

## Proposal schema (all prices per share, or per contract premium for options)

```json
{
  "symbol": "AAPL",
  "instrument": "equity",
  "option": null,
  "side": "buy",
  "is_exit": false,
  "order_type": "limit",
  "limit_price": 150.25,
  "entry_price": 150.25,
  "stop_price": 145.80,
  "target_price": 161.00,
  "invalidation_price": 146.50,
  "invalidation_reason": "daily close back inside the prior range",
  "horizon": "swing",
  "time_stop_hours": 120,
  "thesis": "One falsifiable paragraph.",
  "bull": ["...", "...", "..."],
  "bear": ["...", "...", "..."],
  "catalysts": [{"description": "...", "date": "2026-09-15"}],
  "liquidity": {"avg_dollar_volume": 850000000, "spread_pct": 0.02, "open_interest": null, "avg_volume": 5500000},
  "earnings_date": "2026-10-29",
  "confidence": 0.62,
  "tags": ["breakout", "sector-leader"],
  "evidence": ["get_equity_historicals 2026-03-01..2026-09-08", "red-team verdict PROCEED WITH CHANGES"],
  "requested_qty": null,
  "notes": "red-team required stop moved from 146.00 to 145.80 below the 9/2 swing low"
}
```

Equities are sized in fractional shares when `risk.fractional_shares` is on: pass `max_qty` exactly as printed
(e.g. `1.3333`) as the quantity in both `review_*` and `place_*`. Options are always whole contracts.

For an option: `"instrument": "option"`, `"option": {"expiry": "2026-10-16", "strike": 150, "option_type": "call"}`,
and `entry_price` / `stop_price` / `target_price` / `limit_price` are the premium per contract (the CLI
multiplies by 100). Put open interest and spread % in `liquidity`. Tags can include `earnings_play` to
override the earnings-window rule when the trade is deliberately about earnings.

6. **Symbol notes**: if you learned a durable fact about how this name trades (liquidity, gap behavior,
   spread at the open, reaction to its catalysts), record it: `./bin/tradeagent note <SYMBOL> "..."`.

## Output

Thesis in three lines, the levels, the red-team's strongest objection and how it was handled, the proposal id
and status, `max_qty`, and what happens next (`uv run tradeagent approve <id>` then `/execute`, or `/execute`
directly if auto-eligible). Never place an order from this skill.
