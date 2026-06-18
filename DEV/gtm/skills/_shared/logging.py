"""Structured logging: JSON to stdout + logs/<date>/<skill_name>.jsonl.

Every log line carries run_id and skill_name (bound at run start).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

import structlog

LOGS_ROOT = Path(__file__).resolve().parents[3] / "logs"

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _configured = True


class _JsonlTee:
    """Mirrors every event dict to a per-skill JSONL file."""

    def __init__(self, skill_name: str) -> None:
        day_dir = LOGS_ROOT / date.today().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        self.path = day_dir / f"{skill_name}.jsonl"

    def write(self, event: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")


def get_skill_logger(skill_name: str, run_id: str):
    """A structlog logger bound with skill_name + run_id, teed to a JSONL file."""
    _configure()
    tee = _JsonlTee(skill_name)

    def _tee_processor(logger, method_name, event_dict):
        tee.write(dict(event_dict))
        return event_dict

    log = structlog.wrap_logger(
        structlog.PrintLoggerFactory(file=sys.stdout)(),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _tee_processor,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    return log.bind(skill_name=skill_name, run_id=str(run_id))
