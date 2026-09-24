# Stalled Deployment rollout

A Deployment rollout can stall when new pods cannot schedule, pull their image, start, or pass readiness. Inspect `kubectl rollout status deployment NAME`, ReplicaSet conditions, pod Events, and the Deployment's progress deadline. Compare desired, updated, available, and unavailable replica counts.

Fix the first blocking condition and roll forward, or use `kubectl rollout undo deployment NAME` when the prior revision is known healthy. Confirm the rollout completes and old replicas terminate safely. Record the failed revision and reason so the same image or configuration is not reintroduced.

Apply/remove: use the deployment fixtures under `k8s/`; remove each test resource with its manifest.

## Remediation steps
1. Inspect rollout status, ReplicaSet conditions, pod Events, and progress deadline.
2. Identify the first blocking condition such as scheduling, image pull, startup, or readiness.
3. Return `escalate` with the approved forward fix or rollback decision; do not roll back blindly.
4. Verify the rollout completes and old replicas terminate safely.

A stalled change with healthy old replicas has limited immediate impact; a rollout that removes capacity or blocks recovery of a critical service requires urgent rollback or repair.
