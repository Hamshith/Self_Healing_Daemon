"""
Configuration constants for the Self-Healing Daemon.
All tuneable parameters are centralized here.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ── Polling ──────────────────────────────────────────────
POLL_INTERVAL = 30  # seconds between each cluster check

# ── Anomaly Detection ───────────────────────────────────
RESTART_THRESHOLD = 3  # minimum restarts before flagging a pod
NETWORK_LATENCY_TARGET_URL = os.getenv(
	"NETWORK_LATENCY_TARGET_URL",
	"http://nginx-service.default.svc.cluster.local",
)
NETWORK_LATENCY_THRESHOLD_MS = float(os.getenv("NETWORK_LATENCY_THRESHOLD_MS", "250"))
NETWORK_LATENCY_PROBE_IMAGE = os.getenv(
	"NETWORK_LATENCY_PROBE_IMAGE",
	"curlimages/curl:8.10.1",
)
NETWORK_LATENCY_PROBE_TIMEOUT_SECONDS = int(
	os.getenv("NETWORK_LATENCY_PROBE_TIMEOUT_SECONDS", "60")
)

# ── Prometheus ──────────────────────────────────────────
PROMETHEUS_URL = "http://localhost:9090"

# ── Log Collection ──────────────────────────────────────
LOG_LINES = 50  # last N lines of pod logs to collect

# ── Incident Storage ────────────────────────────────────
INCIDENTS_DIR = "./incidents"
REPORTS_DIR = "./reports"
DB_PATH = "./incidents.db"

# ── LLM (Google Gemini) ────────────────────────────────
MODEL = "gemini-3.5-flash"
MAX_TOKENS = 4096
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")