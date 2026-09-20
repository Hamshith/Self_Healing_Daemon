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

    for anomaly in anomalies:
        # CPUThrottle/ApplicationCrash/etc. are keyed by pod_name+namespace.
        # NetworkLatency is cluster-wide (a Chaos Mesh object, not a pod),
        # so it has no pod_name — handle it separately and skip the
        # per-pod log/event collection below.
        if anomaly["fault_type"] == "NetworkLatency":
            try:
                reporter.print_incident_report(anomaly, {"root_cause_category": "NetworkLatency"})
            except Exception as exc:
                print(f"[daemon] Error printing NetworkLatency report: {exc}")
            try:
                reporter.save_incident_report(anomaly, {"root_cause_category": "NetworkLatency"})
            except Exception as exc:
                print(f"[daemon] Error saving NetworkLatency report: {exc}")
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

        try:
            reporter.save_incident_report(incident, diagnosis)
        except Exception as exc:
            print(f"[daemon] Error saving report for {pod}: {exc}")

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