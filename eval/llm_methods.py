"""
eval/llm_methods.py — LLM-based methods: LLM-no-RAG and LLM+RAG.

Wraps the existing llm_client.diagnose_incident() with two configurations:
  • llm_no_rag: RAG context is replaced with a blank placeholder string so
    the LLM receives no runbook text.  This isolates what retrieval adds
    (H1–H2) over the LLM alone.
  • llm_rag:    Normal path — RAG retrieves the most relevant runbook chunks
    and injects them into the prompt.

Both paths use identical:
  • Model version (config.MODEL — fixed, never auto-updated during eval)
  • Temperature (Gemini default, or override via EVAL_TEMPERATURE env var)
  • System prompt
  • Structured incident fields

Token counting
──────────────
Google Gemini does not expose a per-call cost breakdown in the Python SDK, so
we estimate cost from the input + output token counts multiplied by the
published pricing for the configured model tier.  The estimate is logged and
stored in EvalResult.api_cost_usd for H4 comparison.

Pricing constants (update if you switch model tiers):
  gemini-2.5-flash:  input $0.075 / 1M tokens, output $0.30 / 1M tokens
  gemini-1.5-flash:  input $0.075 / 1M tokens, output $0.30 / 1M tokens
"""
from __future__ import annotations

import os
import time
from typing import Optional

import config
import rag_engine

from eval.types import EvalResult, SAFE_ACTIONS

# ── Pricing constants ─────────────────────────────────────────────────────
# Rates in USD per 1 000 000 tokens.
_INPUT_COST_PER_1M  = float(os.getenv("EVAL_INPUT_COST_PER_1M",  "0.075"))
_OUTPUT_COST_PER_1M = float(os.getenv("EVAL_OUTPUT_COST_PER_1M", "0.30"))


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * _INPUT_COST_PER_1M + output_tokens * _OUTPUT_COST_PER_1M) / 1_000_000


