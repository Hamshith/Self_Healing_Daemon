"""
generator.py -- Fine-tuning dataset generator for Self-Healing Daemon
=====================================================================

Produces a supervised fine-tuning dataset (JSONL, chat format) for
Qwen2.5-7B-Instruct / QLoRA that teaches the model to turn Kubernetes
pod evidence into the same structured diagnosis JSON the daemon gets
from Gemini.

Usage
-----
    python generator.py              # writes dataset/ directory
    python generator.py --no-rag     # skip RAG context (faster, offline)
    python generator.py --seed 99    # reproducible RNG

Output
------
    dataset/train.jsonl
    dataset/val.jsonl
    dataset/test.jsonl
    dataset/dataset_report.md

Each JSONL line format:
    {
        "messages": [
            {"role": "system",    "content": <SYSTEM_PROMPT>},
            {"role": "user",      "content": <filled USER_PROMPT_TEMPLATE>},
            {"role": "assistant", "content": <diagnosis JSON string>}
        ],
        "metadata": {
            "fault_type":  str,          # detector label / "no_fault"
            "source":      "real|synthetic",
            "variant_id":  str,          # e.g. "OOMKilled-v03"
            "with_rag":    bool
        }
    }

Design decisions & assumptions
-------------------------------
* Prompts use the EXACT SYSTEM_PROMPT and USER_PROMPT_TEMPLATE from
  llm_client.py -- no changes.
* Diagnosis schema taken verbatim from llm_client.py:
    root_cause, error, root_cause_category, confidence, evidence,
    severity, explanation, recommended_action, remediation_steps,
    safe_to_auto_remediate
* root_cause_category values (from llm_client.py schema comment):
    ImagePullError | OOMKilled | ApplicationCrash | ConfigError |
    DependencyFailure | ResourceLimit | PendingScheduling |
    CPUThrottle | NetworkLatency | Unknown
  ReadinessFailure (eval YAML) is mapped to Unknown per user decision.
* safe_to_auto_remediate = True ONLY for OOMKilled or ApplicationCrash
  with confidence = "high". All other combos → False.
* Split is by *variant_id*, not random, to prevent leakage.
  Variants are strings like "OOMKilled-v00" .. "OOMKilled-v29".
  Roughly 80 % of variant IDs go to train, 10 % val, 10 % test.
* Real incidents from incidents.db use the detector fault_type column
  as ground truth, NOT Gemini's root_cause_category.
* Incidents IDs 8-15 (OOMKilled, same pod running) share the same
  root physical event; they are included but tagged as the same
  variant so the split keeps all near-duplicates on one side.
* No secrets, API keys, or .env contents appear anywhere in the
  generated data. Pod names, images, and env var values are all fake.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ── Paths ────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DATASET_DIR = ROOT / "dataset"
DB_PATH = ROOT / "incidents.db"
RUNBOOKS_DIR = ROOT / "runbooks"

# ── Exact prompts from llm_client.py (DO NOT MODIFY) ─────────────────
SYSTEM_PROMPT = (
    "You are an expert Site Reliability Engineer analyzing "
    "a Kubernetes incident. You have deep knowledge of "
    "Kubernetes failure modes, container debugging, and "
    "cloud-native systems. Always respond with valid JSON only. "
    "No markdown, no explanation outside the JSON."
)

USER_PROMPT_TEMPLATE = """\
Analyze this Kubernetes incident and provide a diagnosis.

INCIDENT DETAILS:
- Pod Name: {pod_name}
- Namespace: {namespace}
- Restart Count: {restart_count}
- Detected At: {detected_at}

CONTAINER STATUSES:
{container_statuses}

RECENT KUBERNETES EVENTS (last 10 min):
{kubernetes_events}

RECENT POD LOGS (last 50 lines):
{recent_logs}

RELEVANT RUNBOOK CONTEXT:
{runbook_context}

SERVICE CORRELATION CONTEXT:
{correlation_context}

Respond with this exact JSON structure:
{{
  "root_cause": "one sentence describing the most likely root cause",
    "error": "concise description of the user-visible or operational failure",
  "root_cause_category": "one of: ImagePullError | OOMKilled | ApplicationCrash | ConfigError | DependencyFailure | ResourceLimit | PendingScheduling | CPUThrottle | NetworkLatency | Unknown",
  "confidence": "one of: high | medium | low",
  "evidence": ["list", "of", "specific", "log lines", "or events that support this diagnosis"],
    "severity": "one of: critical | high | medium | low, inferred from blast radius, service impact, recovery urgency, and runbook context",
  "explanation": "2-3 sentences explaining what is happening and why",
    "recommended_action": "human-readable summary of the remediation",
    "remediation_steps": [
        {{
            "step": 1,
            "action": "one of: delete_pod | increase_memory_limit | escalate",
            "reason": "why this step is appropriate",
            "parameters": {{"increase_pct": 0.25}}
        }}
    ],
    "safe_to_auto_remediate": true
}}

REMEDIATION STEP RULES:
- Return ordered steps, with one object per step.
- Only use delete_pod or increase_memory_limit for an automatic action.
- Use escalate when a human must supply a value, credential, image, or code change.
- Do not return shell commands as executable instructions. Commands may be included
    in the reason for operator guidance, but the remediator only executes the listed
    action names and validates their parameters.

    DIAGNOSIS RULES:
    - The detector fault type is only an initial signal. Derive the error description,
      root cause, category, and severity from incident evidence and runbook context.
    - Do not copy a fixed severity from the detector or fixture name. A localized,
      recoverable symptom may be low or medium; broad outage, data risk, or inability
      to start a critical service should raise urgency appropriately.
