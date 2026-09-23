# Node pressure and eviction

Node pressure can evict pods or prevent scheduling when memory, disk, inode, or PID capacity is exhausted. Check node Conditions, allocatable resources, kubelet events, and eviction messages with `kubectl describe node NODE`. Identify whether the pressure is cluster-wide or isolated to one node.

Remove unused images and workloads, expand node capacity, or correct requests and limits after identifying the pressured resource. Ensure critical workloads have appropriate priority and disruption budgets. Confirm pressure clears and evicted pods reschedule before declaring recovery.

Apply/remove: reproduce resource pressure only in an isolated test cluster; clean up the workload and unused test images afterward.
