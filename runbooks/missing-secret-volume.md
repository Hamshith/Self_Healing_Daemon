# Missing Secret: volume or environment reference

A pod that references a Secret which does not exist in its namespace may remain Pending or report CreateContainerConfigError. Inspect the pod Events and the Deployment's `secretKeyRef` or Secret volume name. Names are namespace-scoped, and a similarly named Secret in another namespace does not satisfy the reference.

Restore the Secret through the approved secret-management workflow, or patch the workload to the intended Secret name. Never commit a real credential in a fixture or source repository. After the object exists, restart or wait for the controller to recreate the pod, then confirm the Secret is mounted or injected without exposing its value.

Apply/remove: apply `k8s/missingsecret-deployment.yaml`; remove it with `kubectl delete -f k8s/missingsecret-deployment.yaml`.

## Remediation steps
1. Inspect Events and identify the missing Secret reference and namespace.
2. Return `escalate` with the approved secret-management action; never create or print credentials.
3. After the Secret or reference is corrected, wait for or restart the workload.
4. Verify the Secret is mounted or injected without exposing its value.

Treat a missing secret for one isolated worker as contained; a shared credential failure, authentication outage, or possible secret exposure needs immediate security and service-owner attention.
