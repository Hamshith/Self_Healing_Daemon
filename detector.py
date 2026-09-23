"""
Anomaly detection module.
Identifies pods exhibiting any of 7 fault types:
  1. ApplicationCrash    - CrashLoopBackOff / non-zero exit
  2. OOMKilled            - container terminated with OOMKilled reason
  3. ImagePullError       - ImagePullBackOff / ErrImagePull
  4. ConfigError           - CreateContainerConfigError (missing secret/configmap)
  5. PendingScheduling    - pod stuck Pending > 5 minutes
  6. CPUThrottle           - container CPU usage pinned at its limit (via metrics-server)
    7. NetworkLatency        - measured in-cluster HTTP latency exceeds the threshold

Faults 1-5 come from pod container status and are cheap to check on
every poll. Faults 6-7 need different data sources (the metrics API
and the Chaos Mesh CRD), so they live in a separate function that
daemon.py should call alongside detect_anomalies().
"""

import time
import uuid
from datetime import datetime, timezone
from kubernetes import client, config as k8s_config
from kubernetes.stream import stream
import config

PENDING_THRESHOLD_SECONDS = 5 * 60      # 5 minutes
CPU_THROTTLE_CONSECUTIVE_POLLS = 3       # how many polls in a row at-limit before flagging
CPU_THROTTLE_RATIO = 0.95                # usage/limit ratio considered "throttled"

# ── State tracking between poll cycles ──────────────────
_previously_flagged: dict = {}        # "namespace/pod" -> (fault_type, restart_count)
_cpu_throttle_streak: dict = {}       # "namespace/pod/container" -> consecutive at-limit count
_previously_flagged_cpu: set = set()


def _load_kube_config():
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def _pod_key(namespace: str, pod_name: str) -> str:
    return f"{namespace}/{pod_name}"


# ─────────────────────────────────────────────────────────────────
# Faults 1-5: pod container status
# ─────────────────────────────────────────────────────────────────

def _classify_container_status(cs) -> tuple:
    """Return (fault_type, detail_dict) or (None, None)."""
    if not cs.state:
        return None, None

    if cs.state.waiting:
        reason = cs.state.waiting.reason
        detail = {"waiting": {"reason": reason, "message": cs.state.waiting.message}}

        if reason == "CrashLoopBackOff":
            return "ApplicationCrash", detail
        if reason in ("ImagePullBackOff", "ErrImagePull"):
            return "ImagePullError", detail
        if reason == "CreateContainerConfigError":
            return "ConfigError", detail

    elif cs.state.terminated:
        reason = cs.state.terminated.reason
        detail = {
            "terminated": {"reason": reason, "exit_code": cs.state.terminated.exit_code}
        }
        if reason == "OOMKilled":
            return "OOMKilled", detail
        if cs.state.terminated.exit_code != 0:
            return "ApplicationCrash", detail

    # Kubernetes usually restarts an OOM-killed container before the
    # next poll, so by the time we look, cs.state is back to Waiting
    # (e.g. CrashLoopBackOff) and the OOMKilled evidence has moved to
    # last_state.terminated. Without this check, OOMKilled would only
    # ever be caught in the narrow window before the restart happens.
    if cs.last_state and cs.last_state.terminated:
        if cs.last_state.terminated.reason == "OOMKilled":
            return "OOMKilled", {
                "terminated": {
                    "reason": "OOMKilled",
                    "exit_code": cs.last_state.terminated.exit_code,
                    "source": "last_state",
                }
            }

    return None, None


def _check_pending(pod) -> bool:
    if pod.status.phase != "Pending":
        return False
    start_time = pod.metadata.creation_timestamp
    if start_time is None:
        return False
    age = (datetime.now(timezone.utc) - start_time).total_seconds()
    return age > PENDING_THRESHOLD_SECONDS


