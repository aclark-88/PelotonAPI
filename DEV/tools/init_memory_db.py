"""Initialize the WAT v2 cross-session memory database (db/memory.db).

Idempotent: safe to run repeatedly. Creates three tables — ``entities``,
``observations``, ``execution_history`` — plus supporting indexes, exactly as
specified by the WAT v2 architecture.

Usage:
    python tools/init_memory_db.py

Emits a JSON result envelope and exits 0 on success, 2 on a fatal error.
"""

from __future__ import annotations

import sqlite3

from _shared import DB_PATH, ensure_dirs, fatal, ok, run_cli

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS entities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    crd            TEXT UNIQUE NOT NULL,
    cik            TEXT NOT NULL,
    firm_name      TEXT NOT NULL,
    strategies     TEXT NOT NULL,
    calculated_aum REAL DEFAULT 0.0,
    status         TEXT CHECK (status IN ('RAW', 'QUALIFIED', 'OUTREACH_READY', 'REJECTED'))
                        DEFAULT 'RAW',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   INTEGER NOT NULL REFERENCES entities(id),
    key_fact    TEXT NOT NULL,
    value       TEXT NOT NULL,
    category    TEXT NOT NULL,
    observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS execution_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name TEXT NOT NULL,
    task_step     TEXT NOT NULL,
    status        TEXT CHECK (status IN ('success', 'retry', 'skip', 'fatal')),
    details       TEXT NOT NULL,
    timestamp     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_entities_status      ON entities(status);
CREATE INDEX IF NOT EXISTS idx_observations_entity  ON observations(entity_id);
CREATE INDEX IF NOT EXISTS idx_observations_category ON observations(category);
CREATE INDEX IF NOT EXISTS idx_exec_workflow        ON execution_history(workflow_name);
"""


def init_db() -> dict:
    ensure_dirs()
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fatal(f"sqlite error during init: {exc}")

    return ok({"db_path": str(DB_PATH), "tables": tables})


if __name__ == "__main__":
    run_cli(init_db())
