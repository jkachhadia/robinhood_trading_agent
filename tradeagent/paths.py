"""Locate the project root, config file, and data directory.

Resolution order for the project root:
1. TRADEAGENT_HOME
2. CLAUDE_PROJECT_DIR (set by Claude Code for hooks and skills)
3. walk up from cwd until a directory containing config/levers.yaml is found
4. cwd
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    for var in ("TRADEAGENT_HOME", "CLAUDE_PROJECT_DIR"):
        v = os.environ.get(var)
        if v:
            return Path(v).expanduser().resolve()
    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "config" / "levers.yaml").exists():
            return candidate
    return cur


def config_path() -> Path:
    v = os.environ.get("TRADEAGENT_CONFIG")
    if v:
        return Path(v).expanduser().resolve()
    return project_root() / "config" / "levers.yaml"


def data_dir() -> Path:
    v = os.environ.get("TRADEAGENT_DATA")
    d = Path(v).expanduser().resolve() if v else project_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "tradeagent.db"


def overrides_path() -> Path:
    return data_dir() / "overrides.json"


def lessons_path() -> Path:
    return data_dir() / "lessons.md"


def samples_dir() -> Path:
    d = data_dir() / "samples"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return data_dir() / "hooks.log"
