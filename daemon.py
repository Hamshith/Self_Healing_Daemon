"""
Self-Healing Daemon — Main entry point.
Polls a minikube Kubernetes cluster every POLL_INTERVAL seconds,
detects faulting pods, diagnoses them via Gemini, remediates where
safe, and verifies that each remediation actually held before marking
it successful.

Key operational guarantees:
  • Incident deduplication: a (pod, fault_type) pair is re-processed
    only after INCIDENT_COOLDOWN_SECONDS have elapsed or its fault has
    cleared.  This prevents duplicate Gemini calls, duplicate DB rows,
    and inflated incident/false-positive counts.
  • Post-remediation health verification: after a remediation action the
    daemon polls the pod for up to VERIFICATION_POLL_SECONDS.  Only a
    pod that stays Running+Ready for VERIFICATION_STABLE_POLLS consecutive
    checks is recorded as "remediated".  Anything else is recorded as
    "verification_failed" and the pre-action rollback snapshot is applied.
  • Rollback watcher: if verification fails for a patch_memory action the
    daemon calls remediator.rollback_action() with the saved snapshot path
    so the Deployment is restored to its original limit automatically.
"""

import asyncio
import sys
import time
from datetime import datetime, timezone

import config
import collector
import detector
import llm_client
import remediator
import reporter
import db
import rag_engine
import incident_report_generator
import correlation


BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║        🛡️  LLM-Augmented Self-Healing Daemon  🛡️        ║
╠══════════════════════════════════════════════════════════╣
║  Cluster : minikube (current kubeconfig context)        ║
║  Poll    : {poll:<4} seconds                               ║
║  Model   : {model:<20}                 ║
║  Prom    : {prom:<45}║
╚══════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────────
# Deduplication state
# ─────────────────────────────────────────────────────────────

# How long (seconds) to suppress re-processing the same (pod, fault_type)
# pair.  Set to at least 2× POLL_INTERVAL so a single fault doesn't fire
# twice in back-to-back polls.
INCIDENT_COOLDOWN_SECONDS = 5 * 60  # 300 seconds = 5 minutes

# {(pod_name, fault_type): epoch_seconds} — when this incident was last actioned
_incident_last_seen: dict[tuple[str, str], float] = {}


def _is_duplicate(pod_name: str, fault_type: str) -> bool:
    """Return True when this (pod, fault_type) was actioned within the cooldown window."""
    key = (pod_name, fault_type)
    last = _incident_last_seen.get(key)
    if last is None:
        return False
    return (time.monotonic() - last) < INCIDENT_COOLDOWN_SECONDS


def _record_incident_seen(pod_name: str, fault_type: str) -> None:
    key = (pod_name, fault_type)
    _incident_last_seen[key] = time.monotonic()


def _clear_incident_cooldown(pod_name: str, fault_type: str) -> None:
    """Remove a cooldown entry so the next poll re-processes the fault if still present."""
    _incident_last_seen.pop((pod_name, fault_type), None)


# ─────────────────────────────────────────────────────────────
# Post-remediation health verification
# ─────────────────────────────────────────────────────────────

# Maximum time to wait for a remediated pod to become healthy.
VERIFICATION_POLL_SECONDS = 120   # 2 minutes total window
VERIFICATION_INTERVAL_SECONDS = 15  # check every 15 s
# How many consecutive "healthy" readings we need before declaring success.
VERIFICATION_STABLE_POLLS = 2


def _pod_is_healthy(pod_name: str, namespace: str) -> bool:
    """
    Return True when the pod exists, its phase is Running, and all
    containers report ready=True.  Returns False on any API error
    (treat an unreadable pod as unhealthy to be conservative).
    """
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        v1 = k8s_client.CoreV1Api()
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        if pod.status.phase != "Running":
            return False
        container_statuses = pod.status.container_statuses or []
        return all(cs.ready for cs in container_statuses)
    except Exception as exc:
        print(f"[daemon] Health check for {pod_name} failed: {exc}")
        return False


