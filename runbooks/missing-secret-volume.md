# Missing Secret: volume or environment reference

A pod that references a Secret which does not exist in its namespace may remain Pending or report CreateContainerConfigError. Inspect the pod Events and the Deployment's `secretKeyRef` or Secret volume name. Names are namespace-scoped, and a similarly named Secret in another namespace does not satisfy the reference.

Restore the Secret through the approved secret-management workflow, or patch the workload to the intended Secret name. Never commit a real credential in a fixture or source repository. After the object exists, restart or wait for the controller to recreate the pod, then confirm the Secret is mounted or injected without exposing its value.

Apply/remove: apply `k8s/missingsecret-deployment.yaml`; remove it with `kubectl delete -f k8s/missingsecret-deployment.yaml`.
