# Service with no ready endpoints

A Service with no endpoints usually means its selector matches no pods or all matching pods are unready. Compare `kubectl get svc SERVICE -o yaml` with pod labels, then inspect EndpointSlices and readiness conditions. A selector typo, namespace mismatch, or failing readiness probe is more likely than a Service object failure.

Correct labels or selectors, or repair the readiness condition. Do not point the Service at arbitrary pods to hide the issue. Verify EndpointSlices contain the expected addresses and make an in-cluster request through the Service DNS name.

Apply/remove: deploy the sample application with `k8s/sample-app.yaml`; remove it with `kubectl delete -f k8s/sample-app.yaml`.

## Remediation steps
1. Compare Service selectors with pod labels and inspect EndpointSlices and readiness conditions.
2. Identify whether the selector, namespace, or readiness state is wrong.
3. Return `escalate` with the approved label, selector, or readiness correction; never target arbitrary pods.
4. Verify EndpointSlices contain expected addresses and an in-cluster request succeeds.

An empty endpoint set for an unused service is contained, but an empty set on a user-facing or dependency-critical service means requests cannot be served and needs immediate action.