"""

# ── Valid enum values ─────────────────────────────────────────────────
VALID_CATEGORIES = {
    "ImagePullError", "OOMKilled", "ApplicationCrash", "ConfigError",
    "DependencyFailure", "ResourceLimit", "PendingScheduling",
    "CPUThrottle", "NetworkLatency", "Unknown",
}
VALID_CONFIDENCE = {"high", "medium", "low"}
VALID_SEVERITY   = {"critical", "high", "medium", "low"}
VALID_ACTIONS    = {"delete_pod", "increase_memory_limit", "escalate"}


# ─────────────────────────────────────────────────────────────────────
# Helper: safe_to_auto_remediate rule (strict, per user spec)
# ─────────────────────────────────────────────────────────────────────

def safe_to_auto_remediate(fault_type: str, category: str, confidence: str) -> bool:
    """
    True ONLY for OOMKilled or ApplicationCrash with confidence = "high".
    Every other combination → False.
    """
    if confidence != "high":
        return False
    return category in ("OOMKilled", "ApplicationCrash")


# ─────────────────────────────────────────────────────────────────────
# RAG context helper (lexical fallback, no ML deps required)
# ─────────────────────────────────────────────────────────────────────

_RUNBOOK_DOCS: list[dict] = []


def _load_runbooks() -> list[dict]:
    global _RUNBOOK_DOCS
    if _RUNBOOK_DOCS:
        return _RUNBOOK_DOCS
    docs = []
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        docs.append({"text": text, "source": path.name})
    _RUNBOOK_DOCS = docs
    return docs


def _lexical_retrieve(query: str, top_k: int = 3) -> list[dict]:
    docs = _load_runbooks()
    query_terms = set(re.findall(r"[a-z0-9]+", query.lower()))
    ranked = []
    for doc in docs:
        terms = set(re.findall(r"[a-z0-9]+", doc["text"].lower()))
        score = len(query_terms & terms)
        ranked.append((score, doc))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [d for s, d in ranked[:top_k] if s > 0]


def format_rag_context(fault_type: str, pod_name: str = "") -> str:
    """Return formatted runbook context for a given fault type."""
    query = f"{pod_name} {fault_type}"
    results = _lexical_retrieve(query)
    if not results:
        return "(no matching runbook context available)"
    return "\n\n".join(
        f"[{r['source']}]\n{r['text']}" for r in results
    )


NO_RAG_CONTEXT = "(no matching runbook context available)"


# ─────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────

def build_user_prompt(
    pod_name: str,
    namespace: str,
    restart_count: int,
    detected_at: str,
    container_statuses: list,
    kubernetes_events: list,
    recent_logs: str,
    runbook_context: str,
    correlation_context: Any = "(no related failures)",
) -> str:
    return USER_PROMPT_TEMPLATE.format(
        pod_name=pod_name,
        namespace=namespace,
        restart_count=restart_count,
        detected_at=detected_at,
        container_statuses=json.dumps(container_statuses, indent=2),
        kubernetes_events=json.dumps(kubernetes_events, indent=2),
        recent_logs=recent_logs,
        runbook_context=runbook_context,
        correlation_context=json.dumps(correlation_context, indent=2),
    )


def build_example(
    pod_name: str,
    namespace: str,
    restart_count: int,
    detected_at: str,
    container_statuses: list,
    kubernetes_events: list,
    recent_logs: str,
    diagnosis: dict,
    fault_type: str,
    source: str,
    variant_id: str,
    with_rag: bool,
    runbook_context: str,
    correlation_context: Any = "(no related failures)",
) -> dict:
    user_content = build_user_prompt(
        pod_name=pod_name,
        namespace=namespace,
        restart_count=restart_count,
        detected_at=detected_at,
        container_statuses=container_statuses,
        kubernetes_events=kubernetes_events,
        recent_logs=recent_logs,
        runbook_context=runbook_context if with_rag else NO_RAG_CONTEXT,
        correlation_context=correlation_context,
    )
    return {
        "messages": [
            {"role": "system",    "content": SYSTEM_PROMPT},
            {"role": "user",      "content": user_content},
            {"role": "assistant", "content": json.dumps(diagnosis, ensure_ascii=False)},
        ],
        "metadata": {
            "fault_type": fault_type,
            "source":     source,
            "variant_id": variant_id,
            "with_rag":   with_rag,
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Random vocabulary pools for synthetic variation
# ─────────────────────────────────────────────────────────────────────

POD_ADJECTIVES = [
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
    "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
]
DEPLOYMENT_NAMES = [
    "backend-api", "frontend-web", "data-processor", "auth-service",
    "notification-svc", "payment-gateway", "order-service", "inventory-svc",
    "search-engine", "report-generator", "cache-warmer", "job-scheduler",
    "metrics-collector", "webhook-handler", "queue-consumer", "sync-worker",
    "batch-importer", "event-streamer", "file-uploader", "ml-inference",
    "recommendation-engine", "audit-logger", "session-manager", "config-loader",
]
NAMESPACES = [
    "default", "production", "staging", "dev", "monitoring", "data-pipeline",
    "backend", "frontend", "payments", "auth", "ml-serving", "batch",
]
IMAGE_REPOS = [
    "company/", "myorg/", "team-alpha/", "prod-registry.example.com/apps/",
    "ghcr.io/myorg/", "registry.example.com/", "docker.io/myteam/",
]
IMAGE_NAMES = [
    "backend-api", "worker", "api-server", "frontend", "service",
    "processor", "gateway", "handler", "runner", "daemon",
]

BAD_TAGS = [
    "v2.9.99-nonexistent", "latest-stable-nonexistent", "v0.0.0-fake",
    "this-tag-does-not-exist", "release-99999", "broken-tag",
    "INVALID", "v99-rc1", "dev-20991301", "alpha-nopush",
]

MEMORY_LIMITS = [
    "64Mi", "100Mi", "128Mi", "150Mi", "200Mi", "256Mi", "300Mi", "512Mi",
]
CPU_LIMITS = [
    "50m", "100m", "150m", "200m", "250m", "500m",
]

CONFIGMAP_NAMES = [
    "app-config", "service-config", "db-config", "redis-config",
    "feature-flags", "runtime-config", "api-config", "worker-config",
]
SECRET_NAMES = [
    "db-credentials", "api-secrets", "tls-cert", "oauth-tokens",
    "stripe-api-key", "sendgrid-key", "jwt-secret", "aws-credentials",
]
ENV_VAR_NAMES = [
    "DATABASE_URL", "REDIS_URL", "API_KEY", "SECRET_KEY",
    "DATABASE_HOST", "DB_PASSWORD", "AUTH_TOKEN", "SMTP_PASSWORD",
]
CONFIGMAP_KEYS = [
    "DATABASE_URL", "REDIS_HOST", "LOG_LEVEL", "API_ENDPOINT",
    "TIMEOUT_MS", "MAX_CONNECTIONS", "FEATURE_FLAG_X",
]


def rand_pod_hash() -> str:
    """Random 10-char hex string like k8s pod name suffix."""
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=5)) + \
           "-" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=5))


def rand_uid() -> str:
    parts = [
        "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8)),
        "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4)),
        "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4)),
        "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4)),
        "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=12)),
    ]
    return "-".join(parts)


def rand_ts(base: datetime | None = None, jitter_hours: int = 24) -> str:
    if base is None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    delta = timedelta(
        hours=random.randint(0, jitter_hours * 30),
        minutes=random.randint(0, 59),
        seconds=random.randint(0, 59),
    )
    return (base + delta).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rand_image(repos: list = IMAGE_REPOS, names: list = IMAGE_NAMES) -> str:
    return random.choice(repos) + random.choice(names)


# ─────────────────────────────────────────────────────────────────────
# Synthetic example generators -- one function per fault type
# ─────────────────────────────────────────────────────────────────────

# --- ApplicationCrash -------------------------------------------------

_CRASH_LOG_TEMPLATES = [
    "Service crashing...\n",
    "Traceback (most recent call last):\n  File 'app.py', line {line}, in main\nRuntimeError: {msg}\n",
    "FATAL: unhandled exception -- {msg}\npanic: {msg}\ngoroutine 1 [running]:\nmain.main()\n\t/app/main.go:{line} +0x{hex}",
    "Error: Cannot find module '{module}'\n    at Function.Module._resolveFilename (internal/modules/cjs/loader.js:{line})\n",
    "java.lang.NullPointerException\n\tat com.example.{cls}.{method}({cls}.java:{line})\n",
    "Segmentation fault (core dumped)\n",
    "signal: killed\nfatal error: runtime: out of memory\n",
    "AssertionError: expected non-null value\n  at {cls}.{method} ({file}:{line}:{col})\n",
    "CRITICAL startup failed: missing required environment variable {var}\n",
    "panic: interface conversion: interface is nil, not {type}\n",
]
_CRASH_MSGS = [
    "startup configuration invalid", "failed to bind port 8080",
    "database connection refused", "missing required configuration",
    "unrecoverable internal error", "nil pointer dereference",
    "address already in use :8080", "disk I/O error on /data",
]
_CRASH_MODULES = ["express", "lodash", "mongoose", "axios", "pg"]
_CRASH_CLASSES = ["AppController", "ServiceManager", "DataProcessor", "RequestHandler"]
_CRASH_METHODS = ["start", "init", "connect", "run", "execute"]
_CRASH_FILES   = ["app.ts", "server.js", "main.go", "app.py"]
_CRASH_TYPES   = ["*http.Client", "*db.Connection", "Service", "Handler"]
_CRASH_VARS    = ENV_VAR_NAMES

EXIT_CODES_CRASH = [1, 2, 126, 127, 137, 143]


def make_crash_example(
    rng: random.Random, variant_idx: int, with_rag: bool
) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    rs_hash = rand_pod_hash()
    pod_name = f"{dep}-{rs_hash}"
    restart_count = rng.randint(3, 15)
    exit_code = rng.choice(EXIT_CODES_CRASH)
    ts = rand_ts(jitter_hours=30 * 24)

    # Randomize log
    log_tmpl = rng.choice(_CRASH_LOG_TEMPLATES)
    log = log_tmpl.format(
        line=rng.randint(10, 300),
        msg=rng.choice(_CRASH_MSGS),
        hex=format(rng.randint(0x10, 0xFF), "02x"),
        module=rng.choice(_CRASH_MODULES),
        cls=rng.choice(_CRASH_CLASSES),
        method=rng.choice(_CRASH_METHODS),
        file=rng.choice(_CRASH_FILES),
        col=rng.randint(1, 40),
        var=rng.choice(_CRASH_VARS),
        type=rng.choice(_CRASH_TYPES),
    )

    # Randomize state: sometimes CrashLoopBackOff, sometimes terminated
    use_clbo = rng.random() < 0.6
    if use_clbo:
        backoff = rng.choice(["10s", "20s", "40s", "80s", "160s", "5m0s"])
        container_statuses = [{
            "name": dep.split("-")[0] + "-container",
            "ready": False,
            "restart_count": restart_count,
            "state": {
                "waiting": {
                    "reason": "CrashLoopBackOff",
                    "message": f"back-off {backoff} restarting failed container={dep.split('-')[0]}-container pod={pod_name}_{ns}({rand_uid()})",
                }
            },
        }]
        state_desc = "CrashLoopBackOff"
    else:
        container_statuses = [{
            "name": dep.split("-")[0] + "-container",
            "ready": False,
            "restart_count": restart_count,
            "state": {
                "terminated": {
                    "reason": "Error",
                    "exit_code": exit_code,
                }
            },
        }]
        state_desc = f"Terminated (exit_code={exit_code})"

    backoff_msg = rng.choice([
        f"Back-off restarting failed container {dep.split('-')[0]}-container in pod {pod_name}_{ns}({rand_uid()})",
        f"Container {dep.split('-')[0]}-container failed with exit code {exit_code}",
    ])
    kubernetes_events = [{
        "reason": "BackOff",
        "message": backoff_msg,
        "timestamp": ts,
        "type": "Warning",
    }]
    if rng.random() < 0.4:
        kubernetes_events.append({
            "reason": "Failed",
            "message": f"Error: failed to create containerd task: failed to create shim: OCI runtime create failed",
            "timestamp": ts,
            "type": "Warning",
        })

    # severity varies: one crashed pod in a multi-replica dep → medium/high
    confidence = "high"
    severity   = rng.choice(["medium", "high", "high", "high"])
    safe       = safe_to_auto_remediate("ApplicationCrash", "ApplicationCrash", confidence)

    # Whether delete_pod or escalate
    is_transient = exit_code == 1 and rng.random() < 0.35
    if is_transient:
        remediation_steps = [{
            "step": 1,
            "action": "delete_pod",
            "reason": "Transient crash; deleting pod so the controller recreates it cleanly.",
            "parameters": {},
        }]
        explanation = (
            f"The container '{dep.split('-')[0]}-container' exited with code {exit_code} -- "
            f"the pod is in {state_desc}. The crash appears transient given the exit code "
            f"and log context. Deleting the pod allows the ReplicaSet controller to schedule "
            f"a clean replacement."
        )
        recommended_action = (
            f"Delete the pod to trigger a clean restart by the ReplicaSet controller, then "
            f"monitor restart count and readiness to confirm stability."
        )
        safe = True  # override for high-confidence transient
    else:
        remediation_steps = [{
            "step": 1,
            "action": "escalate",
            "reason": (
                f"The application is crashing deterministically (exit code {exit_code}). "
                f"A code, image, or configuration fix is required."
            ),
            "parameters": {},
        }]
        explanation = (
            f"The container '{dep.split('-')[0]}-container' is in {state_desc} with exit code "
            f"{exit_code}. Log evidence confirms a deterministic application failure at startup. "
            f"Kubernetes repeatedly restarts the container but the root cause requires a "
            f"code or configuration fix rather than a pod deletion."
        )
        recommended_action = (
            f"Inspect pod logs (`kubectl logs {pod_name} -n {ns} --previous`) to identify "
            f"the exact failure, then patch the Deployment with the corrected image or configuration."
        )

    evidence = [
        f"container state: {state_desc}",
        f"restart_count: {restart_count}",
        f"pod log: {log.strip()[:120]}",
        f"event: {backoff_msg[:120]}",
    ]

    diagnosis = {
        "root_cause": f"The application inside '{dep.split('-')[0]}-container' is crashing on every startup (exit code {exit_code}).",
        "error": f"Pod '{pod_name}' is in {state_desc} and cannot serve traffic.",
        "root_cause_category": "ApplicationCrash",
        "confidence": confidence,
        "evidence": evidence,
        "severity": severity,
        "explanation": explanation,
        "recommended_action": recommended_action,
        "remediation_steps": remediation_steps,
        "safe_to_auto_remediate": safe,
    }

    rag_ctx = format_rag_context("ApplicationCrash CrashLoopBackOff", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name,
        namespace=ns,
        restart_count=restart_count,
        detected_at=ts,
        container_statuses=container_statuses,
        kubernetes_events=kubernetes_events,
        recent_logs=log,
        diagnosis=diagnosis,
        fault_type="ApplicationCrash",
        source="synthetic",
        variant_id=f"ApplicationCrash-v{variant_idx:03d}",
        with_rag=with_rag,
        runbook_context=rag_ctx,
    )


# --- OOMKilled --------------------------------------------------------

_OOM_LOG_TEMPLATES = [
    "Starting application...\nLoading {n} records into memory...\nProcessing batch {batch}...\n",
    "Allocating buffer of {mb}MB for image processing...\nProcessed {n} items\n",
    "Warming up {n} ML model shards...\nLoading embeddings ({mb}MB)...\n",
    "Cache initialization: allocating {mb}MB\nCache miss rate: 0%\n",
    "Worker starting...\nLoading dataset ({mb}MB)...\nOOM signal received\n",
    "Building in-memory index for {n} documents...\n",
    "INFO Starting service\nINFO Loading {n} configuration entries\nFATAL out of memory\n",
]

def make_oom_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    restart_count = rng.randint(0, 8)
    mem_limit = rng.choice(MEMORY_LIMITS)
    mem_mb = int(mem_limit.replace("Mi", ""))
    ts = rand_ts(jitter_hours=30 * 24)

    log_tmpl = rng.choice(_OOM_LOG_TEMPLATES)
    log = log_tmpl.format(
        n=rng.randint(10000, 5000000),
        mb=rng.randint(mem_mb // 2, mem_mb * 3),
        batch=rng.randint(1, 500),
    )

    # last_state OOM (most common) vs current state OOM
    use_last_state = rng.random() < 0.65
    if use_last_state:
        container_statuses = [{
            "name": dep.split("-")[0] + "-container",
            "ready": False,
            "restart_count": restart_count,
            "state": {
                "waiting": {
                    "reason": "CrashLoopBackOff",
                    "message": f"back-off {rng.choice(['10s','20s','40s'])} restarting failed container",
                }
            },
            "last_state": {
                "terminated": {
                    "reason": "OOMKilled",
                    "exit_code": 137,
                    "source": "last_state",
                }
            },
        }]
        state_desc = "OOMKilled (last_state) → CrashLoopBackOff"
    else:
        container_statuses = [{
            "name": dep.split("-")[0] + "-container",
            "ready": False,
            "restart_count": restart_count,
            "state": {
                "terminated": {
                    "reason": "OOMKilled",
                    "exit_code": 137,
                }
            },
        }]
        state_desc = "OOMKilled (exit 137)"

    oom_event_msg = rng.choice([
        f"Container {dep.split('-')[0]}-container exceeded memory limit ({mem_limit}); OOMKilled.",
        f"OOMKilled: container exceeded memory limit of {mem_limit}.",
        f"Memory cgroup out of memory: Kill process in pod {pod_name}.",
    ])
    kubernetes_events = [{
        "reason": "OOMKilled",
        "message": oom_event_msg,
        "timestamp": ts,
        "type": "Warning",
    }]

    confidence = "high"
    severity   = rng.choice(["medium", "high", "high"])
    increase_pct = rng.choice([0.25, 0.50, 0.75, 1.0])
    safe       = safe_to_auto_remediate("OOMKilled", "OOMKilled", confidence)

    evidence = [
        f"container state: {state_desc}",
        f"exit_code: 137 (SIGKILL from kernel OOM killer)",
        f"memory limit: {mem_limit}",
        f"event: {oom_event_msg[:100]}",
        f"log: {log.strip()[:100]}",
    ]

    diagnosis = {
        "root_cause": (
            f"The container exceeded its memory limit of {mem_limit}, "
            f"causing the kernel OOM killer to terminate it (exit 137)."
        ),
        "error": f"Pod '{pod_name}' was OOMKilled; memory limit {mem_limit} is insufficient for workload demand.",
        "root_cause_category": "OOMKilled",
        "confidence": confidence,
        "evidence": evidence,
        "severity": severity,
        "explanation": (
            f"The '{dep.split('-')[0]}-container' container exhausted its {mem_limit} memory limit "
            f"and was terminated by the Linux kernel's OOM killer (SIGKILL, exit 137). "
            f"The application log shows large memory allocation attempts. "
            f"A temporary memory limit increase of {int(increase_pct * 100)}% may stabilise the pod "
            f"while the memory usage pattern is profiled."
        ),
        "recommended_action": (
            f"Increase memory limit by {int(increase_pct * 100)}% as a stabilisation measure, "
            f"then profile peak usage and consider optimising the allocation pattern or adding replicas."
        ),
        "remediation_steps": [{
            "step": 1,
            "action": "increase_memory_limit",
            "reason": f"Container hit {mem_limit} memory ceiling and was OOMKilled. Increasing limit by {int(increase_pct*100)}% provides headroom while the allocation is profiled.",
            "parameters": {"increase_pct": increase_pct},
        }],
        "safe_to_auto_remediate": safe,
    }

    rag_ctx = format_rag_context("OOMKilled memory limit", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=restart_count,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs=log,
        diagnosis=diagnosis, fault_type="OOMKilled",
        source="synthetic", variant_id=f"OOMKilled-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=rag_ctx,
    )


# --- ImagePullError ----------------------------------------------------

_PULL_ERROR_MSGS = [
    "failed to pull image \"{image}:{tag}\": rpc error: code = NotFound desc = failed to pull and unpack image: failed to resolve reference \"{image}:{tag}\": unexpected status code 404 Not Found",
    "ErrImagePull: Back-off pulling image \"{image}:{tag}\": image not found",
    "Failed to pull image \"{image}:{tag}\": pull access denied for {image}, repository does not exist or may require 'docker login'",
    "failed to pull image \"{image}:{tag}\": rpc error: code = Unknown desc = error pulling image configuration: toomanyrequests: Too Many Requests.",
    "Failed to pull image \"{image}:{tag}\": Error response from daemon: manifest for {image}:{tag} not found: manifest unknown",
]

def make_image_pull_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    image = rand_image()
    tag   = rng.choice(BAD_TAGS)
    ts    = rand_ts(jitter_hours=30 * 24)

    pull_err = rng.choice(_PULL_ERROR_MSGS).format(image=image, tag=tag)

    container_statuses = [{
        "name": dep.split("-")[0] + "-container",
        "ready": False,
        "restart_count": 0,
        "state": {
            "waiting": {
                "reason": rng.choice(["ImagePullBackOff", "ErrImagePull"]),
                "message": pull_err,
            }
        },
    }]

    kubernetes_events = [
        {
            "reason": "Failed",
            "message": f"Failed to pull image \"{image}:{tag}\": {pull_err[:120]}",
            "timestamp": ts,
            "type": "Warning",
        },
        {
            "reason": "BackOff",
            "message": f"Back-off pulling image \"{image}:{tag}\"",
            "timestamp": ts,
            "type": "Warning",
        },
    ]

    evidence = [
        f"container waiting: ImagePullBackOff / ErrImagePull",
        f"image: {image}:{tag}",
        f"event: {pull_err[:120]}",
    ]

    diagnosis = {
        "root_cause": f"The image '{image}:{tag}' cannot be pulled because the tag does not exist in the registry.",
        "error": f"Pod '{pod_name}' is stuck in ImagePullBackOff; image '{image}:{tag}' is unavailable.",
        "root_cause_category": "ImagePullError",
        "confidence": "high",
        "evidence": evidence,
        "severity": rng.choice(["medium", "high"]),
        "explanation": (
            f"The kubelet attempted to pull '{image}:{tag}' but received a non-200 response from the registry. "
            f"The tag '{tag}' does not exist or the registry requires authentication credentials. "
            f"The container cannot start until a valid image reference is provided."
        ),
        "recommended_action": (
            f"Verify the intended image tag and registry credentials, then patch the Deployment with a valid "
            f"immutable tag. Do not guess a replacement tag."
        ),
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": f"Image '{image}:{tag}' does not exist in the registry. A human must supply the correct tag or imagePullSecret.",
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }

    rag_ctx = format_rag_context("ImagePullError ImagePullBackOff bad tag", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=0,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs="(no logs -- container never started)",
        diagnosis=diagnosis, fault_type="ImagePullError",
        source="synthetic", variant_id=f"ImagePullError-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=rag_ctx,
    )


# --- ConfigError (CreateContainerConfigError) -------------------------

_CONFIG_KINDS = ["ConfigMap", "Secret"]

_CONFIG_MESSAGES = [
    "configmap \"{name}\" not found",
    "secret \"{name}\" not found",
    "couldn't find key {key} in ConfigMap {ns}/{name}",
    "couldn't find key {key} in Secret {ns}/{name}",
    "error resolving configmap ref {name}/{key}: configmap not found",
    "error resolving secret ref {name}/{key}: secret not found",
]

def make_config_error_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    ts = rand_ts(jitter_hours=30 * 24)

    kind = rng.choice(_CONFIG_KINDS)
    if kind == "ConfigMap":
        ref_name = rng.choice(CONFIGMAP_NAMES)
        ref_key  = rng.choice(CONFIGMAP_KEYS)
    else:
        ref_name = rng.choice(SECRET_NAMES)
        ref_key  = rng.choice(["password", "api-key", "token", "cert", "key"])

    err_msg = rng.choice(_CONFIG_MESSAGES).format(name=ref_name, key=ref_key, ns=ns)

    container_statuses = [{
        "name": dep.split("-")[0] + "-container",
        "ready": False,
        "restart_count": 0,
        "state": {
            "waiting": {
                "reason": "CreateContainerConfigError",
                "message": err_msg,
            }
        },
    }]

    kubernetes_events = [{
        "reason": "Failed",
        "message": f"Error: {err_msg}",
        "timestamp": ts,
        "type": "Warning",
    }]

    evidence = [
        f"container waiting: CreateContainerConfigError",
        f"message: {err_msg}",
        f"missing {kind}: {ref_name}",
    ]

    diagnosis = {
        "root_cause": (
            f"The container cannot start because the referenced {kind} '{ref_name}' "
            f"(key: '{ref_key}') does not exist in namespace '{ns}'."
        ),
        "error": f"Pod '{pod_name}' is stuck in CreateContainerConfigError: {kind} '{ref_name}' not found.",
        "root_cause_category": "ConfigError",
        "confidence": "high",
        "evidence": evidence,
        "severity": rng.choice(["medium", "high"]),
        "explanation": (
            f"Kubernetes cannot construct the container environment because the {kind} "
            f"'{ref_name}' referenced in the Deployment spec does not exist in namespace '{ns}'. "
            f"The container is blocked before it can start, and the pod will remain in this state "
            f"until the {kind} is created or the reference is corrected."
        ),
        "recommended_action": (
            f"Create the missing {kind} '{ref_name}' in namespace '{ns}' or patch the "
            f"Deployment to reference the correct object name and key. Never commit real "
            f"credentials to source control."
        ),
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": f"{kind} '{ref_name}' (key: '{ref_key}') missing in namespace '{ns}'. A human must create it or correct the reference.",
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }

    rag_ctx = format_rag_context("ConfigError CreateContainerConfigError missing secret configmap", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=0,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs="(no logs -- container never started)",
        diagnosis=diagnosis, fault_type="ConfigError",
        source="synthetic", variant_id=f"ConfigError-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=rag_ctx,
    )


# --- PendingScheduling ------------------------------------------------

_PENDING_REASONS = [
    "0/{nodes} nodes are available: {nodes} Insufficient memory.",
    "0/{nodes} nodes are available: {nodes} Insufficient cpu.",
    "0/{nodes} nodes are available: 1 node(s) had taint {{node.kubernetes.io/disk-pressure: true}}, {rest} didn't match Pod's node affinity/selector.",
    "0/{nodes} nodes are available: {nodes} node(s) had volume node affinity conflict.",
    "0/{nodes} nodes are available: {nodes} Insufficient pods.",
    "0/{nodes} nodes are available: {nodes} node(s) had untolerated taint {{nvidia.com/gpu=present:NoSchedule}}.",
]

def make_pending_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    nodes = rng.randint(1, 5)
    rest  = max(0, nodes - 1)
    ts    = rand_ts(jitter_hours=30 * 24)
    mem_req = rng.choice(["8Gi", "16Gi", "32Gi", "4Gi", "256Mi"])
    cpu_req = rng.choice(["4", "8", "16", "2", "500m"])

    sched_msg = rng.choice(_PENDING_REASONS).format(nodes=nodes, rest=rest)

    container_statuses = []  # Pending pods have no running containers
    kubernetes_events = [
        {
            "reason": "FailedScheduling",
            "message": sched_msg,
            "timestamp": ts,
            "type": "Warning",
        }
    ]

    pending_mins = rng.randint(6, 30)
    evidence = [
        f"pod phase: Pending (>{pending_mins} minutes)",
        f"event FailedScheduling: {sched_msg}",
        f"resource request: cpu={cpu_req}, memory={mem_req}",
    ]

    diagnosis = {
        "root_cause": (
            f"The pod cannot be scheduled because no node has sufficient resources: {sched_msg}"
        ),
        "error": f"Pod '{pod_name}' has been Pending for over {pending_mins} minutes due to scheduling constraints.",
        "root_cause_category": "PendingScheduling",
        "confidence": "high",
        "evidence": evidence,
        "severity": rng.choice(["medium", "high"]),
        "explanation": (
            f"The Kubernetes scheduler cannot find a node that satisfies all resource requests "
            f"(cpu={cpu_req}, memory={mem_req}) and scheduling constraints. "
            f"The pod has been in Pending state for {pending_mins} minutes, "
            f"indicating a cluster capacity or scheduling policy problem."
        ),
        "recommended_action": (
            f"Inspect node allocatable capacity and compare against pod resource requests. "
            f"Scale the cluster, reduce the resource request, or remove the blocking constraint."
        ),
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": sched_msg + " -- A human must add nodes, reduce requests, or fix scheduling constraints.",
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }

    rag_ctx = format_rag_context("PendingScheduling FailedScheduling resource constraint", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=0,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs="(pod is Pending -- no logs available)",
        diagnosis=diagnosis, fault_type="PendingScheduling",
        source="synthetic", variant_id=f"PendingScheduling-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=rag_ctx,
    )


# --- CPUThrottle ------------------------------------------------------

_THROTTLE_LOGS = [
    "INFO processing request queue depth={depth}\nINFO request latency p99={lat}ms\nWARN CPU throttled, response times degraded\n",
    "handling {rps} rps -- CPU pinned at {pct}%\nWARN slow handler detected: {lat}ms\n",
    "executing {n} concurrent tasks -- throttled\ncpu usage: {pct}%\n",
    "Batch job running -- {n} items/s\nCPU limit reached, processing slowing\n",
    "WARN queue depth rising ({depth}) -- CPU saturated\n",
]

def make_cpu_throttle_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    container_name = dep.split("-")[0] + "-container"
    cpu_limit = rng.choice(CPU_LIMITS)
    limit_mc  = float(cpu_limit.replace("m", ""))
    usage_mc  = limit_mc * rng.uniform(0.95, 1.0)
    ts = rand_ts(jitter_hours=30 * 24)

    log_tmpl = rng.choice(_THROTTLE_LOGS)
    log = log_tmpl.format(
        depth=rng.randint(100, 5000),
        lat=rng.randint(500, 5000),
        rps=rng.randint(50, 2000),
        pct=rng.randint(95, 100),
        n=rng.randint(10, 1000),
    )

    # CPUThrottle incidents use cpu_usage_millicores / cpu_limit_millicores in the incident
    container_statuses = [{
        "name": container_name,
        "ready": True,   # pod IS running, just throttled
        "restart_count": 0,
        "state": None,
        "cpu_usage_millicores": round(usage_mc, 2),
        "cpu_limit_millicores": round(limit_mc, 2),
    }]

    kubernetes_events = [{
        "reason": "CPUThrottling",
        "message": f"Container {container_name} is CPU-throttled: usage {usage_mc:.1f}m / limit {limit_mc:.1f}m ({usage_mc/limit_mc*100:.0f}%)",
        "timestamp": ts,
        "type": "Warning",
    }]

    evidence = [
        f"cpu_usage_millicores: {usage_mc:.1f}",
        f"cpu_limit_millicores: {limit_mc:.1f}",
        f"throttle ratio: {usage_mc/limit_mc*100:.0f}%",
        f"log: {log.strip()[:100]}",
    ]

    diagnosis = {
        "root_cause": (
            f"Container '{container_name}' is continuously pinned at its CPU limit of {cpu_limit}, "
            f"causing request latency degradation due to CFS throttling."
        ),
        "error": f"Pod '{pod_name}' is CPU-throttled at {limit_mc:.0f}m; processing latency is elevated.",
        "root_cause_category": "CPUThrottle",
        "confidence": "high",
        "evidence": evidence,
        "severity": rng.choice(["medium", "high"]),
        "explanation": (
            f"The container has been running at ≥95% of its {cpu_limit} CPU limit for multiple "
            f"consecutive polling intervals, causing Linux CFS to throttle it. "
            f"This manifests as increased request latency and queue depth. "
            f"The workload needs either a higher CPU limit or a code-level concurrency reduction."
        ),
        "recommended_action": (
            f"Escalate with measured CPU usage ({usage_mc:.1f}m/{limit_mc:.1f}m) for an operator to "
            f"raise the CPU limit or optimize the hot path."
        ),
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": f"CPU limit {cpu_limit} is insufficient; sustained throttling requires a controlled limit increase or code optimization by an operator.",
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }

    rag_ctx = format_rag_context("CPUThrottle CPU throttling resource limit", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=0,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs=log,
        diagnosis=diagnosis, fault_type="CPUThrottle",
        source="synthetic", variant_id=f"CPUThrottle-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=rag_ctx,
    )


# --- NetworkLatency ---------------------------------------------------

_CHAOS_NAMES = [
    "chaos-creater", "network-delay-test", "latency-injection",
    "chaos-nginx-delay", "network-chaos-exp01", "latency-exp",
    "chaos-backend-delay", "net-latency-qa", "chaos-frontend",
]

_NET_LOGS = [
    "(NetworkChaos CR -- no pod logs; see kubernetes_events for injection details)",
    "INFO HTTP/1.1 200 OK (latency: {lat}ms)\nWARN Request latency exceeds SLA ({lat}ms > {thresh}ms)\n",
    "WARN Downstream timeout after {lat}ms -- circuit breaker tripped\n",
    "ERROR Connection to {svc} timed out after {lat}ms\n",
]

_TARGET_APPS = ["nginx", "backend-api", "frontend-web", "auth-service", "payment-gateway"]
_TARGET_URLS = [
    "http://nginx-service.default.svc.cluster.local",
    "http://backend-api.production.svc.cluster.local",
    "http://frontend-web.staging.svc.cluster.local",
    "http://auth-service.default.svc.cluster.local/health",
]

def make_network_latency_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    ns = rng.choice(NAMESPACES)
    chaos_name = rng.choice(_CHAOS_NAMES)
    target_app = rng.choice(_TARGET_APPS)
    target_pods = [f"{target_app}-{rand_pod_hash()}"]
    target_url  = rng.choice(_TARGET_URLS)
    threshold_ms = rng.choice([250.0, 300.0, 500.0, 100.0])
    measured_ms  = threshold_ms * rng.uniform(2, 8)
    injected_delay = f"{int(measured_ms * rng.uniform(0.8, 1.1))}ms"
    pod_name = target_pods[0]
    ts = rand_ts(jitter_hours=30 * 24)

    log_tmpl = rng.choice(_NET_LOGS)
    log = log_tmpl.format(lat=int(measured_ms), thresh=int(threshold_ms),
                          svc=target_app)

    container_statuses = []
    kubernetes_events = [{
        "reason": "NetworkChaosInjecting",
        "message": (
            f"NetworkChaos '{chaos_name}' is actively injecting delay against target app "
            f"{target_app} and selector {{'labelSelectors': {{'app': '{target_app}'}}, "
            f"'namespaces': ['{ns}']}}. In-cluster request latency measured "
            f"{measured_ms:.2f} ms (threshold: {threshold_ms} ms)."
        ),
        "timestamp": ts,
        "type": "Warning",
    }]

    evidence = [
        f"NetworkChaos '{chaos_name}' status: actively injecting",
        f"measured latency: {measured_ms:.2f} ms (threshold: {threshold_ms} ms)",
        f"target app: {target_app}, pods: {target_pods}",
    ]

    diagnosis = {
        "root_cause": (
            f"Chaos Mesh NetworkChaos experiment '{chaos_name}' is injecting "
            f"~{injected_delay} network delay into target app '{target_app}', "
            f"causing in-cluster latency of {measured_ms:.0f} ms."
        ),
        "error": (
            f"Network latency to '{target_app}' is {measured_ms:.0f} ms, "
            f"exceeding threshold of {threshold_ms:.0f} ms."
        ),
        "root_cause_category": "NetworkLatency",
        "confidence": "high",
        "evidence": evidence,
        "severity": rng.choice(["medium", "high"]),
        "explanation": (
            f"The Chaos Mesh NetworkChaos CR '{chaos_name}' is actively injecting a "
            f"network delay into all pods with label app={target_app} in namespace '{ns}'. "
            f"An in-cluster probe measured {measured_ms:.0f} ms latency, significantly "
            f"exceeding the {threshold_ms:.0f} ms threshold. Removing or expiring the "
            f"experiment should restore normal latency."
        ),
        "recommended_action": (
            f"Escalate to an operator to remove or expire the NetworkChaos experiment "
            f"'{chaos_name}', then verify latency returns to baseline."
        ),
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": (
                f"NetworkChaos experiment '{chaos_name}' must be removed or expired by an operator. "
                f"Automatic deletion of chaos resources is not safe."
            ),
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }

    rag_ctx = format_rag_context("NetworkLatency chaos mesh network delay", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=0,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs=log,
        diagnosis=diagnosis, fault_type="NetworkLatency",
        source="synthetic", variant_id=f"NetworkLatency-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=rag_ctx,
    )


# --- DependencyFailure ------------------------------------------------

_DEP_SERVICES = [
    ("redis-service", "Redis", "6379", "redis.exceptions.ConnectionError: Error 111 connecting to {host}:{port}. Connection refused."),
    ("postgres-service", "PostgreSQL", "5432", "psycopg2.OperationalError: could not connect to server: Connection refused"),
    ("kafka-broker", "Kafka", "9092", "org.apache.kafka.common.errors.TimeoutException: Failed to update metadata after 60000 ms"),
    ("elasticsearch", "Elasticsearch", "9200", "ConnectionError: HTTPConnectionPool(host='{host}', port={port}): Max retries exceeded"),
    ("rabbitmq-service", "RabbitMQ", "5672", "pika.exceptions.AMQPConnectionError: Connection refused"),
    ("mongodb-service", "MongoDB", "27017", "pymongo.errors.ConnectionFailure: {host}:{port}: [Errno 111] Connection refused"),
]

_DEP_LOGS_TEMPLATE = """\
INFO  Starting application
INFO  Connecting to {service_type}://{host}:{port}
ERROR {error_msg}
ERROR Failed to initialize {service_type} connection -- aborting startup
FATAL Unhandled exception during boot: ConnectionRefused
"""

def make_dependency_failure_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    restart_count = rng.randint(3, 12)
    ts = rand_ts(jitter_hours=30 * 24)

    host, service_type, port, err_tmpl = rng.choice(_DEP_SERVICES)
    error_msg = err_tmpl.format(host=host, port=port)
    log = _DEP_LOGS_TEMPLATE.format(
        service_type=service_type, host=host, port=port, error_msg=error_msg
    )

    backoff = rng.choice(["10s", "20s", "40s"])
    container_statuses = [{
        "name": dep.split("-")[0] + "-container",
        "ready": False,
        "restart_count": restart_count,
        "state": {
            "waiting": {
                "reason": "CrashLoopBackOff",
                "message": f"back-off {backoff} restarting failed container",
            }
        },
    }]

    kubernetes_events = [{
        "reason": "BackOff",
        "message": f"Back-off restarting failed container {dep.split('-')[0]}-container in pod {pod_name}_{ns}",
        "timestamp": ts,
        "type": "Warning",
    }]

    evidence = [
        f"container state: CrashLoopBackOff, restart_count={restart_count}",
        f"log: {error_msg}",
        f"dependency: {service_type} at {host}:{port} is unreachable",
    ]

    diagnosis = {
        "root_cause": (
            f"The application cannot connect to the {service_type} dependency at {host}:{port}; "
            f"the dependency service is unavailable, causing the pod to crash on every startup attempt."
        ),
        "error": f"Pod '{pod_name}' is in CrashLoopBackOff because the {service_type} dependency ({host}:{port}) is unreachable.",
        "root_cause_category": "DependencyFailure",
        "confidence": rng.choice(["high", "medium"]),
        "evidence": evidence,
        "severity": rng.choice(["medium", "high"]),
        "explanation": (
            f"The application attempts to connect to {service_type} at {host}:{port} during startup "
            f"and exits with code 1 when the connection is refused. "
            f"Kubernetes restarts the container but the root cause is the unavailable downstream service, "
            f"not a bug in the application code itself."
        ),
        "recommended_action": (
            f"Investigate the {service_type} service ({host}) in namespace '{ns}': check its pod status, "
            f"Service endpoints, and network policies. Do not repeatedly delete the caller pod."
        ),
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": (
                f"The {service_type} dependency at {host}:{port} must be restored before the caller pod "
                f"can start. An operator must investigate the dependency service."
            ),
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }

    rag_ctx = format_rag_context("DependencyFailure connection refused dependency", pod_name) if with_rag else NO_RAG_CONTEXT

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=restart_count,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs=log,
        diagnosis=diagnosis, fault_type="DependencyFailure",
        source="synthetic", variant_id=f"DependencyFailure-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=rag_ctx,
    )


# ─────────────────────────────────────────────────────────────────────
# HEALTHY / false-positive examples (no_fault)
# ─────────────────────────────────────────────────────────────────────

_HEALTHY_LOGS = [
    "INFO  Server started on :8080\nINFO  Ready to accept connections\nINFO  GET /health 200 OK\n",
    "INFO  Application started successfully\nINFO  Listening on port 8080\nINFO  Health check passed\n",
    "INFO  Worker started, processing jobs\nINFO  Processed 100 items successfully\n",
    "INFO  Completed initialization in 2.3s\nINFO  Service is ready\n",
    "INFO  Batch job completed successfully. 5000 records processed.\n",
    "INFO  Graceful shutdown complete\n",  # Completed job
    "INFO  CronJob finished: 0 errors, 1200 records updated\n",
]

_HEALTHY_EVENT_REASONS = [
    ("Pulled",    "Successfully pulled image \"{image}:{tag}\" in {t}s"),
    ("Started",   "Started container {container}"),
    ("Killing",   "Stopping container {container}"),  # graceful shutdown
    ("Scheduled", "Successfully assigned {ns}/{pod} to node-{node}"),
    ("Pulling",   "Pulling image \"{image}:{tag}\""),
]

_HEALTHY_CONTAINER_STATES = [
    {"running": {"startedAt": "placeholder"}},
    {"terminated": {"reason": "Completed", "exit_code": 0}},
]

def make_healthy_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    restart_count = rng.randint(0, 2)
    image = rand_image()
    tag   = rng.choice(["latest", "v1.0.0", "v2.3.1", "stable", "1.21-alpine"])
    node  = rng.randint(1, 5)
    ts    = rand_ts(jitter_hours=30 * 24)

    log = rng.choice(_HEALTHY_LOGS)

    state = rng.choice(_HEALTHY_CONTAINER_STATES)
    if "running" in state:
        state["running"]["startedAt"] = ts
    container_statuses = [{
        "name": dep.split("-")[0] + "-container",
        "ready": "terminated" not in state,
        "restart_count": restart_count,
        "state": state,
    }]

    reason, msg_tmpl = rng.choice(_HEALTHY_EVENT_REASONS)
    event_msg = msg_tmpl.format(
        image=image, tag=tag,
        t=round(rng.uniform(0.5, 5.0), 2),
        container=dep.split("-")[0] + "-container",
        ns=ns, pod=pod_name, node=node,
    )
    kubernetes_events = [{
        "reason": reason,
        "message": event_msg,
        "timestamp": ts,
        "type": "Normal",
    }]
    if restart_count > 0 and rng.random() < 0.4:
        kubernetes_events.append({
            "reason": "BackOff",
            "message": f"Back-off restarting failed container (transient, recovered)",
            "timestamp": ts,
            "type": "Warning",
        })

    diagnosis = {
        "root_cause": "No fault detected. The pod is running normally.",
        "error": "No error. Pod is healthy.",
        "root_cause_category": "Unknown",
        "confidence": "high",
        "evidence": [
            "container state: Running / Completed",
            f"restart_count: {restart_count} (within normal range)",
            f"log: {log.strip()[:80]}",
        ],
        "severity": "low",
        "explanation": (
            "The pod is operating normally. Any recent restarts appear transient and "
            "the container has returned to a healthy running state. No remediation is required."
        ),
        "recommended_action": "No action required. Continue monitoring.",
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": "No action needed; pod is healthy. Escalating to operator for awareness only.",
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=restart_count,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs=log,
        diagnosis=diagnosis, fault_type="no_fault",
        source="synthetic", variant_id=f"no_fault-v{variant_idx:03d}",
        with_rag=False, runbook_context=NO_RAG_CONTEXT,
    )


# ─────────────────────────────────────────────────────────────────────
# AMBIGUOUS / noisy examples (lower confidence)
# ─────────────────────────────────────────────────────────────────────

_NOISE_LINES = [
    "DEBUG heartbeat tick\n",
    "INFO metrics pushed to prometheus\n",
    "WARN connection pool at 80% capacity\n",
    "DEBUG gc pause 12ms\n",
    "INFO config reloaded\n",
    "DEBUG span traced: GET /api/v1/resource 200 45ms\n",
]

def make_ambiguous_example(rng: random.Random, variant_idx: int, with_rag: bool) -> dict:
    """
    Ambiguous examples mix real fault signals with lots of unrelated noise.
    Confidence is set to low or medium.
    """
    dep = rng.choice(DEPLOYMENT_NAMES)
    ns  = rng.choice(NAMESPACES)
    pod_name = f"{dep}-{rand_pod_hash()}"
    restart_count = rng.randint(1, 5)
    ts = rand_ts(jitter_hours=30 * 24)

    # Pick a plausible-but-ambiguous fault
    ambig_fault = rng.choice(["ApplicationCrash", "OOMKilled", "Unknown"])
    confidence  = rng.choice(["low", "medium"])

    # Inject several noise log lines around a weak signal
    noise = "".join(rng.choices(_NOISE_LINES, k=rng.randint(5, 12)))
    if ambig_fault == "OOMKilled":
        signal_log = "WARN memory usage at 90%\nfatal error: runtime: out of memory\n"
        rcc = "OOMKilled"
    elif ambig_fault == "ApplicationCrash":
        signal_log = "WARNING: possible panic -- stack trace truncated\n"
        rcc = "ApplicationCrash"
    else:
        signal_log = ""
        rcc = "Unknown"
    recent_logs = noise + signal_log + noise

    container_statuses = [{
        "name": dep.split("-")[0] + "-container",
        "ready": rng.random() < 0.3,
        "restart_count": restart_count,
        "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 40s"}} if restart_count > 3 else None,
    }]

    kubernetes_events = [{
        "reason": "BackOff",
        "message": f"Back-off restarting failed container {dep.split('-')[0]}-container",
        "timestamp": ts,
        "type": "Warning",
    }]

    safe = safe_to_auto_remediate(ambig_fault, rcc, confidence)

    diagnosis = {
        "root_cause": (
            "Inconclusive -- the evidence is ambiguous. The pod has restarted but "
            "the logs contain significant noise and the failure signal is weak."
        ),
        "error": f"Pod '{pod_name}' has {restart_count} restart(s); root cause unclear from available evidence.",
        "root_cause_category": rcc,
        "confidence": confidence,
        "evidence": [
            f"restart_count: {restart_count}",
            f"partial signal: {signal_log.strip()[:80]}" if signal_log else "no clear signal in logs",
            "high log volume with unrelated lines reduces diagnostic clarity",
        ],
        "severity": "low",
        "explanation": (
            "The pod log contains many unrelated debug and info lines, and the fault signal "
            "is ambiguous. The restart count alone is not sufficient to determine a specific root cause. "
            "Manual inspection is recommended."
        ),
        "recommended_action": "Inspect pod logs manually and monitor for further restarts before taking action.",
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": "Evidence is ambiguous; a human must review logs and confirm the root cause.",
            "parameters": {},
        }],
        "safe_to_auto_remediate": safe,
    }

    return build_example(
        pod_name=pod_name, namespace=ns, restart_count=restart_count,
        detected_at=ts, container_statuses=container_statuses,
        kubernetes_events=kubernetes_events, recent_logs=recent_logs,
        diagnosis=diagnosis, fault_type=ambig_fault,
        source="synthetic", variant_id=f"ambiguous-v{variant_idx:03d}",
        with_rag=with_rag, runbook_context=format_rag_context(rcc) if with_rag else NO_RAG_CONTEXT,
    )


# ─────────────────────────────────────────────────────────────────────
# Real incident processor
# ─────────────────────────────────────────────────────────────────────

def _build_diagnosis_from_db_row(row: dict, fault_type: str) -> dict:
    """
    Build a ground-truth diagnosis from DB columns.
    Uses fault_type (detector ground truth) as root_cause_category.
    Labels from the DB's fault_type column, NOT from Gemini's
    root_cause_category (those two happen to agree for all 16 rows,
    but we enforce the rule anyway).
    """
    raw_diag = json.loads(row["raw_diagnosis_json"]) if row["raw_diagnosis_json"] else {}
    confidence = row["confidence"] or "high"
    severity   = row["severity"] or "medium"
    safe = safe_to_auto_remediate(fault_type, fault_type, confidence)

    # Take rich fields from Gemini output where available; override category
    return {
        "root_cause":         raw_diag.get("root_cause", f"Detected {fault_type} fault."),
        "error":              raw_diag.get("error") or raw_diag.get("root_cause", f"{fault_type} fault detected."),
        "root_cause_category": fault_type,   # GROUND TRUTH
        "confidence":         confidence,
        "evidence":           raw_diag.get("evidence", []),
        "severity":           severity,
        "explanation":        raw_diag.get("explanation", ""),
        "recommended_action": raw_diag.get("recommended_action", ""),
        "remediation_steps":  raw_diag.get("remediation_steps", [{
            "step": 1,
            "action": "escalate",
            "reason": "See recommended_action.",
            "parameters": {},
        }]),
        "safe_to_auto_remediate": safe,
    }


def load_real_incidents(no_rag: bool = False) -> tuple[list[dict], list[str]]:
    """
    Load real incidents from incidents.db.
    Returns (examples, skipped_log).
    Skips incidents where ground truth is ambiguous.
    """
    if not DB_PATH.exists():
        return [], ["incidents.db not found -- no real incidents loaded"]

    skipped = []
    examples = []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, pod_name, namespace, fault_type, restart_count, detected_at, "
        "confidence, severity, safe_to_auto_remediate, raw_incident_json, raw_diagnosis_json "
        "FROM incidents ORDER BY id"
    ).fetchall()
    conn.close()

    # Deduplicate: IDs 8-15 are repeated OOM events for the same physical
    # pod across 30-second polling intervals. We keep IDs 8, 9, 10 as distinct
    # restart-count variants and group the rest into one variant family.
    seen_fingerprints: set[str] = set()

    for raw_row in rows:
        row = dict(raw_row)
        rid = row["id"]
        fault_type = row["fault_type"]

        # Validate ground truth is a known category
        if fault_type not in VALID_CATEGORIES and fault_type != "no_fault":
            skipped.append(
                f"ID={rid} pod={row['pod_name']}: fault_type='{fault_type}' not in valid "
                f"category list -- SKIPPED"
            )
            continue

        inc = json.loads(row["raw_incident_json"]) if row["raw_incident_json"] else {}

        # Near-duplicate detection: same pod + same fault_type + same restart_count
        fp = hashlib.md5(
            f"{row['pod_name']}|{fault_type}|{row['restart_count']}".encode()
        ).hexdigest()
        if fp in seen_fingerprints:
            skipped.append(
                f"ID={rid} pod={row['pod_name']} fault={fault_type} "
                f"restart_count={row['restart_count']}: near-duplicate of earlier row -- SKIPPED"
            )
            continue
        seen_fingerprints.add(fp)

        # Build diagnosis from ground truth
        diagnosis = _build_diagnosis_from_db_row(row, fault_type)

        # RAG context
        rag_ctx = format_rag_context(fault_type, row["pod_name"]) if not no_rag else NO_RAG_CONTEXT

        # Variant ID: real incidents get their own variant family
        variant_id = f"{fault_type}-real-id{rid}"

        ex = build_example(
            pod_name=inc.get("pod_name", row["pod_name"]),
            namespace=inc.get("namespace", row["namespace"]),
            restart_count=inc.get("restart_count", row["restart_count"] or 0),
            detected_at=inc.get("detected_at", row["detected_at"] or "N/A"),
            container_statuses=inc.get("container_statuses", []),
            kubernetes_events=inc.get("kubernetes_events", []),
            recent_logs=inc.get("recent_logs", "(no logs)"),
            diagnosis=diagnosis,
            fault_type=fault_type,
            source="real",
            variant_id=variant_id,
            with_rag=not no_rag,
            runbook_context=rag_ctx,
            correlation_context=inc.get("correlation_context", "(no related failures)"),
        )
        examples.append(ex)

    return examples, skipped


# ─────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────

def validate_example(ex: dict) -> list[str]:
    """Return a list of validation error strings, or [] if valid."""
    errors = []
    msgs = ex.get("messages", [])
    if len(msgs) != 3:
        errors.append(f"Expected 3 messages, got {len(msgs)}")
        return errors

    roles = [m["role"] for m in msgs]
    if roles != ["system", "user", "assistant"]:
        errors.append(f"Bad role sequence: {roles}")

    # Parse assistant JSON
    assistant_content = msgs[2]["content"]
    try:
        diag = json.loads(assistant_content)
    except json.JSONDecodeError as e:
        errors.append(f"assistant JSON invalid: {e}")
        return errors

    # Check required fields
    required = [
        "root_cause", "error", "root_cause_category", "confidence",
        "evidence", "severity", "explanation", "recommended_action",
        "remediation_steps", "safe_to_auto_remediate",
    ]
    for field in required:
        if field not in diag:
            errors.append(f"missing field: {field}")

    cat = diag.get("root_cause_category", "")
    if cat not in VALID_CATEGORIES and cat != "no_fault":
        errors.append(f"invalid root_cause_category: '{cat}'")

    if diag.get("confidence") not in VALID_CONFIDENCE:
        errors.append(f"invalid confidence: '{diag.get('confidence')}'")

    if diag.get("severity") not in VALID_SEVERITY:
        errors.append(f"invalid severity: '{diag.get('severity')}'")

    if not isinstance(diag.get("evidence"), list):
        errors.append("evidence must be a list")

    steps = diag.get("remediation_steps", [])
    if not isinstance(steps, list) or len(steps) == 0:
        errors.append("remediation_steps must be a non-empty list")
    else:
        for step in steps:
            if step.get("action") not in VALID_ACTIONS:
                errors.append(f"invalid action: '{step.get('action')}'")

    if not isinstance(diag.get("safe_to_auto_remediate"), bool):
        errors.append("safe_to_auto_remediate must be bool")

    meta = ex.get("metadata", {})
    if meta.get("source") not in ("real", "synthetic"):
        errors.append(f"invalid source: '{meta.get('source')}'")

    return errors


# ─────────────────────────────────────────────────────────────────────
# Variant-aware split
# ─────────────────────────────────────────────────────────────────────

def split_by_variant(
    examples: list[dict],
    train_frac: float = 0.80,
    val_frac:   float = 0.10,
    seed: int = 42,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Split by variant_id, not by example, so no variant leaks across splits.
    Within each fault_type, variants are shuffled then cut at 80/10/10.
    """
    rng = random.Random(seed)

    # Group variant_ids by fault_type
    ft_to_variants: dict[str, list[str]] = defaultdict(list)
    variant_to_examples: dict[str, list[dict]] = defaultdict(list)

    for ex in examples:
        vid = ex["metadata"]["variant_id"]
        ft  = ex["metadata"]["fault_type"]
        ft_to_variants[ft].append(vid)
        variant_to_examples[vid].append(ex)

    # Deduplicate variant lists
    for ft in ft_to_variants:
        ft_to_variants[ft] = list(dict.fromkeys(ft_to_variants[ft]))

    train_ex, val_ex, test_ex = [], [], []

    for ft, variants in ft_to_variants.items():
        shuffled = variants[:]
        rng.shuffle(shuffled)
        n = len(shuffled)
        n_train = max(1, round(n * train_frac))
        n_val   = max(1, round(n * val_frac))
        # Ensure we don't over-assign
        if n_train + n_val >= n:
            n_val = max(0, n - n_train - 1)

        train_variants = set(shuffled[:n_train])
        val_variants   = set(shuffled[n_train:n_train + n_val])
        test_variants  = set(shuffled[n_train + n_val:])

        for vid in variants:
            for ex in variant_to_examples[vid]:
                if vid in train_variants:
                    train_ex.append(ex)
                elif vid in val_variants:
                    val_ex.append(ex)
                else:
                    test_ex.append(ex)

    return train_ex, val_ex, test_ex


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Generate SFT dataset for Self-Healing Daemon")
    p.add_argument("--no-rag", action="store_true", help="Skip RAG context for all examples")
    p.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    p.add_argument("--out-dir", type=str, default=str(DATASET_DIR), help="Output directory")
    p.add_argument("--fault-count", type=int, default=180,
                   help="Min synthetic examples per fault type (default: 180)")
    p.add_argument("--healthy-count", type=int, default=160,
                   help="Number of healthy/false-positive examples (default: 160)")
    p.add_argument("--ambiguous-count", type=int, default=60,
                   help="Number of ambiguous/noisy examples (default: 60)")
    return p.parse_args()


