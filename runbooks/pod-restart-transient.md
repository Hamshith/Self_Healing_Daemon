
# Transient pod restart

A restart may be transient when a process exits once because of a node event, temporary dependency outage, or startup race. Compare current and previous logs, termination reason, restart count, node events, and readiness history. A single restart without a continuing anomaly is not enough evidence for an automated fix.

Allow the controller to recreate the pod and observe several health checks. If restarts continue, classify the underlying signal as application, resource, configuration, or dependency failure. Avoid deleting healthy pods merely to reset a counter, because that destroys useful evidence and can increase service disruption.

Apply/remove: use `k8s/broken-deployment.yaml` only for controlled CrashLoopBackOff testing; remove it with the matching delete command.
