# Service with no ready endpoints

A Service with no endpoints usually means its selector matches no pods or all matching pods are unready. Compare `kubectl get svc SERVICE -o yaml` with pod labels, then inspect EndpointSlices and readiness conditions. A selector typo, namespace mismatch, or failing readiness probe is more likely than a Service object failure.

Correct labels or selectors, or repair the readiness condition. Do not point the Service at arbitrary pods to hide the issue. Verify EndpointSlices contain the expected addresses and make an in-cluster request through the Service DNS name.

Apply/remove: deploy the sample application with `k8s/sample-app.yaml`; remove it with `kubectl delete -f k8s/sample-app.yaml`.