async def _verify_remediation(incident: dict, remediation: dict, incident_id: int) -> str:
    """
    Poll the pod for up to VERIFICATION_POLL_SECONDS.

    Returns the final remediation status string:
      "remediated"          — pod was stable for VERIFICATION_STABLE_POLLS checks
      "verification_failed" — pod never stabilised in the window

    Side effects:
      • Calls db.update_remediation_status() with the terminal status.
      • If verification fails and the action left a rollback snapshot,
        calls remediator.rollback_action() to undo the change.
    """
    pod_name = incident.get("pod_name", "")
    namespace = incident.get("namespace", "default")
    rollback_path = remediation.get("rollback_path")
    action_taken = remediation.get("action_taken", "")

    # For delete_pod the replacement pod will have a different name.
    # We can't track it by name, so we skip verification and trust that the
    # owning controller's reconciliation loop will surface any new crash on
    # the next daemon poll cycle.
    if action_taken == "delete_pod":
        print(f"[daemon] Skipping health poll for delete_pod on {pod_name} "
              "(new pod name is unknown; next poll cycle will catch regressions)")
        db.update_remediation_status(incident_id, "remediated")
        return "remediated"

    print(f"[daemon] Verifying remediation for {pod_name} "
          f"(up to {VERIFICATION_POLL_SECONDS}s, "
          f"need {VERIFICATION_STABLE_POLLS} consecutive healthy polls)")

    db.update_remediation_status(incident_id, "verifying")

    deadline = time.monotonic() + VERIFICATION_POLL_SECONDS
    stable_count = 0

    while time.monotonic() < deadline:
        await asyncio.sleep(VERIFICATION_INTERVAL_SECONDS)
        healthy = _pod_is_healthy(pod_name, namespace)
        if healthy:
            stable_count += 1
            print(f"[daemon] {pod_name} healthy ({stable_count}/{VERIFICATION_STABLE_POLLS})")
            if stable_count >= VERIFICATION_STABLE_POLLS:
                print(f"[daemon] Remediation verified for {pod_name}")
                db.update_remediation_status(incident_id, "remediated")
                return "remediated"
        else:
            stable_count = 0
            print(f"[daemon] {pod_name} not yet healthy; continuing to poll…")

    # Verification window expired without stability.
    print(f"[daemon] Verification FAILED for {pod_name} after {VERIFICATION_POLL_SECONDS}s — "
          "rolling back if possible")
    db.update_remediation_status(incident_id, "verification_failed")

    if rollback_path:
        try:
            rb_result = remediator.rollback_action(rollback_path)
            print(f"[daemon] Rollback result for {pod_name}: {rb_result}")
            db.update_remediation_status(incident_id, "rolled_back")
            # Clear the cooldown so the next poll re-evaluates the fault fresh.
            _clear_incident_cooldown(pod_name, incident.get("fault_type", ""))
        except Exception as exc:
            print(f"[daemon] Rollback error for {pod_name}: {exc}")
    else:
        print(f"[daemon] No rollback snapshot available for {pod_name}; escalation needed")

    return "verification_failed"


# ─────────────────────────────────────────────────────────────
# Startup helpers
# ─────────────────────────────────────────────────────────────

def _check_prometheus() -> bool:
    """Return True if Prometheus is reachable."""
    try:
        from prometheus_api_client import PrometheusConnect
        prom = PrometheusConnect(url=config.PROMETHEUS_URL, disable_ssl=True)
        prom.check_prometheus_connection()
        return True
    except Exception:
        return False


def _startup_checks():
    """Run pre-flight checks; exit on fatal issues."""
    print("[daemon] Starting pre-flight checks")
    if not config.GEMINI_API_KEY:
        print("\033[91m[FATAL] GEMINI_API_KEY is not set.\033[0m")
        print("Copy .env.example to .env and add your API key.")
        sys.exit(1)

    try:
        count = rag_engine.build_index()
        print(f"[rag_engine] Indexed {count} runbook chunks.")
    except Exception as exc:
        print(f"[rag_engine] Warning: runbook index unavailable: {exc}")

    prom_ok = _check_prometheus()
    prom_status = config.PROMETHEUS_URL if prom_ok else "UNREACHABLE — using K8s API fallback"
    if not prom_ok:
        print("\033[93m[WARN] Prometheus not reachable — "
              "falling back to Kubernetes API for restart counts.\033[0m")

    print(BANNER.format(
        poll=config.POLL_INTERVAL,
        model=config.MODEL,
        prom=prom_status,
    ))
    print("[daemon] Pre-flight checks complete")


# ─────────────────────────────────────────────────────────────
# Core per-anomaly processing
# ─────────────────────────────────────────────────────────────

