"""Local, evaluation-only inference for the fine-tuned Qwen LoRA adapters."""
from __future__ import annotations

import gc
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from eval.llm_methods import _eval_result_from_diagnosis
from eval.types import EvalResult, VALID_CATEGORIES

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MODEL_SPECS = {
    "finetuned_3b": {
        "base_model": "Qwen/Qwen2.5-3B-Instruct",
        "adapter": _PROJECT_ROOT / "finetuning" / "qwen-k8s-diagnosis-3b" / "final",
    },
    "finetuned_7b": {
        "base_model": "Qwen/Qwen2.5-7B-Instruct",
        "adapter": _PROJECT_ROOT / "finetuning" / "qwen-k8s-diagnosis-7b" / "checkpoint-252",
    },
}

_ACTIVE_METHOD: str | None = None
_ACTIVE_MODEL: Any = None
_ACTIVE_TOKENIZER: Any = None
_TORCH: Any = None


def get_local_model_metadata(methods: list[str]) -> dict[str, dict[str, str]]:
    """Describe selected local base models and adapter checkpoints for run logs."""
    return {
        method: {
            "base_model": _MODEL_SPECS[method]["base_model"],
            "adapter": (
                Path(_MODEL_SPECS[method]["adapter"])
                .relative_to(_PROJECT_ROOT)
                .as_posix()
            ),
            "quantization": "4-bit NF4",
        }
        for method in methods
    }


def _load_model(method: str) -> tuple[Any, Any]:
    global _ACTIVE_METHOD, _ACTIVE_MODEL, _ACTIVE_TOKENIZER, _TORCH

    spec = _MODEL_SPECS[method]
    adapter_path = Path(spec["adapter"])
    if not adapter_path.is_dir():
        raise FileNotFoundError(
            f"Fine-tuned adapter for {method} was not found: {adapter_path}"
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
            "Local fine-tuned evaluation requires PyTorch, Transformers, "
            "Accelerate, PEFT, and bitsandbytes. Install the project's "
            "evaluation dependencies and a CUDA-enabled PyTorch build."
        ) from exc

    if not torch.cuda.is_available():
        raise RuntimeError(
            "Local fine-tuned evaluation requires a visible CUDA GPU. "
            "Check the NVIDIA driver and CUDA-enabled PyTorch installation."
        )

    release_local_model()
    requested_gpu_mib = int(os.getenv("EVAL_LOCAL_GPU_MEMORY_MIB", "3072"))
    reserve_gpu_mib = int(os.getenv("EVAL_LOCAL_GPU_RESERVE_MIB", "512"))
    if requested_gpu_mib <= 0 or reserve_gpu_mib < 0:
        raise ValueError(
            "EVAL_LOCAL_GPU_MEMORY_MIB must be positive and "
            "EVAL_LOCAL_GPU_RESERVE_MIB must be non-negative."
        )

    gpu_index = torch.cuda.current_device()
    free_bytes, total_bytes = torch.cuda.mem_get_info(gpu_index)
    free_mib = free_bytes // (1024 * 1024)
    gpu_budget_mib = min(requested_gpu_mib, free_mib - reserve_gpu_mib)
    if gpu_budget_mib < 512:
        raise RuntimeError(
            f"Only {free_mib} MiB of GPU memory is free; at least 512 MiB "
            "must remain available to load a local model. Close GPU-heavy "
            "applications or reduce EVAL_LOCAL_GPU_RESERVE_MIB."
        )

    print(
        f"[eval/local] Loading {method} ({spec['base_model']}) in 4-bit; "
        f"GPU budget={gpu_budget_mib} MiB of "
        f"{total_bytes // (1024 * 1024)} MiB, with automatic CPU placement."
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
        adapter_path,
        use_fast=True,
        trust_remote_code=False,
    )
    base_model = AutoModelForCausalLM.from_pretrained(
        spec["base_model"],
        quantization_config=quantization_config,
        torch_dtype=torch.float16,
        device_map="auto",
        max_memory=max_memory,
        low_cpu_mem_usage=True,
        trust_remote_code=False,
    )
    model = PeftModel.from_pretrained(
        base_model,
        adapter_path,
        is_trainable=False,
    )
    model.eval()

    _ACTIVE_METHOD = method
    _ACTIVE_MODEL = model
    _ACTIVE_TOKENIZER = tokenizer
    _TORCH = torch
    return model, tokenizer


def _get_model(method: str) -> tuple[Any, Any]:
    if method not in _MODEL_SPECS:
        raise ValueError(f"Unknown local evaluation method: {method}")
    if _ACTIVE_METHOD != method or _ACTIVE_MODEL is None:
        return _load_model(method)
    return _ACTIVE_MODEL, _ACTIVE_TOKENIZER


def release_local_model() -> None:
    """Unload the active local model so another adapter can use the GPU."""
    global _ACTIVE_METHOD, _ACTIVE_MODEL, _ACTIVE_TOKENIZER, _TORCH

    _ACTIVE_MODEL = None
    _ACTIVE_TOKENIZER = None
    _ACTIVE_METHOD = None
    gc.collect()
    if _TORCH is not None and _TORCH.cuda.is_available():
        _TORCH.cuda.empty_cache()
    _TORCH = None


