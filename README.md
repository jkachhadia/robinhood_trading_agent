# trading_agent

A Claude Code trading agent for a Robinhood **Agentic** brokerage account, wrapped in an enforcement layer
that the model cannot bypass.

- **Agent**: Claude Code itself (your subscription, no API key), connected to Robinhood's official Trading
  MCP server (`https://agent.robinhood.com/mcp/trading`).
- **Levers and permissioning**: `tradeagent`, a small Python package driven by Claude Code hooks. Every
  `place_*_order` call passes through a gate (proposal match → review-before-place → hard limits → autonomy
  mode) before it can reach the broker. Everything is journaled in SQLite.
- **Rigor**: the analysis protocol lives in `CLAUDE.md`, the skills in `.claude/skills/`, and five read-only
  subagents (technical, fundamental, catalyst, options, red-team) in `.claude/agents/`.

Robinhood enforces almost nothing on its side (no per-order caps, no loss limits, no kill switch; "approve
first" is just an instruction to the agent). This repo is where those controls live.

## Setup

```bash
# 1. Python side (uv: https://docs.astral.sh/uv/)
uv sync
uv run tradeagent init            # writes config/levers.yaml (defaults: approve_all + dry_run ON) and data/tradeagent.db
uv run pytest                     # 49 tests

# 2. Claude Code side (run from this folder, interactively, on a desktop)
claude                            # accept workspace trust; approve the project MCP server when asked
/mcp                              # authenticate robinhood-trading (browser OAuth; creates the Agentic account on first connect)
/status                           # should render the gate state
```

Then in that same session try the read-only flow: ask for `get_portfolio` and `get_equity_positions`. The
first raw response per tool is saved under `data/samples/` so the parsers can be checked
(`uv run tradeagent inspect get_portfolio`). If `/status` shows equity `n/a` after `get_portfolio` ran, the
equity field name is not in `tradeagent/fieldmap.py` yet; add it to `EQUITY_KEYS_LIST`.

## Daily use

| Command | What it does |
|---|---|
| `/status` | mode, dry run, P&L vs limits, positions, pending approvals |
| `/scan [intraday]` | scanners, watchlists, earnings/macro calendar → ranked shortlist (never trades) |
| `/analyze TICKER [swing\|intraday] [option]` | parallel analysts + red-team → proposal JSON → `tradeagent propose` |
| `/execute` | review → place every approved / auto-eligible proposal through the gate |
| `/manage` | positions vs their plan (stop / target / invalidation / time stop) → exits |
| `/review-day` | reconcile, P&L, post-mortems, lessons, `data/reviews/<date>.md` |
| `/pending`, `/approve <id>`, `/reject <id>` | the approval queue |
| `/mode approve_all\|tiered\|autonomous`, `/kill [off]` | autonomy and emergency stop |

Terminal equivalents: `uv run tradeagent status | pending | approve <id> | reject <id> | mode <m> | kill on|off |
dry-run on|off | orders | decisions | positions | plan | stats | inspect <tool> | show <id> | reset <id> | halt`.

## Levers (`config/levers.yaml`)

```yaml
mode: approve_all        # approve_all | tiered | autonomous   (uv run tradeagent mode ...)
kill_switch: false       # uv run tradeagent kill on|off
dry_run: true            # paper mode: the gate denies the order and journals a simulated fill instead
risk:      max_order_notional_usd, max_position_pct, max_open_positions, max_daily_loss_pct,
           max_weekly_loss_pct, risk_per_trade_pct, min_reward_risk, max_trades_per_day,
           cooldown_minutes_same_symbol, equity_floor_usd
tiered:    auto_max_notional_usd, auto_min_confidence, new_symbol_requires_approval,
           options_require_approval, exits_auto_allowed
universe:  allowlist, blocklist, min_price, min_avg_dollar_volume, exclude_earnings_within_days
options:   enabled, max_premium_per_trade_usd, max_options_exposure_pct, dte_min, dte_max,
           max_contracts, min_open_interest, max_bid_ask_spread_pct
session:   allowed_hours, no_new_positions_after, require_fresh_snapshot_minutes,
           require_review_before_place, require_proposal_for_place, proposal_ttl_hours,
           limit_price_tolerance_pct
```

The YAML is re-read on every tool call. Runtime toggles (`mode`, `kill_switch`, `dry_run`) go to
`data/overrides.json` and win over the YAML. Claude Code is denied write access to both files, to the hooks,
and to the `tradeagent/` package by `.claude/settings.json`.

### What the gate does on `place_*_order`

1. subagent? kill switch? → deny
2. parse the order (unknown field names → deny, fail closed)
3. match a stored proposal (symbol, side, instrument, qty ≤ sized max, limit within tolerance) → else deny
4. a `review_*_order` with identical params in the last 30 min → else deny
5. hard limits: market hours, 15:30 cutoff, fresh snapshot (live only), equity floor, daily/weekly loss,
   notional, buying power, position %, open-position count, trades/day, cooldown, universe, liquidity,
   earnings window, options rules (DTE, contracts, premium, exposure), exit qty ≤ held
6. mode: `approve_all` → human; `tiered` → auto if small, confident, known symbol, not an option (or a
   risk-reducing exit), else human; `autonomous` → allow
7. `dry_run` → deny with a simulated fill recorded; otherwise allow and mark the proposal executing

"Human" means a permission prompt in an interactive session, or a queued proposal (`tradeagent approve <id>`)
in a headless one.

## Scheduled runs

`scripts/run.sh <scan|manage|execute|review-day>` runs `claude -p "/<skill>"` headless with
`--permission-mode dontAsk --permission-prompts none` and `TRADEAGENT_HEADLESS=1`. `scripts/launchd/install.sh`
installs macOS launchd agents (08:30 scan, `/manage` every 30 min, `/execute` at 09:50 and 13:05, 14:30 intraday
scan, 16:15 review). See `scripts/launchd/README.md`. Before relying on it, verify once by hand that a headless
run in `approve_all` mode queues rather than executes: `TRADEAGENT_HEADLESS=1 scripts/run.sh status`.

## Going live (suggested ladder)

1. `dry_run: true`, `mode: approve_all` for a few sessions. Check `data/samples/`, `tradeagent decisions`,
   and that positions/equity parse. Fix `fieldmap.py` aliases if not.
2. `uv run tradeagent dry-run off` with tiny caps (`max_order_notional_usd: 100`) and one manually approved
   1-share order. Confirm the broker order id shows up in `tradeagent orders` and `get_equity_orders`.
3. Raise caps gradually; switch to `tiered` once `tradeagent stats` shows positive expectancy over a
   meaningful sample. `autonomous` only with limits you would accept losing in one bad day.

## Layout

```
CLAUDE.md                 mandate, hard rules, analysis/execution standard
.mcp.json                 robinhood-trading HTTP MCP server
.claude/settings.json     permissions (read tools allowed; place_* only via the gate) + hooks
.claude/hooks/            hook wrapper → python -m tradeagent.hooks <event>
.claude/agents/           technical-analyst, fundamental-analyst, catalyst-researcher, options-strategist, red-team
.claude/skills/           status, mode, kill, pending, approve, reject, scan, analyze, propose, execute, manage, review-day
config/levers.yaml        every lever
tradeagent/               config, models, db, fieldmap (parsers), snapshot, sizing, limits, pnl, journal, gate, hooks, report, stats, cli
scripts/run.sh            headless runner; scripts/launchd/ scheduling
data/                     tradeagent.db, overrides.json, lessons.md, samples/, reviews/, runs/, hooks.log
proposals/                proposal JSON files written by /analyze and /manage
tests/                    pytest (gate per mode, limits, sizing, parsers, market clock, hook subprocess)
```

## Known constraints

- Robinhood's MCP: long single-leg options only, no margin, no shorting, no paper environment, undocumented
  response formats, no streaming quotes (intraday means polling every ~30 minutes, not HFT). Crypto is
  disabled here. New York and some other states cannot trade crypto anyway.
- Claude Code hooks in `.claude/settings.json` only run after workspace trust has been accepted once in an
  interactive session in this folder; `claude -p` runs use your subscription quota.
- You are liable for every order the agent places, in every mode.