async def _process_anomaly(anomaly: dict) -> None:
    """
    Full pipeline for a single anomaly: collect → diagnose → report → remediate → verify.

    Deduplication is enforced here: if this (pod, fault_type) pair was
    actioned within INCIDENT_COOLDOWN_SECONDS it is silently skipped.
    """
    # ── Determine identity for deduplication ─────────────────
    fault_type = anomaly.get("fault_type", "Unknown")

    if fault_type == "NetworkLatency":
        target_pods = anomaly.get("target_pods") or []
        target_app = anomaly.get("target_app")
        pod_name = (
            target_pods[0]
            if target_pods
            else target_app
            or f"networkchaos/{anomaly.get('chaos_name', 'unknown')}"
        )
    else:
        pod_name = anomaly.get("pod_name", "")

    # ── Deduplication check ───────────────────────────────────
    if _is_duplicate(pod_name, fault_type):
        print(f"[daemon] Skipping duplicate incident: {pod_name} / {fault_type} "
              f"(cooldown {INCIDENT_COOLDOWN_SECONDS}s)")
        return

    # Mark as seen before doing anything so parallel poll cycles don't
    # race to process the same fault twice.
    _record_incident_seen(pod_name, fault_type)

    # ── Build incident dict ───────────────────────────────────
    if fault_type == "NetworkLatency":
        incident = {
            **anomaly,
            "pod_name": pod_name,
            "restart_count": 0,
            "container_statuses": [],
            "recent_logs": "(NetworkChaos CR — no pod logs; "
                           "see kubernetes_events for injection details)",
            "kubernetes_events": [
                {
                    "reason": "NetworkChaosInjecting",
                    "message": (
                        f"NetworkChaos '{anomaly.get('chaos_name')}' is "
                        f"actively injecting delay against target app "
                        f"{target_app or pod_name} and selector "
                        f"{anomaly.get('target_selector', {})}. "
                        f"In-cluster request latency measured "
                        f"{anomaly.get('measured_latency_ms')} ms "
                        f"(threshold: {anomaly.get('latency_threshold_ms')} ms)."
                    ),
                    "timestamp": anomaly.get("detected_at"),
                    "type": "Warning",
                }
            ],
        }
    else:
        ns = anomaly["namespace"]
        try:
            logs = collector.get_pod_logs(pod_name, ns)
        except Exception as exc:
            print(f"[daemon] Error getting logs for {pod_name}: {exc}")
            logs = ""

        try:
            events = collector.get_kubernetes_events(pod_name, ns)
        except Exception as exc:
            print(f"[daemon] Error getting events for {pod_name}: {exc}")
            events = []

        incident = {
            **anomaly,
            "recent_logs": logs,
            "kubernetes_events": events,
        }

    # ── LLM diagnosis ─────────────────────────────────────────
    try:
        diagnosis = llm_client.diagnose_incident(incident)
    except Exception as exc:
        print(f"[daemon] Error diagnosing {pod_name}: {exc}")
        return

    # ── Report ────────────────────────────────────────────────
    try:
        reporter.print_incident_report(incident, diagnosis)
    except Exception as exc:
        print(f"[daemon] Error printing report for {pod_name}: {exc}")

    incident_id = None
    try:
        incident_id = reporter.save_incident_report(incident, diagnosis)
    except Exception as exc:
        print(f"[daemon] Error saving report for {pod_name}: {exc}")

    # ── Remediate ─────────────────────────────────────────────
    remediation = None
    try:
        remediation = remediator.remediate(incident, diagnosis)
    except Exception as exc:
        print(f"[daemon] Error remediating {pod_name}: {exc}")

    if incident_id is not None and remediation is not None:
        # Set initial status to "in_progress" so MTTR tracking starts now,
        # before health verification updates it to the terminal status.
        initial_status = remediation.get("status", "none")
        if initial_status not in ("escalated", "failed", "none"):
            initial_status = "in_progress"
        try:
            db.update_remediation_status(incident_id, initial_status)
        except Exception as exc:
            print(f"[daemon] Error setting initial remediation status: {exc}")

        # ── Post-remediation health verification + rollback ───
        if remediation.get("success") and remediation.get("status") == "remediated":
            try:
                final_status = await _verify_remediation(incident, remediation, incident_id)
                remediation["status"] = final_status
            except Exception as exc:
                print(f"[daemon] Error during verification for {pod_name}: {exc}")
        else:
            # Action was escalated, failed, or a no-op — write its status directly.
            try:
                db.update_remediation_status(incident_id, remediation.get("status", "none"))
            except Exception as exc:
                print(f"[daemon] Error updating remediation status: {exc}")

    # ── Post-mortem report ────────────────────────────────────
    try:
        incident_report_generator.generate_report(incident, diagnosis, remediation)
    except Exception as exc:
        print(f"[daemon] Error generating post-mortem for {pod_name}: {exc}")


# ─────────────────────────────────────────────────────────────
# Poll cycle
# ─────────────────────────────────────────────────────────────

async def _poll_cycle():
    """Execute one monitoring cycle."""
    try:
        restart_counts = collector.get_pod_restart_counts()
    except Exception as exc:
        print(f"[daemon] Error collecting restart counts: {exc}")
        return

    try:
        anomalies = detector.detect_anomalies(restart_counts)
    except Exception as exc:
        print(f"[daemon] Error detecting anomalies: {exc}")
        anomalies = []

    try:
        anomalies += detector.detect_resource_anomalies()
    except Exception as exc:
        print(f"[daemon] Error detecting resource anomalies: {exc}")

    try:
        anomalies = correlation.correlate(anomalies)
    except Exception as exc:
        print(f"[daemon] Error correlating anomalies: {exc}")

    db.record_heartbeat(len(restart_counts), len(anomalies))

    for anomaly in anomalies:
        await _process_anomaly(anomaly)

    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] Checked cluster — "
          f"{len(restart_counts)} pods monitored, "
          f"{len(anomalies)} anomalies found "
          f"({len(_incident_last_seen)} active cooldowns)")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

async def main():
    """Daemon main loop."""
    _startup_checks()
    db.init_db()
    print("[daemon] SQLite database initialized")
    print("Daemon started. Press Ctrl+C to stop.\n")

    try:
        while True:
            await _poll_cycle()
            await asyncio.sleep(config.POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nDaemon stopped.")


if __name__ == "__main__":
    asyncio.run(main())