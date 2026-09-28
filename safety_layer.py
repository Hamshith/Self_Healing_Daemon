"""
safety_layer.py — Centralised, measurable safety gate for LLM remediation.

This module is the primary technical contribution of the paper.

The Five Safety Layers
──────────────────────
Every LLM diagnosis passes through five independent gates before any
Kubernetes mutation is allowed.  Each gate is independently measurable
(it emits a SafetyEvent when it fires) so we can report exactly how often
each layer intercepted a bad proposal in production and in adversarial probing.

  Layer 1 — safe_to_auto_remediate guard
      The LLM must explicitly return safe_to_auto_remediate=True. Any diagnosis
      that does not claim human safety vetoes all automatic action. This is the
      outermost gate and the easiest to audit: a single boolean in the JSON.

  Layer 2 — Action allow-list
      The recommended action string must be in ALLOWED_ACTIONS. Free-form
      strings such as "exec_command", "delete_deployment", "scale_replicas",
      "create_secret", or an arbitrary kubectl command are rejected without
      executing any Kubernetes API call. This is enforced by string equality,
      not pattern matching — the set is closed, not filtered.

  Layer 3 — Parameter schema validation
      Each allowed action has a declared parameter schema. Parameters from the
      LLM JSON are validated against the schema before the action handler runs.
      Concretely: increase_memory_limit requires increase_pct to be a float in
      (0, 1]. The validator rejects wrong types, out-of-range values, and extra
      undeclared parameters. Any violation is recorded as a SafetyEvent of type
      INVALID_PARAMETER and the action is replaced with escalate.

  Layer 4 — Pre-action snapshot
      Before any mutation is applied, the current Deployment or Pod state is
      serialised to rollbacks/<pod>_<action>_<timestamp>.json. This is not a
      safety gate (it does not block an action), but it is the prerequisite for
      Layer 5 — without the snapshot there is nothing to roll back to.

  Layer 5 — Post-action verification + automatic rollback
      After the Kubernetes API call returns, the daemon polls the affected pod
      for up to VERIFICATION_POLL_SECONDS. Only a pod that is Running+Ready for
      VERIFICATION_STABLE_POLLS consecutive checks is recorded as "remediated".
      A pod that never stabilises triggers rollback_action() with the Layer-4
      snapshot path and the incident is recorded as "rolled_back".

Measurement
───────────
Every gate that fires records a SafetyEvent to the database via
db.record_safety_event(). The eval/safety_probe.py module runs adversarial
incidents through the LLM+guardrail pipeline and reports:

  - unsafe_action_rate:    fraction of LLM responses proposing an unlisted action
  - invalid_param_rate:    fraction proposing valid action but invalid parameters
  - overclaiming_auto_safe: safe_to_auto_remediate=True for a dangerous incident
  - interception_rate:     fraction of unsafe proposals caught by Layers 1–3
  - false_negative_rate:   fraction that bypassed all gates (should be 0)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ── Layer 2: Action allow-list ────────────────────────────────────────────

ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "delete_pod",
    "increase_memory_limit",
    "escalate",
})


# ── Layer 3: Parameter schemas ────────────────────────────────────────────

class ParamType(Enum):
    FLOAT  = "float"
    INT    = "int"
    STRING = "string"
    BOOL   = "bool"


@dataclass
class ParamSpec:
    name: str
    type: ParamType
    required: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    # If not None, the value must be one of these strings.
    choices: Optional[list[str]] = None


# Schema per allow-listed action.  Extra keys not listed here are rejected.
ACTION_SCHEMAS: dict[str, list[ParamSpec]] = {
    "delete_pod": [],   # no parameters
    "increase_memory_limit": [
        ParamSpec(
            name="increase_pct",
            type=ParamType.FLOAT,
            required=False,      # defaults to 0.25 if absent
            min_value=0.0,       # exclusive: must be > 0
            max_value=1.0,       # inclusive: at most 100% increase
        ),
    ],
    "escalate": [
        ParamSpec(
            name="reason",
            type=ParamType.STRING,
            required=False,      # reason can be omitted; falls back to step.reason
        ),
    ],
}


# ── Safety event types ────────────────────────────────────────────────────

class SafetyEventType(str, Enum):
    # Layer 1
    UNSAFE_AUTO_REMEDIATE   = "unsafe_auto_remediate"   # safe_to_auto_remediate=False, actioned
    # Layer 2
    UNLISTED_ACTION         = "unlisted_action"          # action not in ALLOWED_ACTIONS
    # Layer 3
    INVALID_PARAMETER       = "invalid_parameter"        # wrong type / out of range
    EXTRA_PARAMETER         = "extra_parameter"          # parameter not in schema
    # Layer 4 (informational — not a veto)
    SNAPSHOT_WRITTEN        = "snapshot_written"
    SNAPSHOT_FAILED         = "snapshot_failed"          # snapshot write failed; action blocked
    # Layer 5
    VERIFICATION_PASSED     = "verification_passed"
    VERIFICATION_FAILED     = "verification_failed"
    ROLLBACK_TRIGGERED      = "rollback_triggered"
    ROLLBACK_SUCCEEDED      = "rollback_succeeded"
    ROLLBACK_FAILED         = "rollback_failed"


@dataclass
class SafetyEvent:
    """
    One safety-layer interception event.

    Written to the database via db.record_safety_event() so the paper can
    report exactly how often each layer fired and what it intercepted.
    """
    event_type: SafetyEventType
    incident_id: Optional[int]          # None during eval/probe runs
    pod_name: str
    namespace: str
    proposed_action: str                # what the LLM sent
    veto_reason: str                    # human-readable explanation of why it was blocked
    proposed_parameters: dict = field(default_factory=dict)
    layer: int = 0                      # 1–5 matching the layer descriptions above
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "event_type":          self.event_type.value,
            "incident_id":         self.incident_id,
            "pod_name":            self.pod_name,
            "namespace":           self.namespace,
            "proposed_action":     self.proposed_action,
            "veto_reason":         self.veto_reason,
            "proposed_parameters": json.dumps(self.proposed_parameters, default=str),
            "layer":               self.layer,
            "recorded_at":         self.recorded_at,
        }


# ── Validation helpers ────────────────────────────────────────────────────

def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_parameters(action: str, parameters: dict,
                          pod_name: str, namespace: str,
                          incident_id: Optional[int]) -> list[SafetyEvent]:
    """
    Validate *parameters* against the schema for *action*.

    Returns a list of SafetyEvents for every violation found.
    An empty list means the parameters are valid.
    """
    events: list[SafetyEvent] = []
    schema = ACTION_SCHEMAS.get(action, [])
    declared_names = {spec.name for spec in schema}

    # Extra (undeclared) parameters — potential prompt-injection attempting to
    # sneak in a command or override a system value.
    for key in parameters:
        if key not in declared_names:
            events.append(SafetyEvent(
                event_type=SafetyEventType.EXTRA_PARAMETER,
                incident_id=incident_id,
                pod_name=pod_name,
                namespace=namespace,
                proposed_action=action,
                veto_reason=(
                    f"Parameter '{key}' is not declared in the schema for "
                    f"action '{action}' and was rejected."
                ),
                proposed_parameters=parameters,
                layer=3,
            ))

    for spec in schema:
        raw = parameters.get(spec.name)

        # Required parameter missing
        if spec.required and raw is None:
            events.append(SafetyEvent(
                event_type=SafetyEventType.INVALID_PARAMETER,
                incident_id=incident_id,
                pod_name=pod_name,
                namespace=namespace,
                proposed_action=action,
                veto_reason=f"Required parameter '{spec.name}' is missing.",
                proposed_parameters=parameters,
                layer=3,
            ))
            continue

        if raw is None:
            continue  # optional, absent — OK

        # Type check
        if spec.type == ParamType.FLOAT:
            coerced = _coerce_float(raw)
            if coerced is None:
                events.append(SafetyEvent(
                    event_type=SafetyEventType.INVALID_PARAMETER,
                    incident_id=incident_id,
                    pod_name=pod_name,
                    namespace=namespace,
                    proposed_action=action,
                    veto_reason=(
                        f"Parameter '{spec.name}' must be a number; "
                        f"received {type(raw).__name__!r} value {raw!r}."
                    ),
                    proposed_parameters=parameters,
                    layer=3,
                ))
                continue
            # Range check (min exclusive, max inclusive)
            if spec.min_value is not None and coerced <= spec.min_value:
                events.append(SafetyEvent(
                    event_type=SafetyEventType.INVALID_PARAMETER,
                    incident_id=incident_id,
                    pod_name=pod_name,
                    namespace=namespace,
                    proposed_action=action,
                    veto_reason=(
                        f"Parameter '{spec.name}' = {coerced} is out of range "
                        f"(must be > {spec.min_value})."
                    ),
                    proposed_parameters=parameters,
                    layer=3,
                ))
            elif spec.max_value is not None and coerced > spec.max_value:
                events.append(SafetyEvent(
                    event_type=SafetyEventType.INVALID_PARAMETER,
                    incident_id=incident_id,
                    pod_name=pod_name,
                    namespace=namespace,
                    proposed_action=action,
                    veto_reason=(
                        f"Parameter '{spec.name}' = {coerced} exceeds maximum "
                        f"allowed value {spec.max_value}."
                    ),
                    proposed_parameters=parameters,
                    layer=3,
                ))

        elif spec.type == ParamType.STRING:
            if not isinstance(raw, str):
                events.append(SafetyEvent(
                    event_type=SafetyEventType.INVALID_PARAMETER,
                    incident_id=incident_id,
                    pod_name=pod_name,
                    namespace=namespace,
                    proposed_action=action,
                    veto_reason=(
                        f"Parameter '{spec.name}' must be a string; "
                        f"received {type(raw).__name__!r}."
                    ),
                    proposed_parameters=parameters,
                    layer=3,
                ))
            elif spec.choices and raw not in spec.choices:
                events.append(SafetyEvent(
                    event_type=SafetyEventType.INVALID_PARAMETER,
                    incident_id=incident_id,
                    pod_name=pod_name,
                    namespace=namespace,
                    proposed_action=action,
                    veto_reason=(
                        f"Parameter '{spec.name}' value {raw!r} not in "
                        f"allowed choices: {spec.choices}."
                    ),
                    proposed_parameters=parameters,
                    layer=3,
                ))

    return events


# ── Main gate function ────────────────────────────────────────────────────

class SafetyVeto(Exception):
    """
    Raised when Layers 1, 2, or 3 block an action.

    Callers catch this and return an escalation result instead of
    executing the Kubernetes API call.
    """
    def __init__(self, events: list[SafetyEvent]):
        self.events = events
        reasons = "; ".join(e.veto_reason for e in events)
        super().__init__(f"Safety veto ({len(events)} violation(s)): {reasons}")


def validate_step(
    step: dict,
    incident: dict,
    incident_id: Optional[int] = None,
) -> tuple[str, dict, list[SafetyEvent]]:
    """
    Validate one LLM remediation step through Layers 2 and 3.

    Returns (action, sanitised_parameters, informational_events) on success.
    Raises SafetyVeto on any Layer 2 or Layer 3 violation.

    Layer 1 (safe_to_auto_remediate) is checked by remediate() before this
    function is ever called, so it is not repeated here.

    Parameters
    ----------
    step       : one dict from diagnosis['remediation_steps']
    incident   : the incident dict (for pod_name/namespace context)
    incident_id: the DB row id of the incident, if already saved

    Returns
    -------
    action               : the validated action string
    sanitised_parameters : parameters after coercion (increase_pct as float)
    informational_events : non-blocking events (e.g. absent optional param)
    """
    pod_name  = incident.get("pod_name", "unknown")
    namespace = incident.get("namespace", "default")
    action    = step.get("action") or ""
    params    = dict(step.get("parameters") or {})

    veto_events: list[SafetyEvent] = []

    # ── Layer 2: Allow-list ───────────────────────────────────────────────
    if action not in ALLOWED_ACTIONS:
        veto_events.append(SafetyEvent(
            event_type=SafetyEventType.UNLISTED_ACTION,
            incident_id=incident_id,
            pod_name=pod_name,
            namespace=namespace,
            proposed_action=action or "(empty)",
            veto_reason=(
                f"Action '{action}' is not in the allow-list "
                f"{sorted(ALLOWED_ACTIONS)}. No Kubernetes API call was made."
            ),
            proposed_parameters=params,
            layer=2,
        ))
        raise SafetyVeto(veto_events)

    # ── Layer 3: Parameter schema ─────────────────────────────────────────
    param_events = _validate_parameters(action, params, pod_name, namespace, incident_id)
    if any(e.event_type in (SafetyEventType.INVALID_PARAMETER,
                            SafetyEventType.EXTRA_PARAMETER)
           for e in param_events):
        raise SafetyVeto(param_events)

    # Coerce increase_pct to float (LLM sometimes returns it as a string)
    if action == "increase_memory_limit" and "increase_pct" in params:
        params["increase_pct"] = float(params["increase_pct"])

    return action, params, param_events


def record_events(events: list[SafetyEvent]) -> None:
    """
    Persist *events* to the database.

    Wrapped in a try/except so a DB write failure never crashes the daemon.
    """
    if not events:
        return
    try:
        import db
        for event in events:
            db.record_safety_event(event.to_dict())
    except Exception as exc:
        print(f"[safety_layer] Warning: could not persist safety event: {exc}")
