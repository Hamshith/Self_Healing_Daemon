"""
Anomaly detection module.
Identifies pods in CrashLoopBackOff state that exceed the
configured restart threshold and have not already been flagged.
"""

from datetime import datetime
from kubernetes import client, config as k8s_config
import config


# ── State tracking between poll cycles ──────────────────
# Maps pod_name -> restart_count at the time the pod was flagged.
# If a pod's restart count drops back to 0 (was fixed), the entry
# is removed so a *new* incident will fire if it breaks again.
_previously_flagged: dict = {}


def _load_kube_config():
    """Load kubeconfig, preferring in-cluster then local."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def detect_anomalies(restart_counts: dict) -> list:
    """
    Given *restart_counts* ({pod_name: int}), cross-reference with
    live pod status to find pods in CrashLoopBackOff.

    Returns a list of anomaly dicts for pods that:
      1. Have restart_count >= RESTART_THRESHOLD
      2. Are in CrashLoopBackOff (or Waiting/Error) state
      3. Were NOT already flagged in a previous cycle
         (unless their restart count dropped to 0 and climbed again)
    """
    global _previously_flagged

    _load_kube_config()
    v1 = client.CoreV1Api()

    anomalies: list = []

    # ── Clean up pods that have been fixed (restart count reset) ──
    for pod_name in list(_previously_flagged.keys()):
        current = restart_counts.get(pod_name, 0)
        if current == 0:
            del _previously_flagged[pod_name]

    # ── Scan all pods ──────────────────────────────────────────────
    try:
        pods = v1.list_pod_for_all_namespaces(watch=False)
    except Exception as exc:
        print(f"[detector] Error listing pods: {exc}")
        return anomalies

    for pod in pods.items:
        pod_name = pod.metadata.name
        namespace = pod.metadata.namespace
        restart_count = restart_counts.get(pod_name, 0)

        # Skip pods below threshold
        if restart_count < config.RESTART_THRESHOLD:
            continue

        # ── Check container statuses for CrashLoopBackOff ──
        container_statuses_raw = pod.status.container_statuses or []
        is_crash_loop = False
        statuses_info: list = []

        for cs in container_statuses_raw:
            status_detail = {
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "state": None,
            }

            if cs.state:
                if cs.state.waiting:
                    status_detail["state"] = {
                        "waiting": {
                            "reason": cs.state.waiting.reason,
                            "message": cs.state.waiting.message,
                        }
                    }
                    if cs.state.waiting.reason == "CrashLoopBackOff":
                        is_crash_loop = True
                elif cs.state.terminated:
                    status_detail["state"] = {
                        "terminated": {
                            "reason": cs.state.terminated.reason,
                            "exit_code": cs.state.terminated.exit_code,
                        }
                    }
                    # Terminated with non-zero also counts
                    if cs.state.terminated.exit_code != 0:
                        is_crash_loop = True
                elif cs.state.running:
                    status_detail["state"] = {"running": True}

            statuses_info.append(status_detail)

        if not is_crash_loop:
            continue

        # ── Dedup: skip if already flagged with same restart count ──
        if pod_name in _previously_flagged:
            if _previously_flagged[pod_name] == restart_count:
                continue

        # Record that we've flagged this pod at this restart count
        _previously_flagged[pod_name] = restart_count

        anomalies.append(
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "restart_count": restart_count,
                "container_statuses": statuses_info,
                "detected_at": datetime.utcnow().isoformat() + "Z",
            }
        )

    return anomalies
