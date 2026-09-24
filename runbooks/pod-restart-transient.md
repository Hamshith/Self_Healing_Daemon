
# Transient pod restart

A restart may be transient when a process exits once because of a node event, temporary dependency outage, or startup race. Compare current and previous logs, termination reason, restart count, node events, and readiness history. A single restart without a continuing anomaly is not enough evidence for an automated fix.

Allow the controller to recreate the pod and observe several health checks. If restarts continue, classify the underlying signal as application, resource, configuration, or dependency failure. Avoid deleting healthy pods merely to reset a counter, because that destroys useful evidence and can increase service disruption.

Apply/remove: use `k8s/broken-deployment.yaml` only for controlled CrashLoopBackOff testing; remove it with the matching delete command.

## Remediation steps
1. Compare current and previous logs, termination reason, restart count, and node events.
2. Allow health checks to establish whether the restart is transient.
3. If transient and safe, return `delete_pod`; otherwise return `escalate`.
4. Verify several subsequent health checks remain healthy.

One restart without user impact is low concern; repeated restarts, rising error rates, or loss of replicas indicate escalating service risk and require faster investigation.
