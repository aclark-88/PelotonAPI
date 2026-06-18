"""Layer 3 tool: helpers for the WAT v2 memory database (db/memory.db).

Thin, single-purpose accessors over the three tables. Used by workflows to
persist entities, attach observations, transition lifecycle status, and log the
execution trace required by the self-healing loop.

Every public function returns the shared JSON envelope. A small CLI is provided
for quick inspection / manual logging.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from _shared import DB_PATH, fatal, ok, run_cli, skip

VALID_STATUS = {"RAW", "QUALIFIED", "OUTREACH_READY", "REJECTED"}
VALID_EXEC_STATUS = {"success", "retry", "skip", "fatal"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _require_db() -> dict[str, Any] | None:
    if not DB_PATH.exists():
        return fatal(
            f"memory.db not found at {DB_PATH}; run tools/init_memory_db.py first"
        )
    return None


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------
def upsert_entity(
    crd: str,
    cik: str,
    firm_name: str,
    strategies: str,
    calculated_aum: float = 0.0,
    status: str = "RAW",
) -> dict[str, Any]:
    """Insert a new entity or update the mutable fields of an existing one.

    ``crd`` is the natural key. Returns the resulting entity id.
    """
    if err := _require_db():
        return err
    if status not in VALID_STATUS:
        return skip(f"invalid status '{status}'")

    try:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO entities (crd, cik, firm_name, strategies, calculated_aum, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(crd) DO UPDATE SET
                    cik            = excluded.cik,
                    firm_name      = excluded.firm_name,
                    strategies     = excluded.strategies,
                    calculated_aum = excluded.calculated_aum,
                    status         = excluded.status
                """,
                (crd, cik, firm_name, strategies, calculated_aum, status),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM entities WHERE crd = ?", (crd,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fatal(f"sqlite error in upsert_entity: {exc}")
    return ok({"entity_id": row["id"], "crd": crd})


def set_status(crd: str, status: str) -> dict[str, Any]:
    if err := _require_db():
        return err
    if status not in VALID_STATUS:
        return skip(f"invalid status '{status}'")
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE entities SET status = ? WHERE crd = ?", (status, crd)
            )
            conn.commit()
            changed = cur.rowcount
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fatal(f"sqlite error in set_status: {exc}")
    if changed == 0:
        return skip(f"no entity with crd '{crd}'")
    return ok({"crd": crd, "status": status})


def get_entity_by_crd(crd: str) -> dict[str, Any]:
    if err := _require_db():
        return err
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM entities WHERE crd = ?", (crd,)
            ).fetchone()
            obs = []
            if row:
                obs = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM observations WHERE entity_id = ? "
                        "ORDER BY observed_at",
                        (row["id"],),
                    ).fetchall()
                ]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fatal(f"sqlite error in get_entity_by_crd: {exc}")
    if not row:
        return skip(f"no entity with crd '{crd}'")
    return ok({"entity": dict(row), "observations": obs})


def list_by_status(status: str) -> dict[str, Any]:
    if err := _require_db():
        return err
    if status not in VALID_STATUS:
        return skip(f"invalid status '{status}'")
    try:
        conn = _connect()
        try:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM entities WHERE status = ? ORDER BY calculated_aum DESC",
                    (status,),
                ).fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fatal(f"sqlite error in list_by_status: {exc}")
    return ok({"status": status, "entities": rows, "count": len(rows)})


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------
def add_observation(
    entity_id: int, key_fact: str, value: str, category: str
) -> dict[str, Any]:
    if err := _require_db():
        return err
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO observations (entity_id, key_fact, value, category) "
                "VALUES (?, ?, ?, ?)",
                (entity_id, key_fact, value, category),
            )
            conn.commit()
            obs_id = cur.lastrowid
        finally:
            conn.close()
    except sqlite3.IntegrityError as exc:
        return skip(f"foreign key / integrity error (entity_id={entity_id}): {exc}")
    except sqlite3.Error as exc:
        return fatal(f"sqlite error in add_observation: {exc}")
    return ok({"observation_id": obs_id, "entity_id": entity_id})


# ---------------------------------------------------------------------------
# execution_history (self-healing trace)
# ---------------------------------------------------------------------------
def log_execution(
    workflow_name: str, task_step: str, status: str, details: str
) -> dict[str, Any]:
    if err := _require_db():
        return err
    if status not in VALID_EXEC_STATUS:
        return skip(f"invalid execution status '{status}'")
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO execution_history (workflow_name, task_step, status, details) "
                "VALUES (?, ?, ?, ?)",
                (workflow_name, task_step, status, details),
            )
            conn.commit()
            log_id = cur.lastrowid
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fatal(f"sqlite error in log_execution: {exc}")
    return ok({"log_id": log_id})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="memory.db client")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upsert-entity")
    up.add_argument("--crd", required=True)
    up.add_argument("--cik", required=True)
    up.add_argument("--firm-name", required=True)
    up.add_argument("--strategies", required=True)
    up.add_argument("--aum", type=float, default=0.0)
    up.add_argument("--status", default="RAW")

    st = sub.add_parser("set-status")
    st.add_argument("--crd", required=True)
    st.add_argument("--status", required=True)

    ob = sub.add_parser("add-observation")
    ob.add_argument("--entity-id", type=int, required=True)
    ob.add_argument("--key-fact", required=True)
    ob.add_argument("--value", required=True)
    ob.add_argument("--category", required=True)

    lg = sub.add_parser("log")
    lg.add_argument("--workflow", required=True)
    lg.add_argument("--step", required=True)
    lg.add_argument("--status", required=True)
    lg.add_argument("--details", required=True)

    ge = sub.add_parser("get")
    ge.add_argument("--crd", required=True)

    ls = sub.add_parser("list")
    ls.add_argument("--status", required=True)

    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    if args.cmd == "upsert-entity":
        return upsert_entity(
            args.crd, args.cik, args.firm_name, args.strategies, args.aum, args.status
        )
    if args.cmd == "set-status":
        return set_status(args.crd, args.status)
    if args.cmd == "add-observation":
        return add_observation(args.entity_id, args.key_fact, args.value, args.category)
    if args.cmd == "log":
        return log_execution(args.workflow, args.step, args.status, args.details)
    if args.cmd == "get":
        return get_entity_by_crd(args.crd)
    if args.cmd == "list":
        return list_by_status(args.status)
    return fatal(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    run_cli(main())
