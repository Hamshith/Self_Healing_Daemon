"""
eval/types.py — shared data structures for the evaluation harness.

Every method (rule-based, ML, remote LLM, or local fine-tuned LLM) returns an
EvalResult so the harness can compare them on identical fields without
touching method-specific internals.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ── Canonical fault labels ────────────────────────────────────────────────
# The GROUND_TRUTH_CATEGORY for each trial is one of these strings.
# A method's prediction must be one of these strings to be scored as correct.
VALID_CATEGORIES = frozenset({
    "ApplicationCrash",
    "OOMKilled",
    "ImagePullError",
    "ConfigError",
    "PendingScheduling",
    "CPUThrottle",
    "NetworkLatency",
    "DependencyFailure",   # novel — not in detector rules
    "ReadinessFailure",    # novel — not in detector rules
    "ConfigEnvError",      # novel — bad env var only visible in logs
    "Unknown",
})

# ── Action allow-list ─────────────────────────────────────────────────────
# Same allow-list as remediator.py.  Evaluation tracks whether each method
# picks an action from this set, or recommends an unsafe free-form command.
SAFE_ACTIONS = frozenset({"delete_pod", "increase_memory_limit", "escalate"})


@dataclass
class EvalResult:
    """
    One method's output for a single trial of a single fault.

    All methods fill the same fields so the harness can do an
    apples-to-apples comparison across H1–H4.
    """
    # ── Identity ──────────────────────────────────────────────────────────
    method: str                     # method identifier from eval.harness.ALL_METHODS
    fault_scenario: str             # e.g. "OOMKilled", "ReadinessFailure"
    trial_index: int                # 0-based trial number within the scenario

    # ── H1 — Diagnosis accuracy ───────────────────────────────────────────
    predicted_category: str         # root_cause_category returned by the method
    ground_truth_category: str      # injected fault label

    # ── H2 — Remediation quality ──────────────────────────────────────────
    recommended_action: str         # primary action name from the first step
    safe_to_auto_remediate: bool    # did the method flag this as auto-safe?
    is_safe_action: bool            # recommended_action in SAFE_ACTIONS

    # ── H3 — Novel fault handling ─────────────────────────────────────────
    is_novel_fault: bool            # True for faults outside the detector's rulebook

    # ── H4 — Cost ─────────────────────────────────────────────────────────
    latency_ms: float               # wall-clock time for this method call, ms
    api_cost_usd: float             # estimated remote API cost; 0.0 for local inference

    # ── Optional fields filled by LLM methods ─────────────────────────────
    confidence: str = "N/A"         # "high" | "medium" | "low" | "N/A"
    root_cause: str = ""            # free-text root cause
    explanation: str = ""
    retrieval_mode: str = "N/A"     # "semantic" | "lexical" | "N/A"

    # ── Derived convenience properties ────────────────────────────────────
    @property
    def category_correct(self) -> bool:
        return self.predicted_category == self.ground_truth_category

    @property
    def unsafe_action(self) -> bool:
        """True when the method recommended an action not in the allow-list."""
        return not self.is_safe_action


@dataclass
class ScenarioSpec:
    """
    Describes one fault scenario used in the evaluation.

    The harness iterates over a list of ScenarioSpecs, injects each fault
    `n_trials` times, runs all methods, and collects EvalResults.
    """
    name: str                       # unique scenario identifier
    ground_truth_category: str      # injected fault label
    is_novel: bool                  # not in the detector's built-in rulebook?
    k8s_manifest: str               # path to the YAML fixture (relative to project root)
    detector_fault_type: Optional[str] = None  # what the detector emits; None = won't detect
    description: str = ""
    # Extra logs/events injected for novel faults where the detector can't extract them
    synthetic_logs: str = ""
    synthetic_events: list = field(default_factory=list)
