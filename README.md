# LLM-Augmented Self-Healing Daemon for Kubernetes

A Python daemon that monitors a minikube Kubernetes cluster, detects multiple Kubernetes failure modes, measures real in-cluster network latency, and leverages **Google Gemini** to diagnose incidents automatically.

---

## 1. Prerequisites

| Tool | Version | Install |
|---|---|---|
| **Python** | 3.10+ | [python.org](https://www.python.org/downloads/) |
| **minikube** | 1.30+ | [minikube docs](https://minikube.sigs.k8s.io/docs/start/) |
| **kubectl** | 1.27+ | [kubectl docs](https://kubernetes.io/docs/tasks/tools/) |
| **Helm** | 3.x | [helm.sh](https://helm.sh/docs/intro/install/) |
| **Docker** | 20+ | [docker.com](https://docs.docker.com/get-docker/) |
| **Google Gemini API Key** | — | [aistudio.google.com](https://aistudio.google.com/apikey) |

---

## 2. Setup

```bash
# Clone or navigate to the project directory
cd Self_Healing_Daemon

# Create and activate a virtual environment
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Create your .env file from the template
copy .env.example .env      # Windows
# cp .env.example .env      # macOS / Linux

# Edit .env and paste your Gemini API key
# GEMINI_API_KEY=AIza...
```

---

## 3. Start minikube

```bash
minikube start --memory=4096 --cpus=2 --driver=docker
minikube addons enable metrics-server
```

Verify the cluster is running:

```bash
kubectl cluster-info
kubectl get nodes
```

---

## 4. Install Prometheus via Helm

```bash
# Add the Prometheus community Helm chart repo
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# Install kube-prometheus-stack (includes kube-state-metrics)
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace

# Port-forward Prometheus to localhost:9090
kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090 &
```

> **Note:** Prometheus is optional. The daemon falls back to the Kubernetes API if Prometheus is not reachable.

## 5. Install Chaos Mesh via Helm

```bash
# Add the ChaosMesh community Helm chart repo
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update

# Install kube-chaos-mesh-stack
helm install chaos-mesh chaos-mesh/chaos-mesh -n=chaos-mesh --create-namespace --version 2.8.4

```

Chaos Mesh is required only for the NetworkLatency scenario. Verify that its
controller and daemon pods are ready before applying a NetworkChaos resource:

```bash
kubectl get pods -n chaos-mesh
```

The daemon creates a short-lived curl pod inside the cluster and measures the
configured service URL. It reports `NetworkLatency` only when Chaos Mesh
reports `AllInjected=True` and measured latency is at least
`NETWORK_LATENCY_THRESHOLD_MS` (250 ms by default).

These settings can be overridden in `.env`:

```text
NETWORK_LATENCY_TARGET_URL=http://nginx-service.default.svc.cluster.local
NETWORK_LATENCY_THRESHOLD_MS=250
NETWORK_LATENCY_PROBE_IMAGE=curlimages/curl:8.10.1
NETWORK_LATENCY_PROBE_TIMEOUT_SECONDS=60
```

The Kubernetes identity running the daemon needs permission to create, read,
exec into, and delete pods in the probe namespace.

---

## 6. Deploy the Healthy Baseline

```bash
# Deploy healthy baseline pods (nginx frontend + httpd backend)
kubectl apply -f k8s/sample-app.yaml

# Verify pods are running
kubectl get pods
```

---

## 7. Run the Daemon

```bash
python daemon.py
```

You should see a startup banner followed by periodic cluster checks every 30 seconds.

## 8. Run the Dashboard

The dashboard reads the same `incidents.db` file written by the daemon. Start the
API and frontend in separate terminals from `Self_Healing_Daemon`:

```bash
uvicorn api:app --reload --port 8000
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal (normally `http://localhost:5173`).

---

## 9. Inject Fault Scenarios

Apply one scenario at a time from a separate terminal. Wait for the affected
pod to be created before checking its status. Do not apply every fault
manifest together because each scenario is intended to be isolated.

### CrashLoopBackOff / ApplicationCrash

The container exits with code 1 and eventually enters CrashLoopBackOff.

```bash
kubectl apply -f k8s/broken-deployment.yaml
kubectl get pods -l app=broken-service -w
```

### ConfigError

The deployment references the missing `mongo-config` ConfigMap, so the pod
should report CreateContainerConfigError.

```bash
kubectl apply -f k8s/configerror-deployment.yaml
kubectl get pods -l app=mongodb -w
kubectl describe pod -l app=mongodb
```

### Missing Secret / ConfigError

The deployment references the missing `db-credentials` Secret, so the pod
should fail during container configuration.

```bash
kubectl apply -f k8s/missingsecret-deployment.yaml
kubectl get pods -l app=mongodb-secret -w
kubectl describe pod -l app=mongodb-secret
```

### ImagePullError

The manifest uses a deliberately nonexistent nginx image tag.

```bash
kubectl apply -f k8s/imagepullerror-deployment.yaml
kubectl get pods -l app=image-pull-error -w
kubectl describe pod -l app=image-pull-error
```

### OOMKilled

The application exceeds its 100 MiB memory limit. The daemon should detect
OOMKilled and increase the owning Deployment's memory limit by 25 percent.

```bash
kubectl apply -f k8s/oom-deployment.yaml
kubectl get pods -l app=oom-service -w
kubectl logs -f deployment/oom-service
```

After detection, verify the remediation:

```bash
kubectl get deployment oom-service \
  -o jsonpath="{.spec.template.spec.containers[0].resources.limits.memory}"
```

### CPUThrottle

The container has a 50 millicore CPU limit. The daemon compares metrics-server
usage against that limit after three consecutive high-usage polls.

```bash
kubectl apply -f k8s/cputhrottle-deployment.yaml
kubectl get pods -l app=cputhrottle-service -w
kubectl top pod -l app=cputhrottle-service
```

The deployment comments also reference these Prometheus counters for manual
investigation:

```text
container_cpu_cfs_throttled_periods_total
container_cpu_cfs_periods_total
```

### NetworkLatency

Apply the nginx service first, wait until its pod is ready, and then apply the
separate NetworkChaos manifest. Applying the chaos resource before the target
pod exists can result in `Failed to select targets: no pod is selected`.

```bash
kubectl apply -f k8s/networklatency-deployment.yaml
kubectl wait --for=condition=ready pod -l app=nginx --timeout=120s
kubectl apply -f k8s/networklatency-creater.yaml
```

Verify that Chaos Mesh selected and injected the delay:

```bash
kubectl describe networkchaos chaos-creater -n default
kubectl get networkchaos chaos-creater -n default \
  -o jsonpath="{range .status.conditions[*]}{.type}={.status}{'\n'}{end}"
kubectl get networkchaos chaos-creater -n default \
  -o jsonpath="{.status.experiment.containerRecords[0].phase}{'\n'}"
```

The expected state is `Selected=True`, `AllInjected=True`, and phase
`Injected`. To manually measure the service from inside the cluster:

```bash
kubectl run tmp-curl --rm -it --image=curlimages/curl:8.10.1 \
  --restart=Never -- curl -o /dev/null -sS \
  -w "latency=%{time_total}s\n" \
  http://nginx-service.default.svc.cluster.local
```

---

## 10. What to Expect

Once the daemon detects a fault:

1. It collects pod logs, restart counts, and Kubernetes warning events.
2. It sends all signals to **Gemini** for analysis.
3. A colour-coded **Incident Diagnosis Report** is printed to the terminal showing:
   - Root cause and category
   - Confidence and severity
   - Evidence (specific log lines / events)
   - Recommended remediation command
4. The report is saved as a timestamped JSON file in the `incidents/` directory.

Example terminal output:

```
╔════════════════════════════════════════════════════════════╗
║  🔍  INCIDENT DIAGNOSIS REPORT                            ║
╠════════════════════════════════════════════════════════════╣
║  Timestamp        2026-05-03T14:22:01Z                     ║
║  Pod Name         broken-service-7b9d5c6f8-xk2lp           ║
║  Restart Count    5                                         ║
║  Severity         CRITICAL                                  ║
╚════════════════════════════════════════════════════════════╝
```

---

## 11. Clean Up

```bash
# Remove any scenario that is still deployed
kubectl delete -f k8s/broken-deployment.yaml
kubectl delete -f k8s/configerror-deployment.yaml
kubectl delete -f k8s/missingsecret-deployment.yaml
kubectl delete -f k8s/imagepullerror-deployment.yaml
kubectl delete -f k8s/oom-deployment.yaml
kubectl delete -f k8s/cputhrottle-deployment.yaml
kubectl delete -f k8s/networklatency-creater.yaml
kubectl delete -f k8s/networklatency-deployment.yaml

# Remove the healthy baseline
kubectl delete -f k8s/sample-app.yaml

# Uninstall Prometheus (if installed)
helm uninstall prometheus -n monitoring

# Uninstall Chaos Mesh (if installed)
helm uninstall chaos-mesh -n chaos-mesh

# Stop minikube
minikube stop

# (Optional) Delete the minikube cluster entirely
minikube delete
```

---

## 12. Project Structure

```
self-healing-daemon/
├── daemon.py              # Main daemon loop
├── collector.py           # Signal collection (Prometheus + K8s API)
├── detector.py            # Anomaly detection logic
├── llm_client.py          # Google Gemini API integration + prompt
├── reporter.py            # Formats and saves incident reports
├── config.py              # All config constants in one place
├── requirements.txt       # All pip dependencies
├── .env.example           # Template for environment variables
├── k8s/
│   ├── broken-deployment.yaml          # ApplicationCrash
│   ├── configerror-deployment.yaml     # Missing ConfigMap
│   ├── cputhrottle-deployment.yaml     # CPUThrottle
│   ├── imagepullerror-deployment.yaml  # ImagePullError
│   ├── missingsecret-deployment.yaml   # Missing Secret
│   ├── networklatency-creater.yaml     # Chaos Mesh NetworkChaos
│   ├── networklatency-deployment.yaml  # nginx target service
│   ├── oom-deployment.yaml              # OOMKilled
│   └── sample-app.yaml                  # Healthy baseline apps
└── README.md              # This file
```
