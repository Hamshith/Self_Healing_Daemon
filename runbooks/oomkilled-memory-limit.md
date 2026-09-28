# OOMKilled: memory limit

A container terminated with reason `OOMKilled` exceeded its cgroup memory limit. Confirm with `kubectl describe pod POD` and inspect the last termination state, working-set metrics, and application logs. Distinguish a container limit breach from node memory pressure; both can produce memory symptoms but require different fixes.

Measure normal and peak usage before changing limits. A temporary increase is a reasonable controlled experiment, not a substitute for capacity planning. The increase percentage should be chosen based on the observed headroom deficit and available cluster capacity — a small deficit may warrant a modest increase, while a container consistently hitting its limit may need a larger adjustment or a code fix.

Apply/remove: use the OOM fixture under `k8s/oom-deployment.yaml`; remove it with `kubectl delete -f k8s/oom-deployment.yaml`.

## Remediation steps
1. Confirm the termination reason is `OOMKilled` and distinguish it from node pressure.
2. Measure normal and peak memory usage and verify the owning Deployment.
3. If cluster capacity allows and a memory-limit increase is safe, consider issuing `increase_memory_limit` with an appropriate `parameters.increase_pct` derived from the observed deficit — escalate if capacity is insufficient or the root cause is a memory leak.
4. Verify the rollout and a stable restart count over the next several minutes; escalate if restarts continue, as a sustained OOM pattern requires a code-level fix or capacity addition, not repeated limit bumps.

An occasional restart of a non-critical worker is recoverable, but repeated memory exhaustion across serving replicas can cause an outage and should be handled urgently.
