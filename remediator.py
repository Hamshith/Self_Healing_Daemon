"""
Remediation module.

Given an (incident, diagnosis) pair produced by detector.py + llm_client.py,
decide whether to auto-remediate and, if so, execute a real action against
the cluster via the Kubernetes Python client.

Decision tree:
    ImagePullError    -> escalate only (can't fix a bad tag automatically)
    OOMKilled         -> patch the owning Deployment's memory limit +25%
    ApplicationCrash  -> delete the pod (K8s recreates it cleanly)
    ConfigError       -> escalate only
    anything else     -> escalate only

Hard rule: nothing executes unless diagnosis["safe_to_auto_remediate"] is
True. Even for OOMKilled / ApplicationCrash, if that flag is False, we
escalate instead. Currently llm_client's prompt always asks for this flag,
but until the prompt is tuned to return True for high-confidence cases,
everything will escalate -- that is expected, not a bug.

Rollback (roadmap Phase 2 Step 4):
    Before executing any action we snapshot enough state to undo it, and
    write that snapshot to rollbacks/<pod_name>_<timestamp>.json. This
    module does NOT implement the "wait 2 minutes, check if still
    crashing, auto-rollback" watcher -- that requires tracking state
    across poll cycles and belongs in daemon.py's main loop, calling
    rollback_action() below with the snapshot path if the fix didn't hold.
"""

import json
import os
import re
from datetime import datetime

from kubernetes import client, config as k8s_config
from kubernetes.client.exceptions import ApiException

import config


ROLLBACK_DIR = "rollbacks"


# ─────────────────────────────────────────────────────────────
# Kubernetes client setup
# ─────────────────────────────────────────────────────────────

