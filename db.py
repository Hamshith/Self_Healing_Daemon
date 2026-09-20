"""
Database module for incident storage.
Uses SQLite — a single file, no server to run, good fit for a
single daemon process writing incidents. If you outgrow this
later (multiple daemons writing concurrently), swap this module
for a Postgres-backed version; the function signatures below are
written so callers (reporter.py, api.py) don't need to change.
"""

import json
import os
import sqlite3
from datetime import datetime
import config

DB_PATH = getattr(config, "DB_PATH", "./incidents.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_name                TEXT,
    namespace               TEXT,
    fault_type              TEXT,
    restart_count           INTEGER,
    detected_at             TEXT,
    root_cause              TEXT,
    root_cause_category     TEXT,
    severity                TEXT,
    confidence              TEXT,
    recommended_action      TEXT,
    safe_to_auto_remediate  INTEGER,
    remediation_status      TEXT DEFAULT 'none',
    raw_incident_json       TEXT,
    raw_diagnosis_json      TEXT,
    saved_at                TEXT
);

CREATE INDEX IF NOT EXISTS idx_incidents_fault_type ON incidents(fault_type);
CREATE INDEX IF NOT EXISTS idx_incidents_detected_at ON incidents(detected_at);
"""


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the incidents table if it doesn't already exist. Call once at startup."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)) or ".", exist_ok=True)
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def save_incident(incident_data: dict, diagnosis: dict) -> int:
    """
    Insert one incident + its diagnosis. Returns the new row's id.

    Stores the known columns for easy querying/filtering, plus the
    full raw dicts as JSON so nothing is lost even if the schema
    doesn't cover every field (e.g. NetworkLatency incidents have a
    different shape than pod-level ones — raw_incident_json still
    captures it).
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO incidents (
                pod_name, namespace, fault_type, restart_count, detected_at,
                root_cause, root_cause_category, severity, confidence,
                recommended_action, safe_to_auto_remediate,
                raw_incident_json, raw_diagnosis_json, saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident_data.get("pod_name"),
                incident_data.get("namespace"),
                incident_data.get("fault_type"),
                incident_data.get("restart_count"),
                incident_data.get("detected_at"),
                diagnosis.get("root_cause"),
                diagnosis.get("root_cause_category"),
                diagnosis.get("severity"),
                diagnosis.get("confidence"),
                diagnosis.get("recommended_action"),
                int(bool(diagnosis.get("safe_to_auto_remediate", False))),
                json.dumps(incident_data, default=str),
                json.dumps(diagnosis, default=str),
                datetime.utcnow().isoformat() + "Z",
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_remediation_status(incident_id: int, status: str):
    """
    For the remediator to call after it acts on an incident.
    status is a free-form string, e.g. "remediated", "escalated",
    "rolled_back", "failed".
    """
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE incidents SET remediation_status = ? WHERE id = ?",
            (status, incident_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_incidents(limit: int = 100, fault_type: str = None) -> list:
    """Return incidents newest-first, optionally filtered by fault_type."""
    conn = _get_conn()
    try:
        if fault_type:
            rows = conn.execute(
                "SELECT * FROM incidents WHERE fault_type = ? "
                "ORDER BY id DESC LIMIT ?",
                (fault_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_incident_by_id(incident_id: int) -> dict:
    """Return one incident by id, or None if it doesn't exist."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_metrics() -> dict:
    """
    Basic aggregates for the dashboard's metrics panel:
    total incidents, breakdown by fault_type, breakdown by severity,
    and how many were auto-remediated vs escalated.
    """
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM incidents").fetchone()["c"]

        by_fault_type = {
            r["fault_type"]: r["c"]
            for r in conn.execute(
                "SELECT fault_type, COUNT(*) AS c FROM incidents "
                "GROUP BY fault_type"
            ).fetchall()
        }

        by_severity = {
            r["severity"]: r["c"]
            for r in conn.execute(
                "SELECT severity, COUNT(*) AS c FROM incidents "
                "GROUP BY severity"
            ).fetchall()
        }

        by_remediation_status = {
            r["remediation_status"]: r["c"]
            for r in conn.execute(
                "SELECT remediation_status, COUNT(*) AS c FROM incidents "
                "GROUP BY remediation_status"
            ).fetchall()
        }

        return {
            "total_incidents": total,
            "by_fault_type": by_fault_type,
            "by_severity": by_severity,
            "by_remediation_status": by_remediation_status,
        }
    finally:
        conn.close()