def _call_llm(incident: dict) -> tuple[dict, float, int, int]:
    """
    Call Gemini and return (diagnosis_dict, latency_ms, input_tokens, output_tokens).

    Uses the existing llm_client infrastructure so retry logic, JSON parsing,
    and fallback are shared with the daemon.
    """
    from google import genai
    from google.genai import types as genai_types
    from google.api_core.exceptions import ResourceExhausted
    import json, re

    import llm_client  # noqa: F401 — for SYSTEM_PROMPT / USER_PROMPT_TEMPLATE

    user_prompt = llm_client._build_user_prompt(incident)

    t0 = time.perf_counter()
    client_obj = genai.Client(api_key=config.GEMINI_API_KEY)

    temperature = float(os.getenv("EVAL_TEMPERATURE", "0.0"))

    def _call(prompt: str):
        return client_obj.models.generate_content(
            model=config.MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=llm_client.SYSTEM_PROMPT,
                max_output_tokens=config.MAX_TOKENS,
                temperature=temperature,
            ),
        )

    try:
        resp = _call(user_prompt)
    except ResourceExhausted:
        print("[eval/llm] Rate-limited — waiting 30 s …")
        time.sleep(30)
        resp = _call(user_prompt)

    latency_ms = (time.perf_counter() - t0) * 1000

    # Token usage — Gemini SDK returns usage_metadata on the response
    usage = getattr(resp, "usage_metadata", None)
    input_tokens  = getattr(usage, "prompt_token_count",     0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0

    raw = resp.text
    cleaned = llm_client._strip_markdown_fences(raw)

    try:
        diagnosis = json.loads(cleaned)
    except (json.JSONDecodeError, Exception):
        # One retry with explicit JSON request
        retry_prompt = user_prompt + (
            "\n\nIMPORTANT: Your previous response was not valid JSON. "
            "Please respond with valid JSON only. No markdown, no extra text."
        )
        try:
            resp2 = _call(retry_prompt)
            usage2 = getattr(resp2, "usage_metadata", None)
            input_tokens  += getattr(usage2, "prompt_token_count",     0) or 0
            output_tokens += getattr(usage2, "candidates_token_count", 0) or 0
            diagnosis = json.loads(llm_client._strip_markdown_fences(resp2.text))
        except Exception:
            diagnosis = {
                "root_cause": "Unable to parse LLM response",
                "error": "JSON parse failed after retry",
                "root_cause_category": "Unknown",
                "confidence": "low",
                "evidence": [],
                "severity": "medium",
                "explanation": "LLM did not return valid JSON.",
                "recommended_action": "Manually inspect the pod.",
                "remediation_steps": [{"step": 1, "action": "escalate",
                                       "reason": "Parse failure.", "parameters": {}}],
                "safe_to_auto_remediate": False,
            }

    return diagnosis, latency_ms, input_tokens, output_tokens


def _eval_result_from_diagnosis(
    method: str,
    diagnosis: dict,
    incident: dict,
    trial_index: int,
    ground_truth_category: str,
    is_novel: bool,
    latency_ms: float,
    input_tokens: int,
    output_tokens: int,
    retrieval_mode: str,
) -> EvalResult:
    steps = diagnosis.get("remediation_steps") or []
    first_action = "escalate"
    if steps and isinstance(steps[0], dict):
        first_action = steps[0].get("action", "escalate")

    return EvalResult(
        method=method,
        fault_scenario=ground_truth_category,
        trial_index=trial_index,
        predicted_category=diagnosis.get("root_cause_category", "Unknown"),
        ground_truth_category=ground_truth_category,
        recommended_action=first_action,
        safe_to_auto_remediate=bool(diagnosis.get("safe_to_auto_remediate", False)),
        is_safe_action=(first_action in SAFE_ACTIONS),
        is_novel_fault=is_novel,
        latency_ms=latency_ms,
        api_cost_usd=_estimate_cost(input_tokens, output_tokens),
        confidence=diagnosis.get("confidence", "N/A"),
        root_cause=diagnosis.get("root_cause", ""),
        explanation=diagnosis.get("explanation", ""),
        retrieval_mode=retrieval_mode,
    )


# ── LLM without RAG ───────────────────────────────────────────────────────

def diagnose_no_rag(incident: dict, trial_index: int, ground_truth_category: str,
                    is_novel: bool) -> EvalResult:
    """
    Run the LLM on *incident* with the runbook_context field set to the
    empty placeholder string.

    Injecting `runbook_context` directly into the incident dict before
    calling _build_user_prompt() bypasses rag_engine.retrieve() entirely —
    the LLM gets NO runbook text, only the raw pod signals.

    This is the cleanest way to isolate retrieval's contribution: the prompt
    template, model, temperature, and all other fields are identical.
    """
    # Stamp the incident with a blank runbook context so _build_user_prompt
    # skips the RAG call.
    incident_no_rag = {
        **incident,
        "runbook_context": "(no runbook context — ablation study: LLM without RAG)",
    }
    diagnosis, latency_ms, in_tok, out_tok = _call_llm(incident_no_rag)

    return _eval_result_from_diagnosis(
        method="llm_no_rag",
        diagnosis=diagnosis,
        incident=incident,
        trial_index=trial_index,
        ground_truth_category=ground_truth_category,
        is_novel=is_novel,
        latency_ms=latency_ms,
        input_tokens=in_tok,
        output_tokens=out_tok,
        retrieval_mode="none (ablation)",
    )


# ── LLM with RAG ─────────────────────────────────────────────────────────

def diagnose_rag(incident: dict, trial_index: int, ground_truth_category: str,
                 is_novel: bool) -> EvalResult:
    """
    Run the LLM on *incident* with RAG context retrieved from the runbook index.

    The RAG query is built from pod_name + logs keywords, deliberately
    EXCLUDING the detector fault_type string so that retrieval accuracy is
    not inflated by the LLM simply matching the fault name in the runbook
    heading.

    Concretely: the query is constructed from the first 200 chars of recent_logs
    and any kubernetes_events reason strings.  The fault_type label from the
    detector is withheld from the RAG query.
    """
    # Build a query from log content and event reasons, NOT from fault_type.
    # This is the control that prevents a reviewer from saying
    # "retrieval just matches the fault name in the runbook."
    logs_snippet = str(incident.get("recent_logs", ""))[:200]
    event_reasons = " ".join(
        ev.get("reason", "") for ev in (incident.get("kubernetes_events") or [])
    )
    rag_query = f"{logs_snippet} {event_reasons}".strip() or incident.get("pod_name", "incident")

    # Retrieve runbook context without revealing the fault_type label.
    chunks = rag_engine.retrieve(rag_query, top_k=3)
    runbook_context = rag_engine.format_context(chunks)
    retrieval_mode = rag_engine.get_retrieval_mode()

    incident_with_rag = {
        **incident,
        "runbook_context": runbook_context,
    }
    diagnosis, latency_ms, in_tok, out_tok = _call_llm(incident_with_rag)

    return _eval_result_from_diagnosis(
        method="llm_rag",
        diagnosis=diagnosis,
        incident=incident,
        trial_index=trial_index,
        ground_truth_category=ground_truth_category,
        is_novel=is_novel,
        latency_ms=latency_ms,
        input_tokens=in_tok,
        output_tokens=out_tok,
        retrieval_mode=retrieval_mode,
    )