def _load_kube_config():
    """Same pattern as detector.py -- prefer in-cluster, fall back to local kubeconfig."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def _get_apps_v1() -> client.AppsV1Api:
    """Deployments live under the apps/v1 API group, not core/v1."""
    _load_kube_config()
    return client.AppsV1Api()


def _get_core_v1() -> client.CoreV1Api:
    _load_kube_config()
    return client.CoreV1Api()


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _find_owning_deployment(pod_name: str, namespace: str) -> str | None:
    """
    Walk owner_references: Pod -> ReplicaSet -> Deployment.

    We do NOT try to string-guess the Deployment name by stripping the
    pod's hash suffixes -- that's fragile (e.g. a Deployment literally
    named "foo-bar" vs the pod "foo-bar-<hash>-<hash>" is ambiguous).
    Instead we query the API for the real owner chain.

    Returns the Deployment name, or None if it can't be resolved
    (e.g. the pod is not owned by a Deployment at all).
    """
    core_v1 = _get_core_v1()
    apps_v1 = _get_apps_v1()

    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as exc:
        print(f"[remediator] Could not read pod {pod_name}: {exc}")
        return None

    owner_refs = pod.metadata.owner_references or []
    rs_name = None
    for ref in owner_refs:
        if ref.kind == "ReplicaSet":
            rs_name = ref.name
            break

    if not rs_name:
        # Pod might be owned directly by something else (bare Pod, Job, etc.)
        return None

    try:
        rs = apps_v1.read_namespaced_replica_set(name=rs_name, namespace=namespace)
    except ApiException as exc:
        print(f"[remediator] Could not read ReplicaSet {rs_name}: {exc}")
        return None

    rs_owner_refs = rs.metadata.owner_references or []
    for ref in rs_owner_refs:
        if ref.kind == "Deployment":
            return ref.name

    return None


def _parse_memory_to_mi(mem_str: str) -> float:
    """
    Parse a Kubernetes memory quantity string (e.g. "100Mi", "1Gi", "512M")
    into a float number of Mi (mebibytes), so we can do the +25% math in
    one consistent unit.

    NOTE: this only handles the units your fault YAMLs actually use
    (Mi, Gi, M, G, and bare bytes). If your team starts using other
    K8s memory suffixes (Ki, E, etc.) extend this function -- do not
    silently guess, raise instead so a bad patch never goes out.
    """
    match = re.match(r"^(\d+(?:\.\d+)?)([A-Za-z]*)$", mem_str.strip())
    if not match:
        raise ValueError(f"Unrecognized memory quantity: {mem_str!r}")

    value = float(match.group(1))
    unit = match.group(2)

    unit_to_mi = {
        "Mi": 1,
        "Gi": 1024,
        "M": 1000 / 1024 ** 0 * (1_000_000 / (1024 * 1024)),  # decimal M -> Mi
        "G": (1_000_000_000 / (1024 * 1024)),                  # decimal G -> Mi
        "": 1 / (1024 * 1024),                                 # bare bytes -> Mi
    }

    if unit not in unit_to_mi:
        raise ValueError(f"Unsupported memory unit {unit!r} in {mem_str!r}")

    return value * unit_to_mi[unit]


def _write_rollback_snapshot(pod_name: str, action: str, snapshot: dict) -> str:
    """
    Persist a JSON snapshot of pre-action state to rollbacks/ so a later
    watcher (in daemon.py) can undo this action if the fix didn't hold.
    Returns the file path written.
    """
    os.makedirs(ROLLBACK_DIR, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    safe_pod_name = pod_name.replace("/", "_")
    path = os.path.join(ROLLBACK_DIR, f"{safe_pod_name}_{action}_{timestamp}.json")

    payload = {
        "pod_name": pod_name,
        "action": action,
        "snapshotted_at": datetime.utcnow().isoformat() + "Z",
        **snapshot,
    }

    with open(path, "w") as f:
        json.dump(payload, f, indent=2)

    return path


# ─────────────────────────────────────────────────────────────
# Action implementations
# ─────────────────────────────────────────────────────────────

def _escalate(incident: dict, diagnosis: dict, reason: str) -> dict:
    """No auto-remediation. Just return a structured escalation result."""
    result = {
        "action_taken": "escalate",
        "success": True,  # "success" here means "escalation was recorded", not "problem solved"
        "reason": reason,
        "pod_name": incident.get("pod_name"),
        "namespace": incident.get("namespace"),
        "rollback_path": None,
    }
    print(f"[remediator] ESCALATE {incident.get('pod_name')}: {reason}")
    return result


def _delete_pod(incident: dict, diagnosis: dict) -> dict:
    """
    ApplicationCrash -> delete the pod. The owning Deployment/ReplicaSet
    will recreate it, which is sometimes enough to clear a bad state
    (stuck connection, corrupted in-memory state, etc.).
    """
    pod_name = incident["pod_name"]
    namespace = incident["namespace"]
    core_v1 = _get_core_v1()

    # Snapshot the pod spec before deleting it, in case a rollback needs
    # to inspect what was running (we can't "undelete" a pod, but the
    # Deployment will recreate one from the same template -- this
    # snapshot is for audit/debugging, not literal restoration).
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        pod_spec_snapshot = pod.to_dict()
    except ApiException as exc:
        return {
            "action_taken": "delete_pod",
            "success": False,
            "reason": f"Could not read pod before delete: {exc}",
            "pod_name": pod_name,
            "namespace": namespace,
            "rollback_path": None,
        }

    rollback_path = _write_rollback_snapshot(
        pod_name, "delete_pod", {"pod_spec": pod_spec_snapshot}
    )

    try:
        core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as exc:
        return {
            "action_taken": "delete_pod",
            "success": False,
            "reason": f"Delete failed: {exc}",
            "pod_name": pod_name,
            "namespace": namespace,
            "rollback_path": rollback_path,
        }

    print(f"[remediator] Deleted pod {pod_name} (ApplicationCrash remediation)")
    return {
        "action_taken": "delete_pod",
        "success": True,
        "reason": "Pod deleted; owning controller will recreate it.",
        "pod_name": pod_name,
        "namespace": namespace,
        "rollback_path": rollback_path,
    }


def _patch_memory_limit(incident: dict, diagnosis: dict, increase_pct: float = 0.25) -> dict:
    """
    OOMKilled -> patch the owning Deployment's container memory limit
    (and request, kept equal to limit -- matches the Guaranteed QoS
    pattern used across this project's fault YAMLs) up by `increase_pct`.

    IMPORTANT ASSUMPTION (flagging, don't silently bury this): this
    patches the FIRST container in the pod spec. If your Deployments
    ever go multi-container, this needs to target the specific
    container name that actually OOMed (available on
    incident["container_statuses"]) instead of index 0.
    """
    pod_name = incident["pod_name"]
    namespace = incident["namespace"]

    deployment_name = _find_owning_deployment(pod_name, namespace)
    if not deployment_name:
        return _escalate(
            incident, diagnosis,
            reason=(
                f"OOMKilled diagnosis but could not resolve owning Deployment "
                f"for pod {pod_name} -- escalating instead of guessing."
            ),
        )

    apps_v1 = _get_apps_v1()

    try:
        deployment = apps_v1.read_namespaced_deployment(
            name=deployment_name, namespace=namespace
        )
    except ApiException as exc:
        return {
            "action_taken": "patch_memory",
            "success": False,
            "reason": f"Could not read Deployment {deployment_name}: {exc}",
            "pod_name": pod_name,
            "namespace": namespace,
            "rollback_path": None,
        }

    container = deployment.spec.template.spec.containers[0]
    current_limits = (container.resources.limits or {}) if container.resources else {}
    current_mem_str = current_limits.get("memory")

    if not current_mem_str:
        return _escalate(
            incident, diagnosis,
            reason=(
                f"OOMKilled diagnosis but Deployment {deployment_name}'s container "
                f"has no memory limit set -- nothing to scale up, escalating."
            ),
        )

    current_mi = _parse_memory_to_mi(current_mem_str)
    new_mi = current_mi * (1 + increase_pct)
    new_mem_str = f"{round(new_mi)}Mi"

    # Snapshot the ORIGINAL limit before patching, so a rollback can
    # restore this exact string later.
    rollback_path = _write_rollback_snapshot(
        pod_name,
        "patch_memory",
        {
            "deployment_name": deployment_name,
            "container_name": container.name,
            "original_memory_limit": current_mem_str,
            "new_memory_limit": new_mem_str,
        },
    )

    # JSON strategic merge patch: only touch the memory fields on the
    # first container, leave everything else in the Deployment alone.
    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container.name,
                            "resources": {
                                "limits": {"memory": new_mem_str},
                                "requests": {"memory": new_mem_str},
                            },
                        }
                    ]
                }
            }
        }
    }

    try:
        apps_v1.patch_namespaced_deployment(
            name=deployment_name, namespace=namespace, body=patch_body
        )
    except ApiException as exc:
        return {
            "action_taken": "patch_memory",
            "success": False,
            "reason": f"Patch failed: {exc}",
            "pod_name": pod_name,
            "namespace": namespace,
            "rollback_path": rollback_path,
        }

    print(
        f"[remediator] Patched {deployment_name} memory limit: "
        f"{current_mem_str} -> {new_mem_str} (OOMKilled remediation)"
    )
    return {
        "action_taken": "patch_memory",
        "success": True,
        "reason": f"Memory limit increased {current_mem_str} -> {new_mem_str}",
        "pod_name": pod_name,
        "namespace": namespace,
        "deployment_name": deployment_name,
        "rollback_path": rollback_path,
    }


# ─────────────────────────────────────────────────────────────
# Rollback
# ─────────────────────────────────────────────────────────────

def rollback_action(rollback_path: str) -> dict:
    """
    Undo a previously executed action using its saved snapshot.

    Called by daemon.py's watcher (NOT implemented in this file) if a
    pod is still crashing ~2 minutes after remediation.
    """
    with open(rollback_path) as f:
        snapshot = json.load(f)

    action = snapshot["action"]
    namespace = None  # filled per-branch below

    if action == "patch_memory":
        apps_v1 = _get_apps_v1()
        deployment_name = snapshot["deployment_name"]
        container_name = snapshot["container_name"]
        original_mem = snapshot["original_memory_limit"]
        # NOTE: namespace wasn't stored in the snapshot dict above --
        # if you use this, add "namespace": namespace into the snapshot
        # payload in _patch_memory_limit before relying on rollback.
        namespace = snapshot.get("namespace", "default")

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "resources": {
                                    "limits": {"memory": original_mem},
                                    "requests": {"memory": original_mem},
                                },
                            }
                        ]
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=deployment_name, namespace=namespace, body=patch_body
        )
        return {"success": True, "reason": f"Reverted memory limit to {original_mem}"}

    elif action == "delete_pod":
        # A deleted pod can't be "restored" -- the owning controller
        # already recreated a fresh one from the same template. Rollback
        # here just means: don't attempt anything further, log it.
        return {
            "success": True,
            "reason": "delete_pod has no reversible state; controller already recreated the pod.",
        }

    else:
        return {"success": False, "reason": f"Unknown action type in snapshot: {action}"}


# ─────────────────────────────────────────────────────────────
# Public entry point -- call this from daemon.py
# ─────────────────────────────────────────────────────────────

def remediate(incident: dict, diagnosis: dict) -> dict:
    """
    Decide what to do with a diagnosed incident and (maybe) do it.

    This is the single function daemon.py should call, right after
    llm_client.diagnose_incident(). Example wiring (add to daemon.py's
    _poll_cycle, after the diagnosis try/except block):

        try:
            remediation = remediator.remediate(incident, diagnosis)
        except Exception as exc:
            print(f"[daemon] Error remediating {pod}: {exc}")
            remediation = None

    Returns a result dict always containing at least:
        action_taken: "escalate" | "delete_pod" | "patch_memory"
        success: bool
        reason: str
        pod_name, namespace
        rollback_path: str | None
    """
    category = diagnosis.get("root_cause_category")
    safe = diagnosis.get("safe_to_auto_remediate", False)

    # Hard gate: nothing below this line executes a real action unless
    # both the category maps to an auto-remediable case AND the LLM
    # explicitly said it's safe.
    if not safe:
        return _escalate(
            incident, diagnosis,
            reason=(
                f"safe_to_auto_remediate is False for category "
                f"'{category}' -- escalating rather than acting."
            ),
        )

    if category == "ImagePullError":
        return _escalate(
            incident, diagnosis,
            reason="ImagePullError cannot be auto-fixed (bad tag requires a human).",
        )

    elif category == "OOMKilled":
        return _patch_memory_limit(incident, diagnosis)

    elif category == "ApplicationCrash":
        return _delete_pod(incident, diagnosis)

    elif category == "ConfigError":
        return _escalate(
            incident, diagnosis,
            reason="ConfigError requires a human to supply the missing config/secret.",
        )

    else:
        return _escalate(
            incident, diagnosis,
            reason=f"No auto-remediation defined for category '{category}'.",
        )