# OOMKilled: memory limit

A container terminated with reason `OOMKilled` exceeded its cgroup memory limit. Confirm with `kubectl describe pod POD` and inspect the last termination state, working-set metrics, and application logs. Distinguish a container limit breach from node memory pressure; both can produce memory symptoms but require different fixes.

Measure normal and peak usage before changing limits. Increase the container memory limit and request together when cluster capacity allows, or fix the allocation pattern and add bounded queues. Roll out the Deployment and watch for a stable restart count. A temporary 25 percent increase is a reasonable controlled experiment, not a substitute for capacity planning.

Apply/remove: use the OOM fixture under `k8s/oom-deployment.yaml`; remove it with `kubectl delete -f k8s/oom-deployment.yaml`.
