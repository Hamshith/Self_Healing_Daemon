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

# ── Prometheus ──────────────────────────────────────────
PROMETHEUS_URL = "http://localhost:9090"

# ── Log Collection ──────────────────────────────────────
LOG_LINES = 50  # last N lines of pod logs to collect

# ── Incident Storage ────────────────────────────────────
INCIDENTS_DIR = "./incidents"

# ── LLM (Google Gemini) ────────────────────────────────
MODEL = "gemini-2.5-flash"
MAX_TOKENS = 4096
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
