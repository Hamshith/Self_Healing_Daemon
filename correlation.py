"""Best-effort correlation of nearby pod failures through Kubernetes Services."""

from __future__ import annotations

from datetime import datetime, timezone

from kubernetes import client, config as k8s_config

CORRELATION_WINDOW_SECONDS = 120
_recent_failures: dict[str, dict] = {}


def _load_kube_config():
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def _matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def _service_members(v1) -> dict[str, set[str]]:
    services = v1.list_service_for_all_namespaces(watch=False).items
    pods = v1.list_pod_for_all_namespaces(watch=False).items
    members: dict[str, set[str]] = {}
    for service in services:
        selector = service.spec.selector or {}
        if not selector:
            continue
        service_key = f"{service.metadata.namespace}/{service.metadata.name}"
        members[service_key] = {
            f"{pod.metadata.namespace}/{pod.metadata.name}"
            for pod in pods
            if pod.metadata.namespace == service.metadata.namespace
            and _matches(selector, pod.metadata.labels or {})
        }
    return members


def correlate(anomalies: list[dict]) -> list[dict]:
    """Attach recent service-neighborhood context to newly detected anomalies.

    This is intentionally best-effort: Kubernetes Services expose which pods
    belong to a workload, but not application-level request direction. The
    context therefore describes shared service neighborhoods and timing rather
    than claiming a causal dependency that Kubernetes cannot prove.
    """
    if not anomalies:
        return anomalies

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - CORRELATION_WINDOW_SECONDS
    for key, value in list(_recent_failures.items()):
        if value["timestamp"].timestamp() < cutoff:
            del _recent_failures[key]

    try:
        _load_kube_config()
        service_members = _service_members(client.CoreV1Api())
    except Exception as exc:
        print(f"[correlation] Service topology unavailable: {exc}")
        service_members = {}

    pod_services: dict[str, list[str]] = {}
    for service_name, members in service_members.items():
        for pod_key in members:
            pod_services.setdefault(pod_key, []).append(service_name)

    for anomaly in anomalies:
        pod_name = anomaly.get("pod_name")
        namespace = anomaly.get("namespace", "default")
        if not pod_name:
            continue
        pod_key = f"{namespace}/{pod_name}"
        detected_at = anomaly.get("detected_at")
        try:
            timestamp = datetime.fromisoformat(str(detected_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            timestamp = now
        _recent_failures[pod_key] = {
            "timestamp": timestamp,
            "fault_type": anomaly.get("fault_type", "Unknown"),
            "services": pod_services.get(pod_key, []),
        }

        related = []
        for other_key, failure in _recent_failures.items():
            if other_key == pod_key or failure["timestamp"].timestamp() < cutoff:
                continue
            shared_services = sorted(
                set(pod_services.get(pod_key, [])) & set(failure.get("services", []))
            )
            same_namespace = other_key.split("/", 1)[0] == namespace
            if same_namespace and (shared_services or not pod_services.get(pod_key)):
                related.append({
                    "pod": other_key,
                    "fault_type": failure["fault_type"],
                    "services": shared_services or failure.get("services", []),
                    "detected_at": failure["timestamp"].isoformat(),
                })

        if related:
            anomaly["correlation_context"] = {
                "window_seconds": CORRELATION_WINDOW_SECONDS,
                "related_failures": related,
                "note": "Nearby failures share a namespace or Service neighborhood; directionality requires application traces.",
            }

    return anomalies
