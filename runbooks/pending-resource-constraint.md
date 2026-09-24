# Pending: resource constraint

A pod stuck Pending for more than five minutes is usually unschedulable. Check `kubectl describe pod POD` for FailedScheduling events. Typical causes are insufficient CPU or memory, an unmatched node selector, taints without tolerations, unavailable persistent volumes, or affinity rules that cannot be satisfied. Compare requests against node allocatable capacity with `kubectl describe nodes`.

Fix the specific constraint rather than deleting the pod repeatedly. Reduce requests only when the workload can safely run with less capacity, add or resize nodes, or correct selectors, tolerations, storage classes, and claims. Verify that the scheduler places the pod and that it passes readiness checks.

Apply/remove: apply a pending fixture such as `k8s/configerror-deployment.yaml` only when testing; remove it with `kubectl delete -f`.

## Remediation steps
1. Inspect FailedScheduling events and compare requests with node allocatable resources.
2. Identify whether requests, selectors, taints, affinity, or storage blocks scheduling.
3. Return `escalate` with the specific approved scheduling or capacity change.
4. Verify the scheduler places the pod and readiness checks pass.

A delayed batch pod may be tolerated within its schedule, while a critical workload unable to schedule or a capacity shortfall affecting multiple services requires immediate capacity action.
