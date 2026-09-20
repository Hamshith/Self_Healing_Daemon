"""
Incident reporting module.
Pretty-prints diagnosis reports to the terminal with ANSI
colour coding and saves each incident as timestamped JSON.
"""

import json
import os
import re
from datetime import datetime
import config
import db

_SEVERITY_COLORS = {
    "critical": "\033[91m",
    "high": "\033[93m",
    "medium": "\033[94m",
    "low": "\033[92m",
}
_BOLD = "\033[1m"
_RESET = "\033[0m"
_CYAN = "\033[96m"
_WHITE = "\033[97m"


def _colorize(severity):
    color = _SEVERITY_COLORS.get(severity.lower(), _WHITE)
    return f"{color}{_BOLD}{severity.upper()}{_RESET}"


def _wrap(text, width=56):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def print_incident_report(incident_data, diagnosis):
    sev = diagnosis.get("severity", "unknown")
    border = "=" * 60
    thin = "-" * 60
    P = _CYAN + "║" + _RESET

    print(f"\n{_CYAN}╔{'═'*60}╗{_RESET}")
    print(f"{P}  {_BOLD}🔍  INCIDENT DIAGNOSIS REPORT{' '*29}{_CYAN}║{_RESET}")
    print(f"{_CYAN}╠{'═'*60}╣{_RESET}")

    for label, key in [("Timestamp","detected_at"),("Pod Name","pod_name"),
                        ("Namespace","namespace"),("Restart Count","restart_count")]:
        print(f"{P}  {_BOLD}{label:<18}{_RESET}{incident_data.get(key,'N/A')}")

    print(f"{_CYAN}╠{'═'*60}╣{_RESET}")
    print(f"{P}  {_BOLD}{'Root Cause':<18}{_RESET}{diagnosis.get('root_cause','N/A')}")
    print(f"{P}  {_BOLD}{'Category':<18}{_RESET}{diagnosis.get('root_cause_category','N/A')}")
    print(f"{P}  {_BOLD}{'Confidence':<18}{_RESET}{diagnosis.get('confidence','N/A')}")
    print(f"{P}  {_BOLD}{'Severity':<18}{_RESET}{_colorize(sev)}")

    print(f"{_CYAN}╟{'─'*60}╢{_RESET}")
    print(f"{P}  {_BOLD}Explanation:{_RESET}")
    for l in _wrap(diagnosis.get("explanation","N/A")):
        print(f"{P}    {l}")

    print(f"{_CYAN}╟{'─'*60}╢{_RESET}")
    print(f"{P}  {_BOLD}Evidence:{_RESET}")
    for e in diagnosis.get("evidence",[]):
        print(f"{P}    • {e}")

    print(f"{_CYAN}╟{'─'*60}╢{_RESET}")
    print(f"{P}  {_BOLD}Recommended Action:{_RESET}")
    for l in _wrap(diagnosis.get("recommended_action","N/A")):
        print(f"{P}    {l}")

    auto = diagnosis.get("safe_to_auto_remediate", False)
    print(f"{P}  {_BOLD}{'Auto-Remediate?':<18}{_RESET}{'Yes ✅' if auto else 'No ❌'}")
    print(f"{_CYAN}╚{'═'*60}╝{_RESET}\n")


def save_incident_report(incident_data, diagnosis):
    os.makedirs(config.INCIDENTS_DIR, exist_ok=True)
    pod = incident_data.get("pod_name","unknown")
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    safe_pod = re.sub(r"[^a-zA-Z0-9_\-]", "_", pod)
    filename = f"incident_{safe_pod}_{ts}.json"
    filepath = os.path.join(config.INCIDENTS_DIR, filename)

    report = {
        "incident_data": incident_data,
        "diagnosis": diagnosis,
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"[reporter] Incident saved → {filepath}")

    # Also write to the operational DB so the frontend/API can query it.
    # Kept as a separate try/except — a DB write failing should never
    # cost you the JSON audit file you already have on disk.
    try:
        row_id = db.save_incident(incident_data, diagnosis)
        print(f"[reporter] Incident saved to DB → id={row_id}")
        return row_id
    except Exception as exc:
        print(f"[reporter] Warning: failed to save incident to DB: {exc}")
        return None