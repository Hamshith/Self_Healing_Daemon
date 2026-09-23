# Pending: resource constraint

A pod stuck Pending for more than five minutes is usually unschedulable. Check `kubectl describe pod POD` for FailedScheduling events. Typical causes are insufficient CPU or memory, an unmatched node selector, taints without tolerations, unavailable persistent volumes, or affinity rules that cannot be satisfied. Compare requests against node allocatable capacity with `kubectl describe nodes`.

Fix the specific constraint rather than deleting the pod repeatedly. Reduce requests only when the workload can safely run with less capacity, add or resize nodes, or correct selectors, tolerations, storage classes, and claims. Verify that the scheduler places the pod and that it passes readiness checks.

Apply/remove: apply a pending fixture such as `k8s/configerror-deployment.yaml` only when testing; remove it with `kubectl delete -f`.