def detect_anomalies(restart_counts: dict) -> list:
    """
    Detects faults 1-5 (ApplicationCrash, OOMKilled, ImagePullError,
    ConfigError, PendingScheduling) from live pod status.
    """
    global _previously_flagged

    _load_kube_config()
    v1 = client.CoreV1Api()

    anomalies: list = []
    active_fault_keys = set()

    try:
        pods = v1.list_pod_for_all_namespaces(watch=False)
    except Exception as exc:
        print(f"[detector] Error listing pods: {exc}")
        return anomalies

    for pod in pods.items:
        pod_name = pod.metadata.name
        namespace = pod.metadata.namespace
        pod_key = _pod_key(namespace, pod_name)
        restart_count = restart_counts.get(
            pod_key, restart_counts.get(pod_name, 0)
        )

        fault_type = None
        statuses_info: list = []

        if _check_pending(pod):
            fault_type = "PendingScheduling"

        for cs in (pod.status.container_statuses or []):
            ft, detail = _classify_container_status(cs)
            statuses_info.append(
                {
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "state": detail,
                }
            )
            if ft in ("OOMKilled", "ImagePullError", "ConfigError"):
                fault_type = ft
            elif ft == "ApplicationCrash" and restart_count >= config.RESTART_THRESHOLD:
                fault_type = ft

        if fault_type is None:
            continue

        prev = _previously_flagged.get(pod_key)
        active_fault_keys.add(pod_key)
        if prev == (fault_type, restart_count):
            continue
        _previously_flagged[pod_key] = (fault_type, restart_count)

        anomalies.append(
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "fault_type": fault_type,
                "restart_count": restart_count,
                "container_statuses": statuses_info,
                "detected_at": datetime.utcnow().isoformat() + "Z",
            }
        )

    # Only retain suppression state for faults observed in this successful
    # listing. Restart counts alone cannot identify recovery: a recovered
    # pod may keep a non-zero count, while Pending pods commonly have zero.
    for pod_key in list(_previously_flagged):
        if pod_key not in active_fault_keys:
            del _previously_flagged[pod_key]

    return anomalies


# ─────────────────────────────────────────────────────────────────
# Fault 6: CPUThrottle  (needs metrics.k8s.io, i.e. metrics-server)
# ─────────────────────────────────────────────────────────────────

def _parse_cpu_to_millicores(value: str) -> float:
    """Convert a k8s CPU string ('50m', '0.5', '1') to millicores."""
    if value is None:
        return 0.0
    if value.endswith("n"):          # nanocores
        return float(value[:-1]) / 1_000_000
    if value.endswith("m"):          # millicores
        return float(value[:-1])
    return float(value) * 1000       # whole cores


def _detect_cpu_throttle(v1) -> list:
    """
    Compares live CPU usage (from metrics-server) against each
    container's configured CPU limit. If usage stays pinned at/above
    CPU_THROTTLE_RATIO of the limit for CPU_THROTTLE_CONSECUTIVE_POLLS
    polls in a row, flags it as CPUThrottle.

    Requires metrics-server to be installed in the cluster
    (`kubectl top pods` should work if it is).
    """
    global _cpu_throttle_streak, _previously_flagged_cpu

    anomalies = []
    custom_api = client.CustomObjectsApi()

    try:
        metrics = custom_api.list_cluster_custom_object(
            group="metrics.k8s.io", version="v1beta1", plural="pods"
        )
    except Exception as exc:
        print(f"[detector] metrics-server unavailable, skipping CPU throttle check: {exc}")
        return anomalies

    # Build a lookup of configured cpu limits per container from live pod specs
    try:
        pods = v1.list_pod_for_all_namespaces(watch=False)
    except Exception as exc:
        print(f"[detector] Error listing pods for CPU limits: {exc}")
        return anomalies

    limits_lookup = {}  # "namespace/pod/container" -> limit in millicores
    for pod in pods.items:
        for c in pod.spec.containers:
            limit = None
            if c.resources and c.resources.limits:
                limit = c.resources.limits.get("cpu")
            if limit:
                key = f"{pod.metadata.namespace}/{pod.metadata.name}/{c.name}"
                limits_lookup[key] = _parse_cpu_to_millicores(limit)

    seen_this_poll = set()

    for item in metrics.get("items", []):
        try:
            pod_name = item["metadata"]["name"]
            namespace = item["metadata"]["namespace"]
            for container in item.get("containers", []):
                c_name = container["name"]
                key = f"{namespace}/{pod_name}/{c_name}"
                limit_mc = limits_lookup.get(key)
                if limit_mc is None:
                    continue  # no CPU limit set, nothing to throttle against

                usage_mc = _parse_cpu_to_millicores(container["usage"].get("cpu"))
                ratio = usage_mc / limit_mc if limit_mc else 0

                if ratio >= CPU_THROTTLE_RATIO:
                    _cpu_throttle_streak[key] = _cpu_throttle_streak.get(key, 0) + 1
                else:
                    _cpu_throttle_streak[key] = 0
                    _previously_flagged_cpu.discard(key)

                seen_this_poll.add(key)

                if (
                    _cpu_throttle_streak[key] >= CPU_THROTTLE_CONSECUTIVE_POLLS
                    and key not in _previously_flagged_cpu
                ):
                    _previously_flagged_cpu.add(key)
                    anomalies.append(
                        {
                            "pod_name": pod_name,
                            "namespace": namespace,
                            "fault_type": "CPUThrottle",
                            "restart_count": 0,
                            "container_name": c_name,
                            "cpu_usage_millicores": usage_mc,
                            "cpu_limit_millicores": limit_mc,
                            "detected_at": datetime.utcnow().isoformat() + "Z",
                        }
                    )
        except Exception as exc:
            # One malformed metrics item must never take down detection
            # for every other pod, or (worse) abort before
            # _detect_network_latency() gets called this poll.
            print(f"[detector] Skipping malformed CPU metrics item: {exc}")
            continue

    # Prune entries we didn't see this poll entirely, rather than just
    # zeroing the streak — leaving zeroed keys around forever is a slow
    # memory leak over long-running pod churn.
    for key in list(_cpu_throttle_streak.keys()):
        if key not in seen_this_poll:
            del _cpu_throttle_streak[key]
            _previously_flagged_cpu.discard(key)

    return anomalies


