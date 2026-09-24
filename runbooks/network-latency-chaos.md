# Network latency: injected delay

Network latency is confirmed when a Chaos Mesh NetworkChaos resource reports injection active and an in-cluster probe observes latency above the configured threshold. Check the NetworkChaos selector, target service, direction, duration, and status conditions. Also compare application timeout and retry metrics so an injected delay is not confused with a slow dependency or DNS problem.

Remove or expire the experiment after collecting evidence, then verify request latency and error rate return to baseline. For a real incident, inspect the network path, service endpoints, node health, and timeout budgets before changing application retries. Record the experiment name and measured latency in the incident report.

Apply/remove: apply `k8s/networklatency-creater.yaml`; remove it with `kubectl delete -f k8s/networklatency-creater.yaml`.

## Remediation steps
1. Confirm NetworkChaos is active and record target and measured latency.
2. Return `escalate` with the experiment name and evidence so an operator can remove or expire it.
3. Verify latency and error rate return to baseline after removal.

Delay affecting a test target is limited to that experiment; latency that breaches user timeouts across a production path can cause broad request failure and needs urgent action.
