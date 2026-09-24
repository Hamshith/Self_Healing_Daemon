# Node pressure and eviction

Node pressure can evict pods or prevent scheduling when memory, disk, inode, or PID capacity is exhausted. Check node Conditions, allocatable resources, kubelet events, and eviction messages with `kubectl describe node NODE`. Identify whether the pressure is cluster-wide or isolated to one node.

Remove unused images and workloads, expand node capacity, or correct requests and limits after identifying the pressured resource. Ensure critical workloads have appropriate priority and disruption budgets. Confirm pressure clears and evicted pods reschedule before declaring recovery.

Apply/remove: reproduce resource pressure only in an isolated test cluster; clean up the workload and unused test images afterward.

## Remediation steps
1. Inspect node Conditions, allocatable capacity, and kubelet eviction events.
2. Identify the pressured resource and affected workloads.
3. Return `escalate` with the required capacity, cleanup, or request/limit change.
4. Verify pressure clears and evicted pods reschedule.

Brief pressure on a non-critical node is containable; eviction of stateful or widely replicated services, especially with data-loss risk, requires immediate cluster response.
