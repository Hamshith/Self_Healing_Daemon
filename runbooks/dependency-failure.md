# Dependency failure

A dependency failure is indicated by connection refusals, timeouts, DNS errors, or elevated downstream error responses. Identify the dependency from logs and traces, then check its Service, Endpoints, readiness state, and recent events. Verify DNS from inside the cluster and distinguish an unavailable dependency from a network policy or credential problem.

Recover the dependency or route traffic to a healthy version. Do not repeatedly restart the caller when the dependency remains unavailable; that amplifies load and hides the original signal. Use bounded timeouts, exponential backoff, and circuit breaking. Confirm downstream health and caller error rate recover together.

Apply/remove: use the sample Service fixtures in `k8s/sample-app.yaml`; remove them with the matching delete command.
