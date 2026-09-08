# Scheduled runs (macOS launchd)

Times are US/Eastern via `TZ` in each plist. Install with:

```bash
scripts/launchd/install.sh          # copies plists to ~/Library/LaunchAgents and loads them
scripts/launchd/install.sh remove   # unloads and removes them
```

| Label | When (ET) | Runs |
|---|---|---|
| com.tradeagent.scan | 08:30 Mon–Fri | `/scan` |
| com.tradeagent.manage | every 30 min 09:45–15:45 Mon–Fri | `/manage` (exits early if market closed) |
| com.tradeagent.execute | 09:50 and 13:05 Mon–Fri | `/execute` |
| com.tradeagent.scan-intraday | 14:30 Mon–Fri | `/scan intraday` |
| com.tradeagent.review | 16:15 Mon–Fri | `/review-day` |

Logs: `data/runs/*.json` (full Claude Code JSON result) and `data/runs/runs.log`.

Prerequisites: `uv sync` done, Claude Code logged in (subscription), workspace trust accepted once in an
interactive session in this folder, and the Robinhood MCP authenticated (`/mcp`). Scheduled runs consume
your Claude subscription usage.
