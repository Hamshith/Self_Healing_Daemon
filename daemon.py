"""
Self-Healing Daemon — Main entry point.
Polls a minikube Kubernetes cluster every POLL_INTERVAL seconds,
detects CrashLoopBackOff pods, diagnoses them via Gemini, and
prints / saves incident reports.
"""

import asyncio
import sys
from datetime import datetime

import config
import collector
import detector
import llm_client
import remediator
import reporter
import db


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
    if not config.GEMINI_API_KEY:
        print("\033[91m[FATAL] GEMINI_API_KEY is not set.\033[0m")
        print("Copy .env.example to .env and add your API key.")
        sys.exit(1)

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

    db.record_heartbeat(len(restart_counts), len(anomalies))

    for anomaly in anomalies:
        # CPUThrottle/ApplicationCrash/etc. are keyed by pod_name+namespace
        # and go through the normal per-pod log/event collection below.
        # NetworkLatency is cluster-wide (a Chaos Mesh object, not a
        # pod) — it has no pod_name, no logs, no restart count. Rather
        # than skip Gemini diagnosis for it (which used to leave an
        # incomplete record with no severity/root_cause/recommended
        # action), build a synthetic incident with the chaos object's
        # identity standing in for pod_name, and events synthesized
        # from the NetworkChaos spec, so it goes through the exact
        # same diagnosis + reporting pipeline as every other fault.
        if anomaly["fault_type"] == "NetworkLatency":
            target_pods = anomaly.get("target_pods") or []
            target_app = anomaly.get("target_app")
            pod_name = (
                target_pods[0]
                if target_pods
                else target_app
                or f"networkchaos/{anomaly.get('chaos_name', 'unknown')}"
            )

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

            try:
                diagnosis = llm_client.diagnose_incident(incident)
            except Exception as exc:
                print(f"[daemon] Error diagnosing NetworkLatency incident: {exc}")
                continue

            try:
                reporter.print_incident_report(incident, diagnosis)
            except Exception as exc:
                print(f"[daemon] Error printing NetworkLatency report: {exc}")

            incident_id = None
            try:
                incident_id = reporter.save_incident_report(incident, diagnosis)
            except Exception as exc:
                print(f"[daemon] Error saving NetworkLatency report: {exc}")

            try:
                remediation = remediator.remediate(incident, diagnosis)
            except Exception as exc:
                print(f"[daemon] Error remediating {incident.get('pod_name')}: {exc}")
                remediation = None

            if incident_id is not None and remediation is not None:
                try:
                    db.update_remediation_status(incident_id, remediation["status"])
                except Exception as exc:
                    print(f"[daemon] Error updating remediation status: {exc}")
            continue

        pod = anomaly["pod_name"]
        ns = anomaly["namespace"]

        # Collect signals
        try:
            logs = collector.get_pod_logs(pod, ns)
        except Exception as exc:
            print(f"[daemon] Error getting logs for {pod}: {exc}")
            logs = ""

        try:
            events = collector.get_kubernetes_events(pod, ns)
        except Exception as exc:
            print(f"[daemon] Error getting events for {pod}: {exc}")
            events = []

        incident = {
            **anomaly,
            "recent_logs": logs,
            "kubernetes_events": events,
        }

        # Diagnose via LLM
        try:
            diagnosis = llm_client.diagnose_incident(incident)
        except Exception as exc:
            print(f"[daemon] Error diagnosing {pod}: {exc}")
            continue

        # Report
        try:
            reporter.print_incident_report(incident, diagnosis)
        except Exception as exc:
            print(f"[daemon] Error printing report for {pod}: {exc}")

        incident_id = None
        try:
            incident_id = reporter.save_incident_report(incident, diagnosis)
        except Exception as exc:
            print(f"[daemon] Error saving report for {pod}: {exc}")

        try:
            remediation = remediator.remediate(incident, diagnosis)
        except Exception as exc:
            print(f"[daemon] Error remediating {incident.get('pod_name')}: {exc}")
            remediation = None

        if incident_id is not None and remediation is not None:
            try:
                db.update_remediation_status(incident_id, remediation["status"])
            except Exception as exc:
                print(f"[daemon] Error updating remediation status: {exc}")

    now = datetime.utcnow().isoformat() + "Z"
    print(f"[{now}] Checked cluster — "
          f"{len(restart_counts)} pods monitored, "
          f"{len(anomalies)} anomalies found")


async def main():
    """Daemon main loop."""
    _startup_checks()
    db.init_db()
    print("Daemon started. Press Ctrl+C to stop.\n")

    try:
        while True:
            await _poll_cycle()
            await asyncio.sleep(config.POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nDaemon stopped.")


if __name__ == "__main__":
    asyncio.run(main())