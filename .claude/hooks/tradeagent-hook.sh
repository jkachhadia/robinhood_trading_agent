#!/bin/bash
# Claude Code hook entrypoint. Usage: tradeagent-hook.sh <event>
# Runs the project's own venv so it works even when `uv` is not on PATH (launchd, cron).
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
export TRADEAGENT_HOME="$ROOT"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  if command -v uv >/dev/null 2>&1; then
    exec uv run --project "$ROOT" python -m tradeagent.hooks "$@"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    exec "$HOME/.local/bin/uv" run --project "$ROOT" python -m tradeagent.hooks "$@"
  else
    echo "tradeagent: no .venv and no uv found; run 'uv sync' in $ROOT" >&2
    # Fail closed for order tools, quiet for everything else.
    if [ "${1:-}" = "pre-tool-use" ]; then exit 2; fi
    exit 0
  fi
fi
exec "$PY" -m tradeagent.hooks "$@"
