"""
eval/harness.py — Main evaluation orchestrator.

Usage
─────
    # Run the existing baselines, 5 trials each (quick smoke-test):
    python -m eval.harness --trials 5 --output eval/results/run_001.jsonl

    # Full evaluation, 20 trials:
    python -m eval.harness --trials 20 --output eval/results/run_full.jsonl

    # Known-fault scenarios only (no novel, no LLM calls):
    python -m eval.harness --scenarios known --methods rule_based ml --trials 20

    # Novel faults only, remote LLM methods only:
    python -m eval.harness --scenarios novel --methods llm_no_rag llm_rag --trials 5

    # Compare the local 3B adapter with the other baselines:
    python -m eval.harness --methods rule_based ml llm_no_rag finetuned_3b

    # Dry-run: builds incident dicts but skips LLM inference:
    python -m eval.harness --dry-run --trials 3

Controls enforced by the harness
─────────────────────────────────
  • Identical incident dict for every method in the same trial — no method
    receives different signals.
  • Same safety allow-list for all methods (from eval.types.SAFE_ACTIONS).
  • RAG query for llm_rag deliberately excludes the fault_type label.
  • Remote model version and temperature are read from config.MODEL and EVAL_TEMPERATURE.
  • Results are written as JSONL so the file survives partial runs; each trial
    is flushed immediately after completion.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure the project root is on sys.path when running as a module
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config
import rag_engine
from eval.types import EvalResult, ScenarioSpec
from eval.scenarios import KNOWN_SCENARIOS, NOVEL_SCENARIOS, ALL_SCENARIOS
import eval.rule_based as rule_based
import eval.ml_baseline as ml_baseline


# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_TRIALS = 20
DEFAULT_OUTPUT = "eval/results/run_{timestamp}.jsonl"
LOCAL_LLM_METHODS = {"finetuned_3b", "finetuned_7b"}
DEFAULT_METHODS = ["rule_based", "ml", "llm_no_rag", "llm_rag"]
ALL_METHODS = DEFAULT_METHODS + ["finetuned_3b", "finetuned_7b"]

# Seconds to wait between consecutive LLM calls to avoid rate-limiting.
LLM_INTER_CALL_SLEEP = float(os.getenv("EVAL_LLM_SLEEP", "2.0"))


# ── Incident builder ─────────────────────────────────────────────────────

def _build_incident_from_scenario(scenario: ScenarioSpec, trial_index: int) -> dict:
    """
    Build an incident dict that every method receives for a given trial.

    For known faults: attempts to collect live signals from the cluster.
    For novel faults: uses the ScenarioSpec's synthetic_logs and
    synthetic_events (the detector wouldn't classify them anyway).

    If the cluster is unreachable, falls back to a stub incident that still
    contains the synthetic fields — useful for dry-runs and CI environments
    without a live cluster.
    """
    base: dict = {
        "pod_name": f"eval-{scenario.name.lower().replace('_', '-')}-{trial_index:03d}",
        "namespace": "default",
        "fault_type": scenario.detector_fault_type or "Unknown",
        "restart_count": 0,
        "container_statuses": [],
        "kubernetes_events": list(scenario.synthetic_events),
        "recent_logs": scenario.synthetic_logs,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "eval_scenario": scenario.name,
        "eval_trial": trial_index,
    }

    if scenario.detector_fault_type is not None:
        # Try to collect live signals from the cluster if possible
        base = _enrich_from_cluster(base, scenario)

    return base


def _enrich_from_cluster(incident: dict, scenario: ScenarioSpec) -> dict:
    """
    Attempt to collect real pod signals from the cluster.

    Falls back to the stub dict if the cluster is unreachable, the pod
    hasn't started yet, or any API call fails.  The harness always runs —
    live signals are preferred but not required.
    """
    try:
        import collector
        pod_name = incident["pod_name"]
        namespace = incident["namespace"]

        # Collect logs (empty string on failure — already handled by collector)
        logs = collector.get_pod_logs(pod_name, namespace)
        events = collector.get_kubernetes_events(pod_name, namespace)

        if logs:
            incident["recent_logs"] = logs
        if events:
            incident["kubernetes_events"] = events

        # Collect container statuses
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        v1 = k8s_client.CoreV1Api()
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        statuses = []
        for cs in (pod.status.container_statuses or []):
            state_dict: dict = {}
            if cs.state:
                if cs.state.waiting:
                    state_dict["waiting"] = {
                        "reason": cs.state.waiting.reason,
                        "message": cs.state.waiting.message,
                    }
                elif cs.state.terminated:
                    state_dict["terminated"] = {
                        "reason": cs.state.terminated.reason,
                        "exit_code": cs.state.terminated.exit_code,
                    }
            statuses.append({
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "state": state_dict,
            })
        incident["container_statuses"] = statuses
        incident["restart_count"] = (
            max((s["restart_count"] for s in statuses), default=0) if statuses else 0
        )
    except Exception as exc:
        print(f"[harness] Cluster enrichment failed ({exc}); using stub incident")

    return incident


# ── Per-trial runner ──────────────────────────────────────────────────────

def _run_trial(
    scenario: ScenarioSpec,
    trial_index: int,
    methods: list[str],
    dry_run: bool = False,
    incident: Optional[dict] = None,
) -> list[EvalResult]:
    """
    Run all requested methods on a single trial of a single scenario.

    Returns a list of EvalResults (one per method).
    """
    if incident is None:
        incident = _build_incident_from_scenario(scenario, trial_index)
    results: list[EvalResult] = []

    for method in methods:
        print(f"  [{method}] trial={trial_index} scenario={scenario.name}")

        if method == "rule_based":
            r = rule_based.diagnose(
                incident, trial_index,
                scenario.ground_truth_category, scenario.is_novel,
            )

        elif method == "ml":
            r = ml_baseline.diagnose(
                incident, trial_index,
                scenario.ground_truth_category, scenario.is_novel,
            )

        elif method == "llm_no_rag":
            if dry_run:
                r = _stub_result("llm_no_rag", scenario, trial_index)
            else:
                from eval.llm_methods import diagnose_no_rag
                r = diagnose_no_rag(
                    incident, trial_index,
                    scenario.ground_truth_category, scenario.is_novel,
                )
                time.sleep(LLM_INTER_CALL_SLEEP)

        elif method == "llm_rag":
            if dry_run:
                r = _stub_result("llm_rag", scenario, trial_index)
            else:
                from eval.llm_methods import diagnose_rag
                r = diagnose_rag(
                    incident, trial_index,
                    scenario.ground_truth_category, scenario.is_novel,
                )
                time.sleep(LLM_INTER_CALL_SLEEP)

        elif method in LOCAL_LLM_METHODS:
            if dry_run:
                r = _stub_result(method, scenario, trial_index)
            else:
                from eval.local_llm_methods import diagnose_local
                r = diagnose_local(
                    method, incident, trial_index,
                    scenario.ground_truth_category, scenario.is_novel,
                )

        else:
            print(f"[harness] Unknown method: {method}, skipping")
            continue

        results.append(r)
        print(
            f"    -> predicted={r.predicted_category} correct={r.category_correct} "
            f"action={r.recommended_action} latency={r.latency_ms:.1f}ms "
            f"cost=${r.api_cost_usd:.6f}"
        )

    return results


def _stub_result(method: str, scenario: ScenarioSpec, trial_index: int) -> EvalResult:
    """Return a placeholder EvalResult for dry-runs (no LLM inference made)."""
    from eval.types import SAFE_ACTIONS
    return EvalResult(
        method=method,
        fault_scenario=scenario.ground_truth_category,
        trial_index=trial_index,
        predicted_category="DRY_RUN",
        ground_truth_category=scenario.ground_truth_category,
        recommended_action="escalate",
        safe_to_auto_remediate=False,
        is_safe_action=True,
        is_novel_fault=scenario.is_novel,
        latency_ms=0.0,
        api_cost_usd=0.0,
        confidence="N/A",
        root_cause="DRY_RUN — no LLM inference made",
        explanation="",
        retrieval_mode="N/A",
    )


# ── Result serialisation ──────────────────────────────────────────────────

def _result_to_dict(r: EvalResult) -> dict:
    return {
        "method":                 r.method,
        "fault_scenario":         r.fault_scenario,
        "trial_index":            r.trial_index,
        "predicted_category":     r.predicted_category,
        "ground_truth_category":  r.ground_truth_category,
        "category_correct":       r.category_correct,
        "recommended_action":     r.recommended_action,
        "safe_to_auto_remediate": r.safe_to_auto_remediate,
        "is_safe_action":         r.is_safe_action,
        "unsafe_action":          r.unsafe_action,
        "is_novel_fault":         r.is_novel_fault,
        "latency_ms":             r.latency_ms,
        "api_cost_usd":           r.api_cost_usd,
        "confidence":             r.confidence,
        "root_cause":             r.root_cause,
        "explanation":            r.explanation,
        "retrieval_mode":         r.retrieval_mode,
        "recorded_at":            datetime.now(timezone.utc).isoformat(),
    }


# ── Main orchestrator ────────────────────────────────────────────────────

def run(
    scenarios: list[ScenarioSpec],
    methods: list[str],
    n_trials: int,
    output_path: str,
    dry_run: bool = False,
) -> list[EvalResult]:
    """
    Run the full evaluation and write results to *output_path* as JSONL.

    Each trial is flushed to disk immediately so a partial run is not lost.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Ensure the RAG index is built once before any llm_rag calls.
    if "llm_rag" in methods and not dry_run:
        print("[harness] Building RAG index …")
        rag_engine.build_index()

    # Train the ML model once.
    if "ml" in methods:
        print("[harness] Training ML classifier …")
        ml_baseline._get_model()

    all_results: list[EvalResult] = []
    total = len(scenarios) * n_trials * len(methods)
    done = 0
    local_methods = [method for method in methods if method in LOCAL_LLM_METHODS]
    non_local_methods = [
        method for method in methods if method not in LOCAL_LLM_METHODS
    ]
    trial_incidents: list[tuple[ScenarioSpec, int, dict]] = []
    local_model_metadata = {}
    if local_methods:
        from eval.local_llm_methods import get_local_model_metadata

        local_model_metadata = get_local_model_metadata(local_methods)

    run_meta = {
        "harness_version": "1.0.0",
        "model": config.MODEL,
        "local_models": local_model_metadata,
        "n_trials": n_trials,
        "methods": methods,
        "scenarios": [s.name for s in scenarios],
        "dry_run": dry_run,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(output_path, "w", encoding="utf-8") as fh:
        # Write metadata as the first line
        fh.write(json.dumps({"type": "meta", **run_meta}) + "\n")
        fh.flush()

        def record_results(trial_results: list[EvalResult]) -> None:
            nonlocal done
            for result in trial_results:
                fh.write(json.dumps(_result_to_dict(result)) + "\n")
                fh.flush()
                all_results.append(result)
                done += 1

        for scenario in scenarios:
            print(f"\n[harness] Scenario: {scenario.name} "
                  f"(novel={scenario.is_novel}, truth={scenario.ground_truth_category})")

            for trial_index in range(n_trials):
                incident = _build_incident_from_scenario(scenario, trial_index)
                trial_incidents.append((scenario, trial_index, incident))
                trial_results = _run_trial(
                    scenario, trial_index, non_local_methods, dry_run, incident
                )
                record_results(trial_results)

                print(f"  [harness] {done}/{total} calls done")

        if local_methods and not dry_run:
            from eval.local_llm_methods import release_local_model

            try:
                for method in local_methods:
                    print(f"\n[harness] Evaluating {method} across all trials")
                    for scenario, trial_index, incident in trial_incidents:
                        trial_results = _run_trial(
                            scenario, trial_index, [method], incident=incident
                        )
                        record_results(trial_results)
                        print(f"  [harness] {done}/{total} calls done")
            finally:
                release_local_model()
        elif local_methods:
            for method in local_methods:
                for scenario, trial_index, incident in trial_incidents:
                    record_results(
                        _run_trial(
                            scenario, trial_index, [method], dry_run=True,
                            incident=incident,
                        )
                    )
                    print(f"  [harness] {done}/{total} calls done")

    print(f"\n[harness] Evaluation complete. {len(all_results)} results -> {output_path}")
    return all_results


# ── CLI entry point ───────────────────────────────────────────────────────

def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Self-Healing Daemon — comparative evaluation harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--trials", type=int, default=DEFAULT_TRIALS,
        help=f"Number of trials per scenario (default: {DEFAULT_TRIALS})",
    )
    p.add_argument(
        "--scenarios", choices=["all", "known", "novel"], default="all",
        help="Which scenario set to run (default: all)",
    )
    p.add_argument(
        "--methods", nargs="+", default=DEFAULT_METHODS,
        choices=ALL_METHODS,
        help="Which methods to run (default: existing four methods)",
    )
    p.add_argument(
        "--output", default=None,
        help="Output JSONL file path. Defaults to eval/results/run_<timestamp>.jsonl",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Build incident dicts and skip all LLM inference",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    if args.scenarios == "known":
        scenarios = KNOWN_SCENARIOS
    elif args.scenarios == "novel":
        scenarios = NOVEL_SCENARIOS
    else:
        scenarios = ALL_SCENARIOS

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or f"eval/results/run_{timestamp}.jsonl"

    print(f"[harness] Starting evaluation")
    print(f"  scenarios : {args.scenarios} ({len(scenarios)} total)")
    print(f"  methods   : {args.methods}")
    print(f"  trials    : {args.trials}")
    print(f"  output    : {output}")
    print(f"  dry_run   : {args.dry_run}")
    print(f"  model     : {config.MODEL}")

    run(
        scenarios=scenarios,
        methods=args.methods,
        n_trials=args.trials,
        output_path=output,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