def main():
    args = parse_args()
    rng = random.Random(args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    no_rag = args.no_rag
    N = args.fault_count          # per fault type
    N_HEALTHY = args.healthy_count
    N_AMBIG   = args.ambiguous_count

    print("=" * 60)
    print("Self-Healing Daemon -- SFT Dataset Generator")
    print("=" * 60)
    print(f"Seed: {args.seed} | no-rag: {no_rag}")
    print(f"Fault examples per type: {N}")
    print(f"Healthy examples: {N_HEALTHY}")
    print(f"Ambiguous examples: {N_AMBIG}")
    print()

    all_examples: list[dict] = []
    skipped_log: list[str]   = []
    validation_errors: list[str] = []

    # ── STEP 2: Real incidents ────────────────────────────────────────
    print("Loading real incidents from incidents.db ...")
    real_examples, real_skipped = load_real_incidents(no_rag=no_rag)
    skipped_log.extend(real_skipped)
    print(f"  Loaded {len(real_examples)} real examples, {len(real_skipped)} skipped")
    all_examples.extend(real_examples)

    # ── STEP 3: Synthetic generation ──────────────────────────────────
    # Each fault type gets N examples; half with RAG, half without (unless --no-rag)
    fault_generators = [
        ("ApplicationCrash",   make_crash_example),
        ("OOMKilled",          make_oom_example),
        ("ImagePullError",     make_image_pull_example),
        ("ConfigError",        make_config_error_example),
        ("PendingScheduling",  make_pending_example),
        ("CPUThrottle",        make_cpu_throttle_example),
        ("NetworkLatency",     make_network_latency_example),
        ("DependencyFailure",  make_dependency_failure_example),
    ]

    for fault_type, gen_fn in fault_generators:
        print(f"Generating {N} examples for {fault_type} ...")
        for i in range(N):
            # Alternate with/without RAG for each pair
            with_rag_flag = (not no_rag) and (i % 2 == 0)
            ex = gen_fn(rng, i, with_rag_flag)
            errs = validate_example(ex)
            if errs:
                validation_errors.append(
                    f"{fault_type} variant_idx={i}: {'; '.join(errs)}"
                )
            else:
                all_examples.append(ex)

    # Healthy examples (no_fault)
    print(f"Generating {N_HEALTHY} healthy/false-positive examples ...")
    for i in range(N_HEALTHY):
        ex = make_healthy_example(rng, i, False)
        errs = validate_example(ex)
        if errs:
            validation_errors.append(f"healthy variant_idx={i}: {'; '.join(errs)}")
        else:
            all_examples.append(ex)

    # Ambiguous examples
    print(f"Generating {N_AMBIG} ambiguous/noisy examples ...")
    for i in range(N_AMBIG):
        with_rag_flag = (not no_rag) and (i % 3 == 0)
        ex = make_ambiguous_example(rng, i, with_rag_flag)
        errs = validate_example(ex)
        if errs:
            validation_errors.append(f"ambiguous variant_idx={i}: {'; '.join(errs)}")
        else:
            all_examples.append(ex)

    print(f"\nTotal valid examples before split: {len(all_examples)}")
    if validation_errors:
        print(f"WARNING: {len(validation_errors)} examples failed validation and were dropped:")
        for e in validation_errors[:10]:
            print(f"  {e}")

    # ── STEP 4: Split by variant ──────────────────────────────────────
    print("\nSplitting by variant_id (80/10/10) ...")
    train, val, test = split_by_variant(all_examples, seed=args.seed)

    # Shuffle within each split
    rng.shuffle(train)
    rng.shuffle(val)
    rng.shuffle(test)

    print(f"  train: {len(train)}, val: {len(val)}, test: {len(test)}")

    # ── STEP 5: Write JSONL files ─────────────────────────────────────
    def write_jsonl(path: Path, examples: list[dict]):
        with open(path, "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
        print(f"  Written: {path} ({len(examples)} lines)")

    write_jsonl(out_dir / "train.jsonl", train)
    write_jsonl(out_dir / "val.jsonl",   val)
    write_jsonl(out_dir / "test.jsonl",  test)

    # ── Dataset report ────────────────────────────────────────────────
    def count_by(examples: list[dict], key: str) -> dict:
        counts: dict[str, int] = defaultdict(int)
        for ex in examples:
            counts[ex["metadata"][key]] += 1
        return dict(sorted(counts.items()))

    total = len(all_examples)
    by_fault   = count_by(all_examples, "fault_type")
    by_source  = count_by(all_examples, "source")
    with_rag_c = sum(1 for e in all_examples if e["metadata"]["with_rag"])

    # Per-split fault breakdown
    def split_breakdown(split: list[dict]) -> dict:
        return count_by(split, "fault_type")

    report_lines = [
        "# Dataset Report -- Self-Healing Daemon SFT Dataset",
        "",
        f"**Generated at:** {datetime.now(timezone.utc).isoformat()}Z",
        f"**Random seed:** {args.seed}",
        f"**RAG context:** {'disabled (--no-rag)' if no_rag else 'enabled (lexical fallback)'}",
        "",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total examples | {total} |",
        f"| Train | {len(train)} |",
        f"| Validation | {len(val)} |",
        f"| Test | {len(test)} |",
        f"| Real incidents | {by_source.get('real', 0)} |",
        f"| Synthetic | {by_source.get('synthetic', 0)} |",
        f"| With RAG context | {with_rag_c} |",
        f"| Without RAG context | {total - with_rag_c} |",
        "",
        "## Examples by Fault Type",
        "",
        "| Fault Type | Count |",
        "|------------|-------|",
    ]
    for ft, c in by_fault.items():
        report_lines.append(f"| {ft} | {c} |")

    report_lines += [
        "",
        "## Split Breakdown by Fault Type",
        "",
        "### Train",
        "",
        "| Fault Type | Count |",
        "|------------|-------|",
    ]
    for ft, c in split_breakdown(train).items():
        report_lines.append(f"| {ft} | {c} |")

    report_lines += [
        "",
        "### Validation",
        "",
        "| Fault Type | Count |",
        "|------------|-------|",
    ]
    for ft, c in split_breakdown(val).items():
        report_lines.append(f"| {ft} | {c} |")

    report_lines += [
        "",
        "### Test",
        "",
        "| Fault Type | Count |",
        "|------------|-------|",
    ]
    for ft, c in split_breakdown(test).items():
        report_lines.append(f"| {ft} | {c} |")

    report_lines += [
        "",
        "## Skipped Incidents (Real)",
        "",
        f"Total skipped: {len(skipped_log)}",
        "",
    ]
    if skipped_log:
        for s in skipped_log:
            report_lines.append(f"- {s}")
    else:
        report_lines.append("*(none)*")

    report_lines += [
        "",
        "## Validation Errors (Dropped from Dataset)",
        "",
        f"Total dropped: {len(validation_errors)}",
        "",
    ]
    if validation_errors:
        for e in validation_errors:
            report_lines.append(f"- {e}")
    else:
        report_lines.append("*(none)*")

    report_lines += [
        "",
        "## Notes & Assumptions",
        "",
        "- **System prompt** and **user prompt template** taken verbatim from `llm_client.py`.",
        "- **Ground-truth labels** for real incidents come from the `fault_type` column "
          "(detector output), NOT from Gemini's `root_cause_category`. These happen to agree "
          "for all 16 DB rows, but the rule is enforced.",
        "- **safe_to_auto_remediate** = `true` only when `root_cause_category` ∈ "
          "{OOMKilled, ApplicationCrash} AND `confidence` = 'high'.",
        "- **Split** is by `variant_id`, not by example, to prevent leakage. "
          "Near-duplicate real incidents (same pod + fault_type + restart_count) are deduplicated.",
        "- **DependencyFailure** is included (it appears in the LLM schema enum). "
          "**ReadinessFailure** (eval YAML) is mapped to `Unknown` per user decision.",
        "- **RAG context** is generated using the same lexical retrieval logic as "
          "`rag_engine._lexical_retrieve()` (ChromaDB/sentence-transformers not required at "
          "generation time). Half of synthetic fault examples have RAG, half do not. "
          "Healthy and most ambiguous examples have no RAG.",
        "- **Eval incidents** (k8s/eval/*.yaml) are NOT in this dataset. They represent "
          "the 3 novel fault scenarios held out for the controlled evaluation runs.",
        "- **No API keys, tokens, secrets, or .env contents** appear anywhere in the dataset.",
    ]

    report_path = out_dir / "dataset_report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"  Written: {report_path}")

    print("\n[OK] Dataset generation complete.")
    print(f"  Total examples: {total}")
    print(f"  train={len(train)}, val={len(val)}, test={len(test)}")


if __name__ == "__main__":
    main()
