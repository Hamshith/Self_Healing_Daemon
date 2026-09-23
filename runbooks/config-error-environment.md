# Configuration error: required environment

Applications commonly crash during startup when a required environment variable is absent, malformed, or has the wrong type. Inspect previous logs for the exact validation message and compare the Deployment environment entries with the documented configuration contract. Confirm ConfigMaps and Secrets exist and that keys are spelled correctly.

Correct the source configuration or create the required object through the deployment system, then roll out the change and watch the new ReplicaSet. Avoid making a missing setting silently optional unless the application contract explicitly permits it. Validate the effective configuration without logging secret values.

Apply/remove: apply `k8s/configerror-deployment.yaml`; remove it with `kubectl delete -f k8s/configerror-deployment.yaml`.
