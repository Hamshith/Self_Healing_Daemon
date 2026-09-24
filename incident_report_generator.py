"""Generate Markdown post-mortems from daemon incident records."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import config

REPORTS_DIR = Path(getattr(config, "REPORTS_DIR", "./reports"))


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value or "unknown")


def _list(items) -> str:
    values = items if isinstance(items, list) else [items]
    return "\n".join(f"- {value}" for value in values if value) or "- None recorded"


def generate_report(incident: dict, diagnosis: dict, remediation: dict | None = None) -> str:
    """Write and return a Markdown post-mortem path."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    incident_data = incident.get("incident_data", incident)
    diagnosis_data = incident.get("diagnosis", diagnosis)
    action = remediation or incident.get("remediation") or {}
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    pod_name = _safe_name(str(incident_data.get("pod_name", "unknown")))
    path = REPORTS_DIR / f"incident_{pod_name}_{timestamp}.md"
    report = f"""# Incident Post-Mortem

## Summary

- **Incident:** {incident_data.get('pod_name', 'unknown')}
- **Namespace:** {incident_data.get('namespace', 'unknown')}
- **Fault type:** {incident_data.get('fault_type', 'unknown')}
- **Detected at:** {incident_data.get('detected_at', 'unknown')}
- **Report generated:** {datetime.utcnow().isoformat()}Z

## Timeline

- **Detection:** {incident_data.get('detected_at', 'unknown')}
- **Remediation status:** {action.get('status', 'not attempted')}
- **Remediation completed:** {action.get('completed_at', 'not recorded')}

## Diagnosis

**Root cause:** {diagnosis_data.get('root_cause', 'unknown')}

**Diagnosed error:** {diagnosis_data.get('error', diagnosis_data.get('root_cause', 'unknown'))}

**Category:** {diagnosis_data.get('root_cause_category', 'unknown')}  
**Confidence:** {diagnosis_data.get('confidence', 'unknown')}  
**Severity:** {diagnosis_data.get('severity', 'unknown')}

{diagnosis_data.get('explanation', 'No explanation recorded.')}

### Evidence

{_list(diagnosis_data.get('evidence', []))}

## Action and Outcome

- **Recommended action:** {diagnosis_data.get('recommended_action', 'none recorded')}
- **Auto-remediation allowed:** {diagnosis_data.get('safe_to_auto_remediate', False)}
- **Action taken:** {action.get('action', 'none')}
- **Outcome:** {action.get('message', action.get('status', 'not attempted'))}

### LLM Remediation Steps

{_list([
    f"Step {step.get('step', index)}: {step.get('action', 'unknown')} - {step.get('reason', '')}"
    for index, step in enumerate(diagnosis_data.get('remediation_steps', []), start=1)
])}

### Step Results

{_list([
    f"Step {step.get('step', index)}: {step.get('status', 'unknown')} - {step.get('reason', '')}"
    for index, step in enumerate(action.get('steps', []), start=1)
])}

## Prevention Recommendations

{_list(diagnosis_data.get('prevention_recommendations', [
    'Add a regression test or alert for this failure mode.',
    'Document the validated remediation in the matching runbook.',
]))}

## Raw Incident Data

```json
{json.dumps(incident_data, indent=2, default=str)}
```
"""
    path.write_text(report, encoding="utf-8")
    return str(path)