# ─────────────────────────────────────────────────────────────────
# Fault 7: NetworkLatency  (Chaos Mesh status plus in-cluster HTTP probe)
# ─────────────────────────────────────────────────────────────────

def _measure_http_latency(v1, target_url: str, namespace: str = "default") -> float:
    """Measure an HTTP request from a temporary in-cluster curl pod."""
    pod_name = f"latency-probe-{uuid.uuid4().hex[:8]}"
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            labels={"app": "self-healing-latency-probe"},
        ),
        spec=client.V1PodSpec(
            restart_policy="Never",
            containers=[client.V1Container(
                name="curl",
                image=config.NETWORK_LATENCY_PROBE_IMAGE,
                command=["sh", "-c", "sleep 60"],
            )],
        ),
    )

    try:
        v1.create_namespaced_pod(namespace=namespace, body=pod)
        deadline = time.monotonic() + config.NETWORK_LATENCY_PROBE_TIMEOUT_SECONDS
        last_status = "Unknown"
        while time.monotonic() < deadline:
            current = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            last_status = current.status.phase or "Unknown"
            if current.status.phase == "Running":
                break
            if current.status.phase in ("Failed", "Succeeded"):
                reasons = []
                for container_status in current.status.container_statuses or []:
                    state = container_status.state
                    if state and state.waiting:
                        reasons.append(
                            f"{container_status.name}: {state.waiting.reason}"
                        )
                    elif state and state.terminated:
                        reasons.append(
                            f"{container_status.name}: {state.terminated.reason}"
                        )
                detail = ", ".join(reasons) or "no container reason"
                raise RuntimeError(
                    f"latency probe pod ended in {current.status.phase} ({detail})"
                )
            time.sleep(0.5)
        else:
            reasons = []
            for container_status in current.status.container_statuses or []:
                state = container_status.state
                if state and state.waiting:
                    reasons.append(f"{container_status.name}: {state.waiting.reason}")
            detail = ", ".join(reasons) or "check pod events"
            raise TimeoutError(
                f"latency probe pod did not become ready; phase={last_status}, {detail}"
            )

        output = stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=[
                "curl", "-sS", "-o", "/dev/null",
                "-w", "%{time_total}",
                "--max-time", str(config.NETWORK_LATENCY_PROBE_TIMEOUT_SECONDS),
                target_url,
            ],
            container="curl",
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        return float(output.strip()) * 1000
    finally:
        try:
            v1.delete_namespaced_pod(
                name=pod_name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )
        except Exception:
            pass

