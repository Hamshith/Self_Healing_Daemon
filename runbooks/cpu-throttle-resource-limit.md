# CPU throttling: restrictive limit

CPU throttling occurs when a container repeatedly reaches its configured CPU limit. Look at container CPU usage and throttled seconds, then compare the limit with request, workload concurrency, and latency. A busy loop, inefficient query, or insufficient limit can all present as slow service behavior; inspect logs and profiles before changing resources.

Raise the CPU limit and request in a controlled rollout when node capacity exists, or optimize the hot path and cap concurrency. Kubernetes will continue scheduling the pod, so readiness alone does not prove the issue is fixed. Verify throttling and latency metrics after the rollout.

Apply/remove: apply `k8s/cputhrottle-deployment.yaml`; remove it with `kubectl delete -f k8s/cputhrottle-deployment.yaml`.

## Remediation steps
1. Confirm throttling metrics and compare CPU request, limit, concurrency, and latency.
2. Return `escalate` with the measured limit change or code optimization required; do not guess CPU values.
3. Verify throttling and latency after the approved rollout.

Mild throttling on a spare workload is usually recoverable, while sustained throttling that causes queue growth, missed objectives, or broad latency increases needs prompt capacity or code intervention.
