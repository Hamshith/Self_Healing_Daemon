# CrashLoopBackOff: application crash

CrashLoopBackOff means the container starts, exits, and Kubernetes backs off before restarting it. Common causes include an uncaught exception, invalid startup arguments, missing files, or a process that exits immediately. Confirm the container termination exit code and inspect current and previous logs with `kubectl logs POD -c CONTAINER --previous`. Review warning events and the image command or arguments in the Deployment.

Fix the application defect or configuration, then deploy a new image or patch the Deployment. A pod deletion can clear a transient failed process, but it is not a durable fix for a deterministic crash. Verify the replacement reaches Ready and its restart count remains stable.

Apply/remove: reproduce with `kubectl apply -f k8s/broken-deployment.yaml`; remove with `kubectl delete -f k8s/broken-deployment.yaml`.

## Remediation steps
1. Inspect current and previous logs and confirm the termination reason.
2. If transient and safe, return `delete_pod` so the controller recreates it.
3. If deterministic, return `escalate` with the suspected code, image, or configuration fix.
4. Verify the replacement pod becomes Ready and its restart count stays stable.

A single restart with healthy replicas is limited impact; a crash across the serving replicas or a critical API that cannot become Ready requires urgent response.
