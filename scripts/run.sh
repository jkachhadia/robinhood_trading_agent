#!/bin/bash
# Headless runner for scheduled sessions.
#   scripts/run.sh scan            # morning scan
#   scripts/run.sh manage          # position management
#   scripts/run.sh execute         # place approved / auto-eligible proposals
#   scripts/run.sh review-day      # after the close
#   scripts/run.sh status          # smoke test
#
# Anything needing approval is queued (the gate denies with "queued for human approval").
# Exits early when the market is closed unless FORCE=1.
set -euo pipefail

SKILL="${1:?usage: run.sh <scan|manage|execute|review-day|status> [extra prompt]}"
shift || true
EXTRA="${*:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export TRADEAGENT_HOME="$ROOT"
export TRADEAGENT_HEADLESS=1
export TRADEAGENT_RUN_KIND="$SKILL"
ulimit -n 65536 2>/dev/null || true   # Claude Code needs more than the macOS default of 256 open files

LOGDIR="$ROOT/data/runs"
mkdir -p "$LOGDIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOGDIR/$STAMP-$SKILL.json"

# Market-hours guard (review-day and status run any time; scan runs pre-market too).
if [ "${FORCE:-0}" != "1" ]; then
  case "$SKILL" in
    manage|execute)
      if ! "$ROOT/.venv/bin/python" - <<'PY'
from tradeagent import marketclock as mc
from tradeagent.config import load_levers
import sys
lv = load_levers()
ok, why = mc.is_open(allowed_hours=lv.session.allowed_hours.value)
print(why)
sys.exit(0 if ok else 1)
PY
      then echo "market closed; skipping $SKILL"; exit 0; fi
      ;;
    scan)
      if ! "$ROOT/.venv/bin/python" - <<'PY'
from tradeagent import marketclock as mc
import sys
sys.exit(0 if mc.is_trading_day(mc.trading_date()) else 1)
PY
      then echo "not a trading day; skipping scan"; exit 0; fi
      ;;
  esac
fi

PROMPT="/$SKILL $EXTRA"
echo "[$STAMP] running: $PROMPT" | tee -a "$LOGDIR/runs.log"

# --permission-mode dontAsk: nothing prompts; the PreToolUse gate's explicit allow is what lets place_* through.
# --permission-prompts none: tell Claude nobody can answer, don't retry.
set +e
claude -p "$PROMPT" \
  --permission-mode dontAsk \
  --permission-prompts none \
  --output-format json \
  --max-turns "${MAX_TURNS:-80}" \
  > "$LOG" 2>"$LOG.err"
RC=$?
set -e

if command -v jq >/dev/null 2>&1; then
  jq -r '.result // empty' "$LOG" | tail -40
  echo "cost_usd=$(jq -r '.total_cost_usd // "n/a"' "$LOG") turns=$(jq -r '.num_turns // "n/a"' "$LOG") rc=$RC"
else
  tail -c 2000 "$LOG"; echo
fi
echo "[$STAMP] done rc=$RC log=$LOG" | tee -a "$LOGDIR/runs.log"
exit $RC
