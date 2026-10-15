"""Resident fine-tuned Qwen model used when Gemini cannot diagnose an incident."""

import json
import os
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent
_ADAPTER_PATH = (
    _PROJECT_ROOT / "finetuning" / "qwen-k8s-diagnosis-3b" / "final"
)
_BASE_MODEL = "Qwen/Qwen2.5-3B-Instruct"
_MODEL: Any = None
_TOKENIZER: Any = None
_TORCH: Any = None


def load_model() -> None:
    """Load the fine-tuned model once and keep it resident for daemon lifetime."""
    global _MODEL, _TOKENIZER, _TORCH

    if _MODEL is not None:
        return
    if not _ADAPTER_PATH.is_dir():
        raise FileNotFoundError(
            f"Fine-tuned Qwen 3B adapter was not found: {_ADAPTER_PATH}"
        )

    try:
        import torch
        from accelerate.utils import get_max_memory
        from peft import PeftModel
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
        )
    except ImportError as exc:
        raise RuntimeError(
            "The Qwen fallback requires PyTorch, Transformers, Accelerate, "
            "PEFT, and bitsandbytes."
        ) from exc

    if not torch.cuda.is_available():
        raise RuntimeError(
            "The Qwen fallback requires a visible CUDA GPU and a "
            "CUDA-enabled PyTorch installation."
        )

    requested_gpu_mib = int(os.getenv("QWEN_FALLBACK_GPU_MEMORY_MIB", "3072"))
    reserve_gpu_mib = int(os.getenv("QWEN_FALLBACK_GPU_RESERVE_MIB", "512"))
    if requested_gpu_mib <= 0 or reserve_gpu_mib < 0:
        raise ValueError(
            "QWEN_FALLBACK_GPU_MEMORY_MIB must be positive and "
            "QWEN_FALLBACK_GPU_RESERVE_MIB must be non-negative."
        )

    gpu_index = torch.cuda.current_device()
    free_bytes, total_bytes = torch.cuda.mem_get_info(gpu_index)
    free_mib = free_bytes // (1024 * 1024)
    gpu_budget_mib = min(requested_gpu_mib, free_mib - reserve_gpu_mib)
    if gpu_budget_mib < 512:
        raise RuntimeError(
            f"Only {free_mib} MiB of GPU memory is free; at least 512 MiB "
            "must remain available to load the Qwen fallback."
        )

    print(
        f"[qwen_fallback] Loading fine-tuned 3B model in 4-bit; "
        f"GPU budget={gpu_budget_mib} MiB of "
        f"{total_bytes // (1024 * 1024)} MiB."
    )
    max_memory = get_max_memory()
    max_memory[gpu_index] = f"{gpu_budget_mib}MiB"
    quantization_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        _ADAPTER_PATH,
        use_fast=True,
        trust_remote_code=False,
    )
    base_model = AutoModelForCausalLM.from_pretrained(
        _BASE_MODEL,
        quantization_config=quantization_config,
        torch_dtype=torch.float16,
        device_map="auto",
        max_memory=max_memory,
        low_cpu_mem_usage=True,
        trust_remote_code=False,
    )
    model = PeftModel.from_pretrained(
        base_model,
        _ADAPTER_PATH,
        is_trainable=False,
    )
    model.eval()

    _MODEL = model
    _TOKENIZER = tokenizer
    _TORCH = torch
    print("[qwen_fallback] Fine-tuned 3B model loaded and resident.")


def _escalation_diagnosis(reason: str, evidence: list[str] | None = None) -> dict:
    return {
        "root_cause": "The local fallback could not produce a valid diagnosis.",
        "error": reason,
        "root_cause_category": "Unknown",
        "confidence": "low",
        "evidence": evidence or [],
        "severity": "medium",
        "explanation": (
            "The fine-tuned Qwen fallback did not return a valid diagnosis. "
            "Manual inspection is required."
        ),
        "recommended_action": "Manually inspect the pod logs.",
        "remediation_steps": [{
            "step": 1,
            "action": "escalate",
            "reason": "The local fallback response could not be parsed safely.",
            "parameters": {},
        }],
        "safe_to_auto_remediate": False,
    }


