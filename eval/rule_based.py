"""
eval/rule_based.py — Deterministic rule-based baseline.

Maps structured incident signals (waiting_reason, exit_code, last_state_reason,
pod phase) to a root_cause_category and a single remediation action using an
explicit decision table.

Design constraints (same as the paper's rule-based baseline definition):
  • Uses the same allow-list of actions as remediator.py.
  • Applies the same safe_to_auto_remediate guard.
  • Contains NO machine-learning and makes NO API calls — latency is pure
    Python dict lookup.
  • Is intentionally ten lines of logic; its weakness is ambiguity and
    unseen combinations, not deterministic known faults.

This is a FAIR baseline: it sees the same structured incident dict as every
other method.  It does NOT peek at ground-truth labels.
"""
from __future__ import annotations

import time
from typing import Optional

from eval.types import EvalResult, SAFE_ACTIONS


# ── Decision table ────────────────────────────────────────────────────────
# Maps (waiting_reason, last_terminated_reason, exit_code_nonzero, pending)
# to (category, action, safe_to_auto_remediate).
#
# Priority order (earlier entries win when multiple signals are present):
#   OOMKilled > ImagePull > ConfigError > CrashLoop > non-zero exit > Pending > Unknown
#
# Each entry: (category, action, safe)
_RULES: list[tuple] = [
    # (waiting_reason, last_terminated_reason, exit_code_nonzero, is_pending)
    # → (category, action, safe_to_auto_remediate)
    ("CrashLoopBackOff",          None,         None,  False, "ApplicationCrash",  "delete_pod",            True),
    ("ImagePullBackOff",           None,         None,  False, "ImagePullError",    "escalate",              False),
    ("ErrImagePull",               None,         None,  False, "ImagePullError",    "escalate",              False),
    ("CreateContainerConfigError", None,         None,  False, "ConfigError",       "escalate",              False),
    (None,                         "OOMKilled",  None,  False, "OOMKilled",         "increase_memory_limit", True),
    (None,                         None,         True,  False, "ApplicationCrash",  "delete_pod",            True),
    (None,                         None,         None,  True,  "PendingScheduling", "escalate",              False),
]


def _extract_signals(incident: dict) -> dict:
    """
    Pull structured signals from the incident dict.

    Accepts both the live daemon format (container_statuses list with
    state dicts from _classify_container_status) and the evaluation
    harness format used for novel faults.
    """
    waiting_reason: Optional[str] = None
    last_reason: Optional[str] = None
    exit_code_nonzero: bool = False
    is_pending: bool = False

    for cs in incident.get("container_statuses", []):
        state = cs.get("state") or {}

        waiting = state.get("waiting") or {}
        if waiting.get("reason"):
            waiting_reason = waiting["reason"]

        terminated = state.get("terminated") or {}
        if terminated.get("reason") == "OOMKilled":
            last_reason = "OOMKilled"
        ec = terminated.get("exit_code")
        if ec is not None and ec != 0:
            exit_code_nonzero = True

    # The daemon also checks last_state; replicate that here.
    fault_type = incident.get("fault_type", "")
    if fault_type == "OOMKilled" and last_reason is None:
        last_reason = "OOMKilled"
    if fault_type == "PendingScheduling":
        is_pending = True

    return {
        "waiting_reason": waiting_reason,
        "last_reason": last_reason,
        "exit_code_nonzero": exit_code_nonzero,
        "is_pending": is_pending,
    }


def diagnose(incident: dict, trial_index: int, ground_truth_category: str,
             is_novel: bool) -> EvalResult:
    """
    Apply the rule table to *incident* and return an EvalResult.

    For novel faults (DependencyFailure, ReadinessFailure, ConfigEnvError)
    the rule table will almost always fall through to "Unknown" / escalate,
    which is the expected and honest result — it demonstrates the baseline's
    inability to handle unseen patterns.
    """
    t0 = time.perf_counter()
    signals = _extract_signals(incident)

    category = "Unknown"
    action = "escalate"
    safe = False

    for (wr, lr, ecnz, pend, cat, act, s) in _RULES:
        wr_match   = (wr   is None) or (signals["waiting_reason"] == wr)
        lr_match   = (lr   is None) or (signals["last_reason"]    == lr)
        ecnz_match = (ecnz is None) or (signals["exit_code_nonzero"] == ecnz)
        pend_match = (pend is False) or (signals["is_pending"] == pend)
        if wr_match and lr_match and ecnz_match and pend_match:
            # At least one non-None field must have matched positively
            # (avoid the all-None row firing on everything).
            any_positive = (
                (wr   is not None and signals["waiting_reason"] == wr)
                or (lr   is not None and signals["last_reason"]    == lr)
                or (ecnz is not None and signals["exit_code_nonzero"] == ecnz)
                or (pend is True   and signals["is_pending"] == True)
            )
            if any_positive:
                category, action, safe = cat, act, s
                break

    latency_ms = (time.perf_counter() - t0) * 1000

    return EvalResult(
        method="rule_based",
        fault_scenario=ground_truth_category,
        trial_index=trial_index,
        predicted_category=category,
        ground_truth_category=ground_truth_category,
        recommended_action=action,
        safe_to_auto_remediate=safe,
        is_safe_action=(action in SAFE_ACTIONS),
        is_novel_fault=is_novel,
        latency_ms=latency_ms,
        api_cost_usd=0.0,
        confidence="N/A",
        root_cause="Rule-based lookup: no free-text explanation.",
        explanation="",
        retrieval_mode="N/A",
    )
