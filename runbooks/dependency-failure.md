# Dependency failure

A dependency failure is indicated by connection refusals, timeouts, DNS errors, or elevated downstream error responses. Identify the dependency from logs and traces, then check its Service, Endpoints, readiness state, and recent events. Verify DNS from inside the cluster and distinguish an unavailable dependency from a network policy or credential problem.

Recover the dependency or route traffic to a healthy version. Do not repeatedly restart the caller when the dependency remains unavailable; that amplifies load and hides the original signal. Use bounded timeouts, exponential backoff, and circuit breaking. Confirm downstream health and caller error rate recover together.

Apply/remove: use the sample Service fixtures in `k8s/sample-app.yaml`; remove them with the matching delete command.

## Remediation steps
1. Identify the failing dependency from logs, traces, Service, and EndpointSlice state.
2. Check dependency health, DNS, network policy, and credentials.
3. Return `escalate` with the dependency recovery or routing change; do not repeatedly delete the caller pod.
4. Verify dependency health and caller error rate recover together.

If only a low-traffic caller is affected, contain the issue while investigating; widespread downstream failures, timeouts, or data-integrity risk require rapid coordination with the dependency owner.
