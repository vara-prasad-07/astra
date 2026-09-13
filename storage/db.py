"""SQLite persistence.

Holds the incident lifecycle described in the design record plus the full graph
state, which is what lets the human-approval gate span a process boundary: the
investigation can finish, the server can restart, and the approval that arrives
ten minutes later still resumes against the exact evidence that was analysed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    severity TEXT,
    status TEXT NOT NULL,
    title TEXT,
    started_at TEXT,
    updated_at TEXT,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    agent TEXT, codename TEXT, source TEXT, kind TEXT,
    summary TEXT, detail_json TEXT, confidence REAL, collected_at TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    name TEXT, description TEXT, score REAL, signals_json TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    decision TEXT, approver TEXT, source TEXT, at TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    action_type TEXT, target TEXT, approver TEXT,
    executed INTEGER, health_restored INTEGER,
    executed_at TEXT, result_json TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence(incident_id);
CREATE INDEX IF NOT EXISTS idx_hypotheses_incident ON hypotheses(incident_id);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_state(state: dict[str, Any]) -> None:
    """Upsert the incident and replace its derived rows with the current state."""
    incident_id = state.get("incident_id")
    if not incident_id:
        return
    incident = state.get("incident") or {}

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO incidents (id, service, severity, status, title, started_at,
                                   updated_at, state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                service=excluded.service, severity=excluded.severity,
                status=excluded.status, title=excluded.title,
                started_at=excluded.started_at, updated_at=excluded.updated_at,
                state_json=excluded.state_json
            """,
            (
                incident_id,
                incident.get("service", "unknown"),
                incident.get("severity", ""),
                state.get("status", incident.get("status", "triggered")),
                incident.get("title", ""),
                incident.get("started_at", ""),
                _now(),
                json.dumps(state, default=str),
            ),
        )

        conn.execute("DELETE FROM evidence WHERE incident_id = ?", (incident_id,))
        conn.executemany(
            """INSERT INTO evidence (incident_id, agent, codename, source, kind,
                                     summary, detail_json, confidence, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    incident_id,
                    item.get("agent", ""),
                    item.get("codename", ""),
                    item.get("source", ""),
                    item.get("kind", ""),
                    item.get("summary", ""),
                    json.dumps(item.get("detail", {}), default=str),
                    item.get("confidence", 1.0),
                    item.get("collected_at", ""),
                )
                for item in state.get("evidence") or []
            ],
        )

        conn.execute("DELETE FROM hypotheses WHERE incident_id = ?", (incident_id,))
        conn.executemany(
            """INSERT INTO hypotheses (incident_id, name, description, score, signals_json)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    incident_id,
                    item.get("name", ""),
                    item.get("description", ""),
                    item.get("score", 0.0),
                    json.dumps(item.get("signals", []), default=str),
                )
                for item in state.get("hypotheses") or []
            ],
        )

        action = state.get("action_result")
        if action:
            conn.execute("DELETE FROM actions WHERE incident_id = ?", (incident_id,))
            conn.execute(
                """INSERT INTO actions (incident_id, action_type, target, approver,
                                        executed, health_restored, executed_at, result_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    incident_id,
                    action.get("action_type", ""),
                    action.get("target", ""),
                    action.get("approver", ""),
                    int(bool(action.get("executed"))),
                    int(bool(action.get("health_restored"))),
                    action.get("executed_at") or "",
                    json.dumps(action, default=str),
                ),
            )


def record_approval(incident_id: str, decision: str, approver: str, source: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO approvals (incident_id, decision, approver, source, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (incident_id, decision, approver, source, _now()),
        )


def load_state(incident_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT state_json FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    return json.loads(row["state_json"]) if row else None


def list_incidents(limit: int = 25) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, service, severity, status, title, started_at, updated_at
               FROM incidents ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def approvals_for(incident_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT decision, approver, source, at FROM approvals "
            "WHERE incident_id = ? ORDER BY at",
            (incident_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def reset() -> None:
    with connect() as conn:
        for table in ("actions", "approvals", "hypotheses", "evidence", "incidents"):
            conn.execute(f"DELETE FROM {table}")
