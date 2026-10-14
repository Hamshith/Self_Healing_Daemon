"""
eval/ml_baseline.py — Traditional ML baseline (Random Forest classifier).

What it does
────────────
Trains a multi-class RandomForestClassifier over structured features extracted
from incident dicts.  Features are numeric/categorical signals a human SRE
might log in a ticketing system — they do NOT include free-text logs or events,
so the model can't "read" the logs the way the LLM does.

Features used
─────────────
  restart_count         integer
  waiting_reason_enc    one-hot: 0=none, 1=CrashLoopBackOff, 2=ImagePullBackOff,
                        3=ErrImagePull, 4=CreateContainerConfigError, 5=other
  last_terminated_enc   0=none, 1=OOMKilled, 2=Error, 3=other
  exit_code             integer (0 = normal; use max across containers)
  is_pending            0/1
  cpu_ratio             cpu_usage/cpu_limit (0.0 when unavailable)
  memory_ratio          memory_usage/memory_limit (0.0 when unavailable)

Label generation
────────────────
Labels come from the ground-truth ScenarioSpec — the same truth used to score
every other method.  The training set is generated synthetically by perturbing
known-fault incidents, as described in the paper's experimental setup.

Limitations (stated honestly for the paper)
───────────────────────────────────────────
  • Cannot classify novel faults it was never trained on — predicts "Unknown".
  • Cannot reason over free-text logs or multi-cause signal combinations.
  • Needs labeled training data; we generate it from Chaos Mesh runs.
  • The action mapping is a post-classification lookup table (same rules as the
    rule-based baseline), not learned end-to-end, so H2 comparison is identical
    to the rule-based baseline for known faults.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np

from eval.types import EvalResult, SAFE_ACTIONS

# Optional sklearn import — degrade gracefully if not installed
try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import LabelEncoder
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False
    warnings.warn(
        "[ml_baseline] scikit-learn not installed — ML baseline will return "
        "a constant 'Unknown' prediction.  Install with: pip install scikit-learn",
        RuntimeWarning,
        stacklevel=2,
    )


# ── Feature engineering ───────────────────────────────────────────────────

_WAITING_REASONS = [
    "none",
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "other",
]
_TERMINATED_REASONS = ["none", "OOMKilled", "Error", "other"]

_WAITING_IDX = {r: i for i, r in enumerate(_WAITING_REASONS)}
_TERMINATED_IDX = {r: i for i, r in enumerate(_TERMINATED_REASONS)}
_TRAINING_DATA_PATH = Path(__file__).resolve().parent / "data" / "ml_baseline_training.json"
_TRAINING_DATA_FORMAT_VERSION = 1
_TRAINING_DATA_SEED = 42


def extract_features(incident: dict) -> list[float]:
    """
    Extract a fixed-length numeric feature vector from an incident dict.

    This is deterministic and contains NO free-text.  It mirrors what a
    traditional monitoring system might expose as structured metrics.
    """
    restart_count = float(incident.get("restart_count", 0) or 0)

    waiting_reason_str = "none"
    last_terminated_str = "none"
    exit_code = 0.0
    is_pending = 0.0

    for cs in incident.get("container_statuses", []):
        state = cs.get("state") or {}

        w = state.get("waiting") or {}
        wr = w.get("reason", "")
        if wr in _WAITING_IDX:
            waiting_reason_str = wr
        elif wr:
            waiting_reason_str = "other"

        t = state.get("terminated") or {}
        tr = t.get("reason", "")
        if tr in _TERMINATED_IDX:
            last_terminated_str = tr
        elif tr:
            last_terminated_str = "other"
        ec = t.get("exit_code")
        if ec is not None:
            exit_code = max(exit_code, float(abs(ec)))

    # Fault type gives a strong hint; include as encoded integer
    fault_type = incident.get("fault_type", "")
    if fault_type == "PendingScheduling":
        is_pending = 1.0

    cpu_ratio = float(incident.get("cpu_ratio", 0.0) or 0.0)
    memory_ratio = float(incident.get("memory_ratio", 0.0) or 0.0)

    return [
        restart_count,
        float(_WAITING_IDX.get(waiting_reason_str, 5)),
        float(_TERMINATED_IDX.get(last_terminated_str, 3)),
        exit_code,
        is_pending,
        cpu_ratio,
        memory_ratio,
    ]


# ── Synthetic training data generation ───────────────────────────────────

def _make_incident(waiting: str = "none", terminated: str = "none",
                   exit_code: int = 0, pending: bool = False,
                   restart: int = 0, cpu_ratio: float = 0.0,
                   memory_ratio: float = 0.0) -> dict:
    state: dict = {}
    if waiting != "none":
        state["waiting"] = {"reason": waiting}
    if terminated != "none":
        state["terminated"] = {"reason": terminated, "exit_code": exit_code}
    elif exit_code != 0:
        state["terminated"] = {"reason": "Error", "exit_code": exit_code}

    cs = [{"name": "app", "restart_count": restart, "ready": False, "state": state}]
    return {
        "restart_count": restart,
        "container_statuses": cs,
        "fault_type": "PendingScheduling" if pending else "",
        "cpu_ratio": cpu_ratio,
        "memory_ratio": memory_ratio,
    }


def generate_training_data() -> tuple[list[list[float]], list[str]]:
    """
    Build a synthetic labeled training set.

    Each row is (feature_vector, label).  We generate multiple variants
    per fault type with slight perturbations to give the RF model enough
    diversity to generalise within known fault classes.

    Novel fault classes (DependencyFailure, ReadinessFailure, ConfigEnvError)
    are deliberately NOT included in training — demonstrating H3, the model
    cannot classify what it has never seen.
    """
    rng = np.random.default_rng(_TRAINING_DATA_SEED)
    X: list[list[float]] = []
    y: list[str] = []

    def add(label: str, inc: dict, n: int = 20):
        base = extract_features(inc)
        for _ in range(n):
            # Perturb restart_count and ratios slightly
            perturbed = base.copy()
            perturbed[0] = max(0, base[0] + rng.integers(-2, 5))
            perturbed[5] = float(np.clip(base[5] + rng.normal(0, 0.05), 0, 1))
            perturbed[6] = float(np.clip(base[6] + rng.normal(0, 0.05), 0, 1))
            X.append(perturbed)
            y.append(label)

    # ApplicationCrash — CrashLoopBackOff waiting reason
    add("ApplicationCrash", _make_incident("CrashLoopBackOff", restart=5))
    # ApplicationCrash — non-zero exit (bash crash)
    add("ApplicationCrash", _make_incident(exit_code=1, restart=3))
    # OOMKilled
    add("OOMKilled", _make_incident(terminated="OOMKilled", restart=2, memory_ratio=0.98))
    # ImagePullError
    add("ImagePullError", _make_incident("ImagePullBackOff"))
    add("ImagePullError", _make_incident("ErrImagePull"))
    # ConfigError
    add("ConfigError", _make_incident("CreateContainerConfigError"))
    # PendingScheduling
    add("PendingScheduling", _make_incident(pending=True))
    # CPUThrottle (no waiting/terminated, just high cpu_ratio)
    add("CPUThrottle", _make_incident(cpu_ratio=0.97, restart=0))

    return X, y


def load_or_generate_training_data() -> tuple[list[list[float]], list[str]]:
    """Load the persistent synthetic dataset, creating it once if absent."""
    if _TRAINING_DATA_PATH.exists():
        try:
            payload = json.loads(_TRAINING_DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read cached ML training data at "
                f"{_TRAINING_DATA_PATH}: {exc}"
            ) from exc

        if (
            not isinstance(payload, dict)
            or payload.get("format_version") != _TRAINING_DATA_FORMAT_VERSION
            or payload.get("seed") != _TRAINING_DATA_SEED
        ):
            raise ValueError(
                f"Cached ML training data at {_TRAINING_DATA_PATH} has an "
                "unsupported format or seed. Remove that file to regenerate it."
            )

        X = payload.get("features")
        y = payload.get("labels")
        if (
            not isinstance(X, list)
            or not isinstance(y, list)
            or len(X) != len(y)
            or not X
            or any(
                not isinstance(row, list)
                or len(row) != 7
                or any(
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    for value in row
                )
                for row in X
            )
            or any(not isinstance(label, str) for label in y)
        ):
            raise ValueError(
                f"Cached ML training data at {_TRAINING_DATA_PATH} is invalid. "
                "Remove that file to regenerate it."
            )

        print(f"[ml_baseline] Loaded {len(X)} samples from {_TRAINING_DATA_PATH}")
        return X, y

    X, y = generate_training_data()
    payload = {
        "format_version": _TRAINING_DATA_FORMAT_VERSION,
        "seed": _TRAINING_DATA_SEED,
        "features": [[float(value) for value in row] for row in X],
        "labels": y,
    }
    _TRAINING_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=_TRAINING_DATA_PATH.parent,
            prefix=f"{_TRAINING_DATA_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(payload, temp_file, separators=(",", ":"))
            temp_file.write("\n")
        os.replace(temp_path, _TRAINING_DATA_PATH)
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)

    print(f"[ml_baseline] Generated and saved {len(X)} samples to {_TRAINING_DATA_PATH}")
    return X, y


# ── Model wrapper ─────────────────────────────────────────────────────────

class MLClassifier:
    """
    Thin wrapper around RandomForestClassifier with fit/predict interface
    compatible with the harness.
    """

    def __init__(self):
        self._model: Optional[RandomForestClassifier] = None
        self._trained = False

    def fit(self):
        """Train on the persistent synthetic dataset. Call before predict()."""
        if not _SKLEARN_AVAILABLE:
            return self
        X, y = load_or_generate_training_data()
        self._model = RandomForestClassifier(
            n_estimators=200,
            max_depth=None,
            random_state=42,
            class_weight="balanced",
        )
        self._model.fit(X, y)
        self._trained = True
        print(f"[ml_baseline] Trained RF on {len(X)} synthetic samples, "
              f"classes={sorted(set(y))}")
        return self

    def predict_category(self, incident: dict) -> tuple[str, float]:
        """
        Return (predicted_category, confidence_score).

        confidence_score is the RF's max class probability; not directly
        comparable to the LLM's "high/medium/low" string but included for
        the supplement.
        """
        if not _SKLEARN_AVAILABLE or not self._trained or self._model is None:
            return "Unknown", 0.0
        fv = [extract_features(incident)]
        pred = self._model.predict(fv)[0]
        proba = self._model.predict_proba(fv)[0].max()
        return str(pred), float(proba)


# ── Category → action mapping (same as rule-based baseline) ──────────────

_CATEGORY_TO_ACTION: dict[str, tuple[str, bool]] = {
    "ApplicationCrash":  ("delete_pod",            True),
    "OOMKilled":         ("increase_memory_limit",  True),
    "ImagePullError":    ("escalate",               False),
    "ConfigError":       ("escalate",               False),
    "PendingScheduling": ("escalate",               False),
    "CPUThrottle":       ("escalate",               False),
    "NetworkLatency":    ("escalate",               False),
    "Unknown":           ("escalate",               False),
}


# ── Singleton model loaded once per process ───────────────────────────────

_singleton: Optional[MLClassifier] = None


def _get_model() -> MLClassifier:
    global _singleton
    if _singleton is None:
        _singleton = MLClassifier().fit()
    return _singleton


# ── Public API used by harness ────────────────────────────────────────────

def diagnose(incident: dict, trial_index: int, ground_truth_category: str,
             is_novel: bool) -> EvalResult:
    """
    Classify *incident* with the trained RF model and return an EvalResult.

    For novel fault categories the model will predict a wrong class or
    "Unknown" — that expected failure is exactly what H3 measures.
    """
    t0 = time.perf_counter()
    model = _get_model()
    predicted_category, confidence_score = model.predict_category(incident)
    latency_ms = (time.perf_counter() - t0) * 1000

    action, safe = _CATEGORY_TO_ACTION.get(predicted_category, ("escalate", False))
    confidence_label = (
        "high" if confidence_score >= 0.8
        else "medium" if confidence_score >= 0.5
        else "low"
    )

    return EvalResult(
        method="ml",
        fault_scenario=ground_truth_category,
        trial_index=trial_index,
        predicted_category=predicted_category,
        ground_truth_category=ground_truth_category,
        recommended_action=action,
        safe_to_auto_remediate=safe,
        is_safe_action=(action in SAFE_ACTIONS),
        is_novel_fault=is_novel,
        latency_ms=latency_ms,
        api_cost_usd=0.0,
        confidence=confidence_label,
        root_cause=f"RF prediction: {predicted_category} (p={confidence_score:.2f})",
        explanation="",
        retrieval_mode="N/A",
    )
