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

Respond with this exact JSON structure:
{{
  "root_cause": "one sentence describing the most likely root cause",
  "root_cause_category": "one of: ImagePullError | OOMKilled | ApplicationCrash | ConfigError | DependencyFailure | ResourceLimit | PendingScheduling | CPUThrottle | NetworkLatency | Unknown",
  "confidence": "one of: high | medium | low",
  "evidence": ["list", "of", "specific", "log lines", "or events that support this diagnosis"],
  "severity": "one of: critical | high | medium | low",
  "explanation": "2-3 sentences explaining what is happening and why",
  "recommended_action": "specific kubectl or config command to fix this",
  "safe_to_auto_remediate": false
}}
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
    )


def _call_gemini(user_prompt: str) -> str:
    """Send prompt to Google Gemini and return raw text response."""
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=config.MAX_TOKENS,
        ),
    )
    return response.text


def diagnose_incident(incident_data: dict) -> dict:
    """
    Build a prompt from *incident_data*, call Gemini, and return
    the parsed JSON diagnosis dict.

    Handles:
      - ResourceExhausted (rate limit) → wait 30 s and retry once
      - JSON parse failure             → retry once asking for valid JSON
    """
    user_prompt = _build_user_prompt(incident_data)

    # ── First attempt ──────────────────────────────────────────────
    try:
        raw = _call_gemini(user_prompt)
    except ResourceExhausted:
        print("[llm_client] Rate-limited by Gemini — waiting 30 s …")
        time.sleep(30)
        raw = _call_gemini(user_prompt)

    # ── Strip markdown fences and parse JSON ───────────────────────
    cleaned = _strip_markdown_fences(raw)
    try:
        diagnosis = json.loads(cleaned)
        return diagnosis
    except json.JSONDecodeError:
        pass  # will retry

    # ── Retry once, explicitly asking for valid JSON ───────────────
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

    cleaned = _strip_markdown_fences(raw)
    try:
        diagnosis = json.loads(cleaned)
        return diagnosis
    except json.JSONDecodeError:
        # Return a best-effort fallback so the daemon doesn't crash
        return {
            "root_cause": "Unable to parse LLM response",
            "root_cause_category": "Unknown",
            "confidence": "low",
            "evidence": [raw[:500]],
            "severity": "medium",
            "explanation": "The LLM did not return valid JSON after two attempts.",
            "recommended_action": "Manually inspect the pod logs.",
            "safe_to_auto_remediate": False,
        }