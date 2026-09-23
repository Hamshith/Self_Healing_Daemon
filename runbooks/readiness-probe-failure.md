# Readiness probe failure

Readiness failures remove a pod from Service endpoints without necessarily restarting it. Inspect probe path, port, scheme, initial delay, timeout, and failure threshold, then test the endpoint from inside the cluster. Common causes are a slow startup, wrong port, dependency-gated health check, or an application that is alive but not ready.

Fix the application endpoint or tune probe timing based on measured startup behavior. Keep liveness and readiness semantics separate: liveness should detect a dead process, while readiness should gate traffic. Verify endpoint membership and request success after rollout.

Apply/remove: add a probe to a sample Deployment for testing; remove the Deployment with its manifest.
