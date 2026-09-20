"""
Signal collection module.
Gathers pod restart counts (Prometheus with K8s fallback),
pod logs, and Kubernetes events for anomalous pods.
"""

from datetime import datetime, timedelta
from kubernetes import client, config as k8s_config
from prometheus_api_client import PrometheusConnect
import config


def _load_kube_config():
    """Load kubeconfig, preferring in-cluster then local."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def get_pod_restart_counts() -> dict:
    """
    Return {"namespace/pod_name": restart_count} for every pod in the cluster.

    Primary source: Prometheus  kube_pod_container_status_restarts_total
    Fallback:       Kubernetes  Python client (pod.status.container_statuses)
    """
    # ── Try Prometheus first ────────────────────────────
    try:
        prom = PrometheusConnect(url=config.PROMETHEUS_URL, disable_ssl=True)
        results = prom.custom_query("kube_pod_container_status_restarts_total")
        restart_counts: dict = {}
        for item in results:
            pod = item["metric"].get("pod", "unknown")
            namespace = item["metric"].get("namespace", "default")
            pod_key = f"{namespace}/{pod}"
            count = int(float(item["value"][1]))
            # Keep the highest count if a pod has multiple containers
            if pod_key not in restart_counts or count > restart_counts[pod_key]:
                restart_counts[pod_key] = count
        if restart_counts:
            return restart_counts
    except Exception:
        pass  # Fall through to K8s API fallback

    # ── Fallback: Kubernetes API ────────────────────────
    _load_kube_config()
    v1 = client.CoreV1Api()
    restart_counts = {}
    try:
        pods = v1.list_pod_for_all_namespaces(watch=False)
        for pod in pods.items:
            if pod.status.container_statuses:
                max_restarts = max(
                    cs.restart_count for cs in pod.status.container_statuses
                )
                pod_key = f"{pod.metadata.namespace}/{pod.metadata.name}"
                restart_counts[pod_key] = max_restarts
            else:
                pod_key = f"{pod.metadata.namespace}/{pod.metadata.name}"
                restart_counts[pod_key] = 0
    except Exception as exc:
        print(f"[collector] Error fetching restart counts from K8s API: {exc}")

    return restart_counts


def get_pod_logs(pod_name: str, namespace: str = "default") -> str:
    """
    Return the last LOG_LINES lines of logs from *pod_name*.
    Returns an empty string when logs are unavailable.
    """
    _load_kube_config()
    v1 = client.CoreV1Api()
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=config.LOG_LINES,
        )
        return logs or ""
    except Exception:
        return ""


def get_kubernetes_events(pod_name: str, namespace: str = "default") -> list:
    """
    Return Warning-type events related to *pod_name* from the last 10 minutes.

    Each event is a dict with keys: reason, message, timestamp, type.
    """
    _load_kube_config()
    v1 = client.CoreV1Api()
    ten_minutes_ago = datetime.utcnow() - timedelta(minutes=10)

    events_out: list = []
    try:
        field_selector = f"involvedObject.name={pod_name}"
        events = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=field_selector,
        )
        for ev in events.items:
            # Use last_timestamp, fall back to event_time or first_timestamp
            ts = ev.last_timestamp or ev.event_time or ev.first_timestamp
            if ts is None:
                continue
            # ts may be a datetime object already
            if isinstance(ts, datetime):
                event_time = ts
            else:
                event_time = datetime.fromisoformat(str(ts))

            # Only events from last 10 minutes
            if event_time.replace(tzinfo=None) < ten_minutes_ago:
                continue

            # Only Warning events
            if ev.type != "Warning":
                continue

            events_out.append(
                {
                    "reason": ev.reason,
                    "message": ev.message,
                    "timestamp": event_time.isoformat() + "Z"
                    if not str(event_time).endswith("Z")
                    else event_time.isoformat(),
                    "type": ev.type,
                }
            )
    except Exception as exc:
        print(f"[collector] Error fetching K8s events for {pod_name}: {exc}")

    return events_out
