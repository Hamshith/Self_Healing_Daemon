# eval/scenarios.py — Fault scenario registry for the evaluation harness.
#
# Contains BOTH the 7 known fault scenarios (in-rulebook) and the 3 novel
# fault scenarios (outside the detector's rulebook).
#
# Novel faults use synthetic_logs and synthetic_events because the daemon's
# detector won't classify them — the harness directly feeds these signals
# to each method, mirroring what the daemon would collect from the pod.

from __future__ import annotations
from eval.types import ScenarioSpec


# ── Known faults — covered by the detector's built-in rules ──────────────
KNOWN_SCENARIOS: list[ScenarioSpec] = [
    ScenarioSpec(
        name="ApplicationCrash_CrashLoop",
        ground_truth_category="ApplicationCrash",
        is_novel=False,
        k8s_manifest="k8s/broken-deployment.yaml",
        detector_fault_type="ApplicationCrash",
        description="busybox exits with code 1 on every start → CrashLoopBackOff",
    ),
    ScenarioSpec(
        name="OOMKilled",
        ground_truth_category="OOMKilled",
        is_novel=False,
        k8s_manifest="k8s/oom-deployment.yaml",
        detector_fault_type="OOMKilled",
        description="Container allocates memory beyond the 100Mi limit → OOMKilled",
    ),
    ScenarioSpec(
        name="ImagePullError",
        ground_truth_category="ImagePullError",
        is_novel=False,
        k8s_manifest="k8s/imagepullerror-deployment.yaml",
        detector_fault_type="ImagePullError",
        description="Non-existent image tag → ImagePullBackOff",
    ),
    ScenarioSpec(
        name="ConfigError_MissingSecret",
        ground_truth_category="ConfigError",
        is_novel=False,
        k8s_manifest="k8s/missingsecret-deployment.yaml",
        detector_fault_type="ConfigError",
        description="Pod references a Secret that does not exist → CreateContainerConfigError",
    ),
    ScenarioSpec(
        name="ConfigError_MissingConfigMap",
        ground_truth_category="ConfigError",
        is_novel=False,
        k8s_manifest="k8s/configerror-deployment.yaml",
        detector_fault_type="ConfigError",
        description="Pod references a ConfigMap that does not exist → CreateContainerConfigError",
    ),
    ScenarioSpec(
        name="CPUThrottle",
        ground_truth_category="CPUThrottle",
        is_novel=False,
        k8s_manifest="k8s/cputhrottle-deployment.yaml",
        detector_fault_type="CPUThrottle",
        description="Tight CPU limit → container CPU usage pinned at limit",
    ),
    ScenarioSpec(
        name="NetworkLatency",
        ground_truth_category="NetworkLatency",
        is_novel=False,
        k8s_manifest="k8s/networklatency-creater.yaml",
        detector_fault_type="NetworkLatency",
        description="Chaos Mesh NetworkChaos CR injects 400ms delay",
    ),
]


# ── Novel faults — NOT covered by the detector's built-in rules ──────────
# For these the harness builds an incident dict directly from the synthetic
# logs/events fields rather than waiting for the detector.

NOVEL_SCENARIOS: list[ScenarioSpec] = [
    ScenarioSpec(
        name="DependencyFailure",
        ground_truth_category="DependencyFailure",
        is_novel=True,
        k8s_manifest="k8s/eval/dependency-failure-deployment.yaml",
        detector_fault_type=None,   # detector won't flag this
        description=(
            "App crashes because a downstream Redis service is unreachable. "
            "The log says 'Connection refused' not 'OOMKilled', so the rule "
            "table maps this to ApplicationCrash at best, not DependencyFailure."
        ),
        synthetic_logs=(
            "INFO  Starting application\n"
            "INFO  Connecting to redis://redis-service:6379\n"
            "ERROR redis.exceptions.ConnectionError: Error 111 connecting to "
            "redis-service:6379. Connection refused.\n"
            "ERROR Failed to initialize cache layer — aborting startup\n"
            "FATAL Unhandled exception during boot: ConnectionRefused\n"
            "Traceback (most recent call last):\n"
            "  File 'app.py', line 42, in connect_redis\n"
            "    self.redis = Redis(host='redis-service', port=6379)\n"
            "redis.exceptions.ConnectionError: Error 111 connecting to "
            "redis-service:6379. Connection refused."
        ),
        synthetic_events=[
            {
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "timestamp": "2026-09-28T18:00:00Z",
                "type": "Warning",
            }
        ],
    ),
    ScenarioSpec(
        name="ReadinessFailure",
        ground_truth_category="ReadinessFailure",
        is_novel=True,
        k8s_manifest="k8s/eval/readiness-failure-deployment.yaml",
        detector_fault_type=None,
        description=(
            "Pod is Running but the readiness probe fails because the app's "
            "/health endpoint returns HTTP 503.  The pod is never added to "
            "Service endpoints so traffic drops silently.  Detector sees 0 "
            "restarts and Running phase — no rule fires."
        ),
        synthetic_logs=(
            "INFO  Server started on :8080\n"
            "INFO  GET /health → 503 (database not ready)\n"
            "WARN  Primary DB connection pool exhausted: 0 connections available\n"
            "INFO  GET /health → 503\n"
            "INFO  GET /health → 503\n"
            "WARN  Readiness check failing: DB pool exhausted\n"
            "INFO  GET /api/v1/users → 500 Internal Server Error\n"
            "WARN  All in-flight requests failing due to missing DB connection"
        ),
        synthetic_events=[
            {
                "reason": "Unhealthy",
                "message": (
                    "Readiness probe failed: HTTP probe failed with statuscode: 503"
                ),
                "timestamp": "2026-09-28T18:00:05Z",
                "type": "Warning",
            }
        ],
    ),
    ScenarioSpec(
        name="ConfigEnvError",
        ground_truth_category="ConfigEnvError",
        is_novel=True,
        k8s_manifest="k8s/eval/config-env-error-deployment.yaml",
        detector_fault_type=None,
        description=(
            "Container starts successfully (no CreateContainerConfigError — "
            "the env var IS present, just has the wrong value) but crashes "
            "immediately because DATABASE_URL points to a non-existent host. "
            "Logs clearly show the bad value; pod status shows CrashLoopBackOff "
            "so the rule/ML baselines classify it as ApplicationCrash, not "
            "the more specific ConfigEnvError that logs reveal."
        ),
        synthetic_logs=(
            "INFO  Loading configuration\n"
            "INFO  DATABASE_URL=postgres://db-wrong-host:5432/myapp\n"
            "ERROR psycopg2.OperationalError: could not translate host name "
            "\"db-wrong-host\" to address: Name or service not known\n"
            "ERROR Failed to connect to database at postgres://db-wrong-host:5432/myapp\n"
            "FATAL Application startup failed: database connection refused\n"
            "INFO  Exiting with code 1"
        ),
        synthetic_events=[
            {
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "timestamp": "2026-09-28T18:00:10Z",
                "type": "Warning",
            },
            {
                "reason": "Started",
                "message": "Started container app",
                "timestamp": "2026-09-28T18:00:09Z",
                "type": "Normal",
            },
        ],
    ),
]

ALL_SCENARIOS: list[ScenarioSpec] = KNOWN_SCENARIOS + NOVEL_SCENARIOS
