"""Shared primitives for WAT v2 Layer 3 tools.

Every tool in this package returns a uniform result envelope:

    {"status": "success" | "retry" | "skip" | "fatal", "data": <any>, "error": <str|null>}

Status contract
---------------
- success : the operation completed; ``data`` holds the result.
- retry   : a transient condition (rate limit, 5xx, timeout). The caller MAY
            re-invoke after the indicated backoff. ``data`` MAY carry hints
            (e.g. ``retry_after`` seconds).
- skip    : a non-fatal, item-local failure (missing/unparseable filing,
            404). The caller SHOULD log and move to the next item.
- fatal   : an unrecoverable condition (bad credentials, programmer error,
            corrupt input). The caller MUST halt the affected workflow step.

Path resolution is centralized here so every tool agrees on where the project
root, database, drafts, and downloaded filings live regardless of the current
working directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths — resolved relative to this file so tools work from any CWD.
# tools/_shared.py -> parents[1] == project root (the DEV workspace).
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
TOOLS_DIR: Path = PROJECT_ROOT / "tools"
WORKFLOWS_DIR: Path = PROJECT_ROOT / "workflows"
DB_DIR: Path = PROJECT_ROOT / "db"
DB_PATH: Path = DB_DIR / "memory.db"
DRAFTS_DIR: Path = PROJECT_ROOT / "drafts"
DATA_DIR: Path = PROJECT_ROOT / "data"
FILINGS_DIR: Path = DATA_DIR / "filings"
BRIEFS_DIR: Path = PROJECT_ROOT / "briefs"
CONFIG_DIR: Path = PROJECT_ROOT / "config"

# Valid envelope statuses.
SUCCESS = "success"
RETRY = "retry"
SKIP = "skip"
FATAL = "fatal"


# ---------------------------------------------------------------------------
# Result envelope helpers
# ---------------------------------------------------------------------------
def _envelope(status: str, data: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "data": data, "error": error}


def ok(data: Any = None) -> dict[str, Any]:
    return _envelope(SUCCESS, data=data, error=None)


def retry(error: str, data: Any = None) -> dict[str, Any]:
    return _envelope(RETRY, data=data, error=error)


def skip(error: str, data: Any = None) -> dict[str, Any]:
    return _envelope(SKIP, data=data, error=error)


def fatal(error: str, data: Any = None) -> dict[str, Any]:
    return _envelope(FATAL, data=data, error=error)


def is_ok(result: dict[str, Any]) -> bool:
    return result.get("status") == SUCCESS


# ---------------------------------------------------------------------------
# CLI emission
# ---------------------------------------------------------------------------
def emit(result: dict[str, Any]) -> int:
    """Print a result envelope as JSON and return a process exit code.

    Exit codes let shell-level orchestration distinguish a hard failure
    (``fatal`` -> 2) from soft outcomes (``success``/``skip`` -> 0) and
    transient ones (``retry`` -> 75, the BSD EX_TEMPFAIL convention).
    """
    print(json.dumps(result, default=str, indent=2))
    status = result.get("status")
    if status == FATAL:
        return 2
    if status == RETRY:
        return 75
    return 0


def run_cli(result: dict[str, Any]) -> None:
    sys.exit(emit(result))


def ensure_dirs() -> None:
    """Create the runtime directories if they do not yet exist."""
    for d in (DB_DIR, DRAFTS_DIR, DATA_DIR, FILINGS_DIR, BRIEFS_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
