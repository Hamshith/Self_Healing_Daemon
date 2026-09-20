"""FastAPI read API for the self-healing daemon dashboard."""

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import db

app = FastAPI(title="Self-Healing Daemon API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

HEARTBEAT_TIMEOUT_SECONDS = 90


def _json_safe(row):
    if row is None:
        return None
    row["safe_to_auto_remediate"] = bool(row.get("safe_to_auto_remediate"))
    return row


@app.on_event("startup")
def startup():
    db.init_db()


@app.get("/incidents")
def incidents(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return [_json_safe(row) for row in db.get_all_incidents(limit=limit, offset=offset)]


@app.get("/incidents/{incident_id}")
def incident(incident_id: int):
    row = db.get_incident_by_id(incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return _json_safe(row)


@app.get("/health")
def health():
    heartbeat = db.get_heartbeat()
    if heartbeat is None:
        return {
            "status": "stale",
            "pods_monitored": None,
            "last_check_timestamp": None,
        }

    checked_at = datetime.fromisoformat(
        heartbeat["last_check_at"].replace("Z", "+00:00")
    )
    age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
    return {
        "status": "ok" if age_seconds < HEARTBEAT_TIMEOUT_SECONDS else "stale",
        "pods_monitored": heartbeat["pods_monitored"],
        "last_check_timestamp": heartbeat["last_check_at"],
    }


@app.get("/metrics")
def metrics():
    return db.get_metrics()
