# LLM-Augmented Self-Healing Daemon for Kubernetes

A Python daemon that monitors a minikube Kubernetes cluster, detects pods in **CrashLoopBackOff** state using Prometheus metrics and the Kubernetes Event API, and leverages **Google Gemini** to diagnose incidents automatically.

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
## 5. Install ChaosMesh via Helm

```bash
# Add the ChaosMesh community Helm chart repo
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update

# Install kube-chaos-mesh-stack
helm install chaos-mesh chaos-mesh/chaos-mesh -n=chaos-mesh --create-namespace --version 2.8.4

```


> **Note:** Prometheus is optional. The daemon falls back to the Kubernetes API if Prometheus is not reachable.

For NetworkChaos detection, start the nginx deployment first and then apply
`k8s/networklatency-creater.yaml`. The daemon creates a short-lived curl pod
inside the cluster and measures the configured service URL. It reports
`NetworkLatency` only when Chaos Mesh reports `AllInjected=True` and the
measured latency is at least `NETWORK_LATENCY_THRESHOLD_MS` (250 ms by
default).

These settings can be overridden in `.env`:

```text
NETWORK_LATENCY_TARGET_URL=http://nginx-service.default.svc.cluster.local
NETWORK_LATENCY_THRESHOLD_MS=250
NETWORK_LATENCY_PROBE_IMAGE=curlimages/curl:8.10.1
NETWORK_LATENCY_PROBE_TIMEOUT_SECONDS=10
```

The Kubernetes identity running the daemon needs permission to create, read,
exec into, and delete pods in the probe namespace.

---

## 6. Deploy Sample Apps

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

## 8a. Run the dashboard

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

## 8. Inject a Fault

In a **separate terminal**, apply the broken deployment:

```bash
kubectl apply -f k8s/broken-deployment.yaml
```

This creates a pod that immediately exits with code 1, causing Kubernetes to enter a **CrashLoopBackOff** cycle. After 3+ restarts (roughly 1–2 minutes), the daemon will detect the anomaly.

---

## 9. What to Expect

Once the daemon detects the CrashLoopBackOff pod:

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

## 10. Clean Up

```bash
# Remove the broken deployment
kubectl delete -f k8s/broken-deployment.yaml

# Remove sample apps
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

## Project Structure

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
│   ├── broken-deployment.yaml   # Test fault: CrashLoopBackOff
│   └── sample-app.yaml          # Healthy 2-pod sample application
└── README.md              # This file
```
