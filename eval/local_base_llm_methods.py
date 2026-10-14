"""Evaluation-only inference for untuned local Qwen base instruct models."""
from __future__ import annotations

import gc
import os
import time
from typing import Any

from eval.llm_methods import _eval_result_from_diagnosis
from eval.types import EvalResult

_MODEL_SPECS = {
    "base_3b": "Qwen/Qwen2.5-3B-Instruct",
    "base_7b": "Qwen/Qwen2.5-7B-Instruct",
}
_METHOD_TO_MODEL = {
    "base_3b": "base_3b",
    "base_3b_rag": "base_3b",
    "base_7b": "base_7b",
    "base_7b_rag": "base_7b",
}
BASE_LOCAL_METHODS = set(_METHOD_TO_MODEL)
BASE_RAG_METHODS = {"base_3b_rag", "base_7b_rag"}

_ACTIVE_MODEL_KEY: str | None = None
_ACTIVE_MODEL: Any = None
_ACTIVE_TOKENIZER: Any = None
_TORCH: Any = None


def get_local_model_metadata(methods: list[str]) -> dict[str, dict[str, str | bool]]:
    """Return the model and retrieval configuration recorded in run metadata."""
    return {
        method: {
            "base_model": _MODEL_SPECS[_METHOD_TO_MODEL[method]],
            "fine_tuned": False,
            "quantization": "4-bit NF4",
            "retrieval": "RAG" if method in BASE_RAG_METHODS else "none",
        }
        for method in methods
    }


def _load_model(model_key: str) -> tuple[Any, Any]:
    global _ACTIVE_MODEL_KEY, _ACTIVE_MODEL, _ACTIVE_TOKENIZER, _TORCH

    try:
        import torch
        from accelerate.utils import get_max_memory
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Untuned local-model evaluation requires PyTorch, Transformers, "
            "Accelerate, and bitsandbytes, plus a CUDA-enabled PyTorch build."
        ) from exc

    if not torch.cuda.is_available():
        raise RuntimeError(
            "Untuned local-model evaluation requires a visible CUDA GPU. "
            "Check the NVIDIA driver and CUDA-enabled PyTorch installation."
        )

    release_local_model()
    try:
        requested_gpu_mib = int(os.getenv("EVAL_LOCAL_GPU_MEMORY_MIB", "3072"))
        reserve_gpu_mib = int(os.getenv("EVAL_LOCAL_GPU_RESERVE_MIB", "512"))
    except ValueError as exc:
        raise ValueError(
            "EVAL_LOCAL_GPU_MEMORY_MIB and EVAL_LOCAL_GPU_RESERVE_MIB "
            "must be integers."
        ) from exc
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
            "must remain available to load a local model."
        )

    model_id = _MODEL_SPECS[model_key]
    print(
        f"[eval/local-base] Loading {model_id} in 4-bit; "
        f"GPU budget={gpu_budget_mib} MiB of "
        f"{total_bytes // (1024 * 1024)} MiB, with automatic CPU placement.",
        flush=True,
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
        model_id,
        use_fast=True,
        trust_remote_code=False,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=quantization_config,
        torch_dtype=torch.float16,
        device_map="auto",
        max_memory=max_memory,
        low_cpu_mem_usage=True,
        trust_remote_code=False,
    )
    model.eval()

    _ACTIVE_MODEL_KEY = model_key
    _ACTIVE_MODEL = model
    _ACTIVE_TOKENIZER = tokenizer
    _TORCH = torch
    return model, tokenizer


def _get_model(method: str) -> tuple[Any, Any]:
    if method not in _METHOD_TO_MODEL:
        raise ValueError(f"Unknown untuned local evaluation method: {method}")
    model_key = _METHOD_TO_MODEL[method]
    if _ACTIVE_MODEL_KEY != model_key or _ACTIVE_MODEL is None:
        return _load_model(model_key)
    return _ACTIVE_MODEL, _ACTIVE_TOKENIZER


def release_local_model() -> None:
    """Release the cached base model and clear unused CUDA allocations."""
    global _ACTIVE_MODEL_KEY, _ACTIVE_MODEL, _ACTIVE_TOKENIZER, _TORCH

    _ACTIVE_MODEL = None
    _ACTIVE_TOKENIZER = None
    _ACTIVE_MODEL_KEY = None
    gc.collect()
    if _TORCH is not None and _TORCH.cuda.is_available():
        _TORCH.cuda.empty_cache()
    _TORCH = None


def diagnose_local(
    method: str,
    incident: dict,
    trial_index: int,
    ground_truth_category: str,
    is_novel: bool,
) -> EvalResult:
    import llm_client

    model, tokenizer = _get_model(method)
    if method in BASE_RAG_METHODS:
        from eval.llm_methods import _retrieve_runbook_context

        runbook_context, retrieval_mode = _retrieve_runbook_context(incident)
    else:
        runbook_context = "(no runbook context — untuned local model evaluation)"
        retrieval_mode = "none (untuned local model)"

    model_incident = {**incident, "runbook_context": runbook_context}
    user_prompt = llm_client._build_user_prompt(model_incident)
    user_prompt += (
        "\n\nLOCAL EVALUATION OUTPUT REQUIREMENTS:\n"
        "- Return exactly one complete JSON object and stop after its closing brace.\n"
        "- Keep the diagnosis concise, with exactly one evidence item and one remediation step.\n"
        "- Do not add markdown or commentary outside the JSON object."
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
    max_input_tokens = int(os.getenv("EVAL_LOCAL_MAX_INPUT_TOKENS", "2048"))
    if max_input_tokens < 512:
        raise ValueError("EVAL_LOCAL_MAX_INPUT_TOKENS must be at least 512.")
    if input_ids.shape[-1] > max_input_tokens:
        head_tokens = (max_input_tokens - 1) // 2
        tail_tokens = max_input_tokens - head_tokens - 1
        input_ids = _TORCH.cat(
            [
                input_ids[:, :head_tokens],
                input_ids.new_tensor([[tokenizer.eos_token_id]]),
                input_ids[:, -tail_tokens:],
            ],
            dim=-1,
        )

    input_ids = input_ids.to(model.get_input_embeddings().weight.device)
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
        eos_seen = (
            tokenizer.eos_token_id is not None
            and bool((generated_ids == tokenizer.eos_token_id).any().item())
        )
        print(
            f"[eval/local-base] Raw response for {method} "
            f"scenario={incident.get('eval_scenario', 'unknown')} "
            f"trial={trial_index}: generated_tokens={generated_ids.shape[-1]}, "
            f"max_new_tokens={max_new_tokens}, eos_seen={eos_seen}",
            flush=True,
        )
        print(
            f"[eval/local-base] BEGIN RAW RESPONSE\n{raw_response}\n"
            "[eval/local-base] END RAW RESPONSE",
            flush=True,
        )

    from eval.local_llm_methods import _parse_response

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
        retrieval_mode=retrieval_mode,
    )
