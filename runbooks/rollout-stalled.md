# Stalled Deployment rollout

A Deployment rollout can stall when new pods cannot schedule, pull their image, start, or pass readiness. Inspect `kubectl rollout status deployment NAME`, ReplicaSet conditions, pod Events, and the Deployment's progress deadline. Compare desired, updated, available, and unavailable replica counts.

Fix the first blocking condition and roll forward, or use `kubectl rollout undo deployment NAME` when the prior revision is known healthy. Confirm the rollout completes and old replicas terminate safely. Record the failed revision and reason so the same image or configuration is not reintroduced.

Apply/remove: use the deployment fixtures under `k8s/`; remove each test resource with its manifest.
