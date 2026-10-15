"""
Google Gemini integration module.
Builds a structured prompt from incident signals and parses
the JSON diagnosis returned by the LLM.
"""

import json
import re
import time
from google import genai
from google.genai import types
from google.api_core.exceptions import ResourceExhausted
import config
import rag_engine
import qwen_fallback


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


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ```) that LLMs often wrap around JSON."""
    # Match ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _build_user_prompt(incident_data: dict) -> str:
    """Render the user prompt template with incident signals."""
    query = " ".join(
        str(value)
        for value in (
            incident_data.get("pod_name", ""),
            incident_data.get("fault_type", ""),
            incident_data.get("root_cause", ""),
        )
        if value
    )
    runbook_context = incident_data.get("runbook_context")
    if runbook_context is None:
        print(
            f"[agent] Requesting RAG context for "
            f"{incident_data.get('fault_type', 'unknown')}"
        )
        runbook_context = rag_engine.format_context(rag_engine.retrieve(query))
    else:
        print("[agent] Using incident-provided runbook context")
    return USER_PROMPT_TEMPLATE.format(
        pod_name=incident_data["pod_name"],
        namespace=incident_data["namespace"],
        restart_count=incident_data["restart_count"],
        detected_at=incident_data.get("detected_at", "N/A"),
        container_statuses=json.dumps(
            incident_data.get("container_statuses", []), indent=2
        ),
        kubernetes_events=json.dumps(
            incident_data.get("kubernetes_events", []), indent=2
        ),
        recent_logs=incident_data.get("recent_logs", "(no logs available)"),
        runbook_context=runbook_context,
        correlation_context=json.dumps(
            incident_data.get("correlation_context", "(no related failures)"),
            indent=2,
        ),
    )


def _call_gemini(user_prompt: str) -> str:
    """Send prompt to Google Gemini and return raw text response."""
    print(f"[agent] Calling Gemini model: {config.MODEL}")
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=config.MAX_TOKENS,
        ),
    )
    print("[agent] Gemini response received")
    if not isinstance(response.text, str) or not response.text.strip():
        raise ValueError("Gemini returned an empty response.")
    return response.text


def initialize_fallback_model() -> None:
    """Load the resident Qwen fallback before daemon polling begins."""
    qwen_fallback.load_model()


def _parse_diagnosis(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Gemini returned an empty response.")
    diagnosis = json.loads(_strip_markdown_fences(raw))
    if not isinstance(diagnosis, dict):
        raise ValueError("Gemini response was not a JSON object.")
    return diagnosis


def _use_qwen_fallback(incident_data: dict, reason: Exception) -> dict:
    print(
        f"[llm_client] Gemini diagnosis failed ({reason}); "
        "using fine-tuned Qwen 3B fallback."
    )
    diagnosis = qwen_fallback.diagnose(incident_data)
    print("[llm_client] Fine-tuned Qwen 3B fallback diagnosis received.")
    return diagnosis


def diagnose_incident(incident_data: dict) -> dict:
    """
    Build a prompt from *incident_data*, call Gemini, and return
    the parsed JSON diagnosis dict.

    Gemini rate limits and invalid JSON are retried once. If Gemini still
    fails to return a valid JSON diagnosis, the resident fine-tuned Qwen model
    is used instead.
    """
    print(
        f"[agent] Diagnosing incident: pod={incident_data.get('pod_name', 'unknown')} "
        f"fault={incident_data.get('fault_type', 'unknown')}"
    )

    try:
        user_prompt = _build_user_prompt(incident_data)
        try:
            raw = _call_gemini(user_prompt)
        except ResourceExhausted:
            print("[llm_client] Rate-limited by Gemini — waiting 30 s …")
            time.sleep(30)
            raw = _call_gemini(user_prompt)

        try:
            diagnosis = _parse_diagnosis(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            retry_prompt = (
                user_prompt
                + "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Please respond with valid JSON only. No markdown, no extra text."
            )
            try:
                raw = _call_gemini(retry_prompt)
            except ResourceExhausted:
                print("[llm_client] Rate-limited on retry — waiting 30 s …")
                time.sleep(30)
                raw = _call_gemini(retry_prompt)
            diagnosis = _parse_diagnosis(raw)

        print(
            f"[agent] Diagnosis parsed: category={diagnosis.get('root_cause_category')} "
            f"severity={diagnosis.get('severity')}"
        )
        return diagnosis
    except Exception as exc:
        return _use_qwen_fallback(incident_data, exc)