def diagnose(incident_data: dict) -> dict:
    """Generate a no-RAG diagnosis using the already loaded fine-tuned model."""
    if _MODEL is None or _TOKENIZER is None or _TORCH is None:
        raise RuntimeError("Qwen fallback was not loaded during daemon startup.")

    import llm_client

    incident = {
        **incident_data,
        "runbook_context": "(no runbook context - Gemini fallback)",
    }
    prompt = llm_client._build_user_prompt(incident)
    prompt += (
        "\n\nLOCAL FALLBACK OUTPUT REQUIREMENTS:\n"
        "- Return one complete JSON object matching the requested schema.\n"
        "- Keep all descriptions and evidence concise.\n"
        "- Do not add markdown, commentary, or text after the JSON object."
    )
    encoded_prompt = _TOKENIZER.apply_chat_template(
        [
            {"role": "system", "content": llm_client.SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
    )
    input_ids = encoded_prompt["input_ids"]
    max_input_tokens = int(os.getenv("QWEN_FALLBACK_MAX_INPUT_TOKENS", "2048"))
    if max_input_tokens < 512:
        raise ValueError("QWEN_FALLBACK_MAX_INPUT_TOKENS must be at least 512.")
    if input_ids.shape[-1] > max_input_tokens:
        head_tokens = (max_input_tokens - 1) // 2
        tail_tokens = max_input_tokens - head_tokens - 1
        separator = input_ids.new_tensor([[_TOKENIZER.eos_token_id]])
        input_ids = _TORCH.cat(
            [
                input_ids[:, :head_tokens],
                separator,
                input_ids[:, -tail_tokens:],
            ],
            dim=-1,
        )

    input_ids = input_ids.to(_MODEL.get_input_embeddings().weight.device)
    max_new_tokens = int(os.getenv("QWEN_FALLBACK_MAX_NEW_TOKENS", "384"))
    if max_new_tokens <= 0:
        raise ValueError("QWEN_FALLBACK_MAX_NEW_TOKENS must be positive.")

    with _TORCH.inference_mode():
        output_ids = _MODEL.generate(
            input_ids=input_ids,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            use_cache=True,
            pad_token_id=_TOKENIZER.pad_token_id or _TOKENIZER.eos_token_id,
            eos_token_id=_TOKENIZER.eos_token_id,
        )
    raw_response = _TOKENIZER.decode(
        output_ids[0, input_ids.shape[-1]:],
        skip_special_tokens=True,
    )

    cleaned = llm_client._strip_markdown_fences(raw_response)
    try:
        diagnosis = json.loads(cleaned)
    except json.JSONDecodeError:
        print("[qwen_fallback] Response was not valid JSON; escalating.")
        return _escalation_diagnosis(
            "The fine-tuned Qwen model returned invalid JSON.",
            [raw_response[:500]],
        )

    if not isinstance(diagnosis, dict):
        print("[qwen_fallback] Response was not a JSON object; escalating.")
        return _escalation_diagnosis(
            "The fine-tuned Qwen model did not return a JSON object.",
            [raw_response[:500]],
        )
    if not isinstance(diagnosis.get("safe_to_auto_remediate"), bool):
        diagnosis["safe_to_auto_remediate"] = False
    steps = diagnosis.get("remediation_steps")
    if (
        not isinstance(steps, list)
        or not steps
        or any(not isinstance(step, dict) for step in steps)
    ):
        diagnosis["safe_to_auto_remediate"] = False
        diagnosis["remediation_steps"] = _escalation_diagnosis(
            "The response did not contain valid remediation steps."
        )["remediation_steps"]
    return diagnosis