def _detect_network_latency(custom_api, v1=None) -> list:
    """
    Queries Chaos Mesh's NetworkChaos resources and measures the target
    service from a temporary in-cluster curl pod. A fault is reported
    only when Chaos Mesh is injecting delay and measured latency exceeds
    NETWORK_LATENCY_THRESHOLD_MS.

    This matches the repo's use case: a NetworkChaos resource targeting
    pods labeled `app: nginx` should be reported as NetworkLatency only
    when those nginx pods actually exist in the cluster.
    """
    anomalies = []

    if v1 is None:
        _load_kube_config()
        v1 = client.CoreV1Api()

    try:
        chaos_objs = custom_api.list_cluster_custom_object(
            group="chaos-mesh.org", version="v1alpha1", plural="networkchaos"
        )
    except Exception as exc:
        print(f"[detector] Chaos Mesh NetworkChaos CRD unavailable, skipping: {exc}")
        return anomalies

    try:
        pods = v1.list_pod_for_all_namespaces(watch=False)
    except Exception as exc:
        print(f"[detector] Error listing pods for NetworkLatency target match: {exc}")
        pods = None

    for item in chaos_objs.get("items", []):
        name = item["metadata"]["name"]
        namespace = item["metadata"]["namespace"]
        spec = item.get("spec", {})

        if spec.get("action") != "delay":
            continue

        key = f"{namespace}/{name}"

        selector = spec.get("selector", {})
        target_app = None
        selected_pods = []

        if pods is not None:
            label_selectors = selector.get("labelSelectors", {}) or {}
            target_namespaces = selector.get("namespaces") or [namespace]
            pod_names = selector.get("podNames") or []

            for pod in pods.items:
                metadata = pod.metadata
                labels = metadata.labels or {}
                if metadata.namespace not in target_namespaces:
                    continue
                if pod_names and metadata.name not in pod_names:
                    continue

                matches = True
                for label_name, expected_value in label_selectors.items():
                    actual = labels.get(label_name)
                    if isinstance(expected_value, list):
                        if actual not in expected_value:
                            matches = False
                            break
                    elif actual != expected_value:
                        matches = False
                        break

                if matches:
                    selected_pods.append(metadata.name)
                    if target_app is None and labels.get("app"):
                        target_app = labels["app"]

        status = item.get("status", {})
        conditions = status.get("conditions", [])
        experiment = status.get("experiment", {}) or {}
        observed_phase = (
            status.get("phase")
            or experiment.get("phase")
            or experiment.get("observedPhase")
        )

        is_injecting = any(
            c.get("type") == "AllInjected" and str(c.get("status")).lower() == "true"
            for c in conditions
        ) or observed_phase in ("Run", "Running")

        if is_injecting and (not selected_pods):
            # Keep the alarm only if the target app is actually present.
            # A CR that exists but selects no live pods is not a real app latency incident.
            is_injecting = False

        measured_latency_ms = None
        if is_injecting:
            try:
                measured_latency_ms = _measure_http_latency(
                    v1,
                    config.NETWORK_LATENCY_TARGET_URL,
                    namespace,
                )
                is_injecting = (
                    measured_latency_ms >= config.NETWORK_LATENCY_THRESHOLD_MS
                )
            except Exception as exc:
                print(f"[detector] Latency probe failed for {name}: {exc}")
                is_injecting = False

        if is_injecting:
            anomalies.append(
                {
                    "chaos_name": name,
                    "namespace": namespace,
                    "fault_type": "NetworkLatency",
                    "target_app": target_app,
                    "target_pods": selected_pods,
                    "target_selector": selector,
                    "target_url": config.NETWORK_LATENCY_TARGET_URL,
                    "measured_latency_ms": round(measured_latency_ms, 2),
                    "latency_threshold_ms": config.NETWORK_LATENCY_THRESHOLD_MS,
                    "detected_at": datetime.utcnow().isoformat() + "Z",
                }
            )

    return anomalies


def detect_resource_anomalies() -> list:
    """
    Detects faults 6-7 (CPUThrottle, NetworkLatency), which need the
    metrics API and the Chaos Mesh CRD rather than plain pod status.
    Call this alongside detect_anomalies() in daemon.py's poll loop.

    Fails soft: if metrics-server or Chaos Mesh aren't installed in
    the cluster, the relevant check is skipped (logged, not raised)
    rather than crashing the daemon.
    """
    _load_kube_config()
    v1 = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    anomalies = []
    anomalies.extend(_detect_cpu_throttle(v1))
    anomalies.extend(_detect_network_latency(custom_api, v1))
    return anomalies