def _parse_response(raw_response: str) -> dict:
    import llm_client

    cleaned = llm_client._strip_markdown_fences(raw_response)
    try:
        diagnosis = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            diagnosis, _ = json.JSONDecoder().raw_decode(cleaned.lstrip())
        except json.JSONDecodeError:
            diagnosis = None

    if isinstance(diagnosis, dict):
        return diagnosis

    category_match = re.search(
        r'"root_cause_category"\s*:\s*"([^"]+)"',
        cleaned,
    )
    partial_category = category_match.group(1) if category_match else None
    if partial_category in VALID_CATEGORIES:
        return {
            "root_cause": "Recovered category from incomplete model output.",
            "root_cause_category": partial_category,
            "confidence": "low",
            "explanation": (
                "The model response was truncated or malformed; only the "
                "category was recovered. Remediation requires human review."
            ),
            "remediation_steps": [
                {
                    "step": 1,
                    "action": "escalate",
                    "reason": "The model response was incomplete and could not be validated.",
                    "parameters": {},
                }
            ],
            "safe_to_auto_remediate": False,
        }

    return {
        "root_cause": "The fine-tuned model did not return a JSON diagnosis.",
        "root_cause_category": "Unknown",
        "confidence": "low",
        "explanation": "The response was not a JSON object in the expected format.",
        "remediation_steps": [
            {
                "step": 1,
                "action": "escalate",
                "reason": "The model response could not be parsed safely.",
                "parameters": {},
            }
        ],
        "safe_to_auto_remediate": False,
    }


def diagnose_local(
    method: str,
    incident: dict,
    trial_index: int,
    ground_truth_category: str,
    is_novel: bool,
) -> EvalResult:
    import llm_client

    model, tokenizer = _get_model(method)
    no_rag_incident = {
        **incident,
        "runbook_context": "(no runbook context — local fine-tuned model evaluation)",
    }
    user_prompt = llm_client._build_user_prompt(no_rag_incident)
    user_prompt += (
        "\n\nLOCAL EVALUATION OUTPUT REQUIREMENTS:\n"
        "- Return exactly one complete JSON object and stop immediately after its closing brace.\n"
        "- Keep root_cause, error, explanation, and each evidence item concise.\n"
        "- Include exactly one evidence item and exactly one remediation step.\n"
        "- Do not add markdown, commentary, or special tokens after the JSON object."
    )
    encoded_prompt = tokenizer.apply_chat_template(
        [
            {"role": "system", "content": llm_client.SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
    )
    input_ids = encoded_prompt["input_ids"]
    full_input_ids = input_ids

    max_input_tokens = int(os.getenv("EVAL_LOCAL_MAX_INPUT_TOKENS", "2048"))
    if max_input_tokens < 512:
        raise ValueError("EVAL_LOCAL_MAX_INPUT_TOKENS must be at least 512.")
    if input_ids.shape[-1] > max_input_tokens:
        head_tokens = (max_input_tokens - 1) // 2
        tail_tokens = max_input_tokens - head_tokens - 1
        separator = input_ids.new_tensor([[tokenizer.eos_token_id]])
        input_ids = input_ids[:, :head_tokens]
        input_ids = _TORCH.cat(
            [
                input_ids,
                separator,
                full_input_ids[:, -tail_tokens:],
            ],
            dim=-1,
        )

    input_device = model.get_input_embeddings().weight.device
    input_ids = input_ids.to(input_device)
    max_new_tokens = int(os.getenv("EVAL_LOCAL_MAX_NEW_TOKENS", "384"))
    if max_new_tokens <= 0:
        raise ValueError("EVAL_LOCAL_MAX_NEW_TOKENS must be positive.")

    t0 = time.perf_counter()
    with _TORCH.inference_mode():
        output_ids = model.generate(
            input_ids=input_ids,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            use_cache=True,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    latency_ms = (time.perf_counter() - t0) * 1000

    generated_ids = output_ids[0, input_ids.shape[-1]:]
    raw_response = tokenizer.decode(generated_ids, skip_special_tokens=True)
    if os.getenv("EVAL_LOCAL_DEBUG_RAW", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        generated_token_count = generated_ids.shape[-1]
        eos_seen = (
            tokenizer.eos_token_id is not None
            and bool((generated_ids == tokenizer.eos_token_id).any().item())
        )
        print(
            f"[eval/local] Raw response for {method} "
            f"scenario={incident.get('eval_scenario', 'unknown')} "
            f"trial={trial_index}: generated_tokens={generated_token_count}, "
            f"max_new_tokens={max_new_tokens}, eos_seen={eos_seen}",
            flush=True,
        )
        print(f"[eval/local] BEGIN RAW RESPONSE\n{raw_response}\n"
              "[eval/local] END RAW RESPONSE", flush=True)

    diagnosis = _parse_response(raw_response)
    return _eval_result_from_diagnosis(
        method=method,
        diagnosis=diagnosis,
        incident=incident,
        trial_index=trial_index,
        ground_truth_category=ground_truth_category,
        is_novel=is_novel,
        latency_ms=latency_ms,
        input_tokens=0,
        output_tokens=0,
        retrieval_mode="none (local fine-tuned model)",
    )
