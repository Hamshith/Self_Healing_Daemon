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
    remediation_status         TEXT DEFAULT 'none',
    remediation_started_at     TEXT,
    remediation_completed_at   TEXT,
    fault_injected_at          TEXT,
    raw_incident_json       TEXT,
    raw_diagnosis_json      TEXT,
    saved_at                TEXT
);

CREATE INDEX IF NOT EXISTS idx_incidents_fault_type ON incidents(fault_type);
CREATE INDEX IF NOT EXISTS idx_incidents_detected_at ON incidents(detected_at);

-- Full status-transition history. Without this, update_remediation_status()
-- overwrites the current status in place and every prior transition (and
-- its timestamp) is lost — which is exactly the data MTTR needs.
CREATE TABLE IF NOT EXISTS incident_status_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     INTEGER NOT NULL,
    status          TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_status_history_incident ON incident_status_history(incident_id);
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
    status is a free-form string, e.g. "in_progress", "remediated",
    "escalated", "rolled_back", "failed".

    Unlike a plain overwrite, this also appends to
    incident_status_history, so every transition — and when it
    happened — is preserved. That history is what MTTR is computed
    from: "in_progress" (or the first non-"none" status) marks
    remediation start, and a terminal status ("remediated",
    "escalated", "rolled_back", "failed") marks remediation end.
    """
    now = datetime.utcnow().isoformat() + "Z"
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE incidents SET remediation_status = ? WHERE id = ?",
            (status, incident_id),
        )

        terminal_statuses = {"remediated", "escalated", "rolled_back", "failed"}
        if status not in ("none",):
            # Set remediation_started_at once, on the first real
            # status change away from "none".
            conn.execute(
                """
                UPDATE incidents
                SET remediation_started_at = COALESCE(remediation_started_at, ?)
                WHERE id = ?
                """,
                (now, incident_id),
            )
        if status in terminal_statuses:
            conn.execute(
                "UPDATE incidents SET remediation_completed_at = ? WHERE id = ?",
                (now, incident_id),
            )

        conn.execute(
            "INSERT INTO incident_status_history (incident_id, status, changed_at) "
            "VALUES (?, ?, ?)",
            (incident_id, status, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_incidents(limit: int = 100, offset: int = 0,
                       fault_type: str = None, remediation_status: str = None) -> list:
    """Return incidents newest-first, optionally filtered and paginated."""
    conn = _get_conn()
    try:
        where_clauses = []
        params = []
        if fault_type:
            where_clauses.append("fault_type = ?")
            params.append(fault_type)
        if remediation_status:
            where_clauses.append("remediation_status = ?")
            params.append(remediation_status)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.extend([limit, offset])

        rows = conn.execute(
            f"SELECT * FROM incidents {where_sql} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
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


def set_fault_injected_at(incident_id: int, injected_at: str):
    """
    For your eval script to call after matching a fault-injection run
    to the incident it produced. MTTD needs a T0 (fault injection
    time), which the daemon has no way to know on its own — it only
    ever observes T1 (detected_at). Once your eval script sets this,
    MTTD can be computed directly from the DB instead of by hand.
    """
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE incidents SET fault_injected_at = ? WHERE id = ?",
            (injected_at, incident_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_mttd_mttr_stats() -> dict:
    """
    MTTD (mean time to detect) = detected_at - fault_injected_at,
    averaged over incidents where fault_injected_at has been set
    (i.e. eval runs that called set_fault_injected_at()).

    MTTR (mean time to remediate/escalate) = remediation_completed_at
    - detected_at, averaged over incidents that reached a terminal
    remediation status.

    Both return None if there's no qualifying data yet, rather than
    a misleading 0.
    """
    conn = _get_conn()
    try:
        mttd_rows = conn.execute(
            "SELECT detected_at, fault_injected_at FROM incidents "
            "WHERE fault_injected_at IS NOT NULL"
        ).fetchall()
        mttd_seconds = [
            (datetime.fromisoformat(r["detected_at"].rstrip("Z"))
             - datetime.fromisoformat(r["fault_injected_at"].rstrip("Z"))).total_seconds()
            for r in mttd_rows
        ]

        mttr_rows = conn.execute(
            "SELECT detected_at, remediation_completed_at FROM incidents "
            "WHERE remediation_completed_at IS NOT NULL"
        ).fetchall()
        mttr_seconds = [
            (datetime.fromisoformat(r["remediation_completed_at"].rstrip("Z"))
             - datetime.fromisoformat(r["detected_at"].rstrip("Z"))).total_seconds()
            for r in mttr_rows
        ]

        return {
            "mttd_seconds_avg": sum(mttd_seconds) / len(mttd_seconds) if mttd_seconds else None,
            "mttd_sample_size": len(mttd_seconds),
            "mttr_seconds_avg": sum(mttr_seconds) / len(mttr_seconds) if mttr_seconds else None,
            "mttr_sample_size": len(mttr_seconds),
        }
    finally:
        conn.close()


def get_incidents_over_time(bucket: str = "day") -> list:
    """
    Incident counts bucketed by day (default) or hour, for the
    dashboard's metrics panel. Returns [{"bucket": "2026-09-18", "count": 3}, ...]
    """
    fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m-%d %H:00"
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT strftime('{fmt}', detected_at) AS bucket, COUNT(*) AS count "
            "FROM incidents GROUP BY bucket ORDER BY bucket"
        ).fetchall()
        return [dict(row) for row in rows]
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
            **get_mttd_mttr_stats(),
            "incidents_over_time": get_incidents_over_time(),
        }
    finally:
        conn.close()