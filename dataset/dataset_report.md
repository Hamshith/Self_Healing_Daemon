# Dataset Report -- Self-Healing Daemon SFT Dataset

**Generated at:** 2026-10-06T14:01:28.126605+00:00Z
**Random seed:** 42
**RAG context:** enabled (lexical fallback)

## Summary

| Metric | Count |
|--------|-------|
| Total examples | 1676 |
| Train | 1342 |
| Validation | 167 |
| Test | 167 |
| Real incidents | 16 |
| Synthetic | 1660 |
| With RAG context | 756 |
| Without RAG context | 920 |

## Examples by Fault Type

| Fault Type | Count |
|------------|-------|
| ApplicationCrash | 209 |
| CPUThrottle | 181 |
| ConfigError | 182 |
| DependencyFailure | 180 |
| ImagePullError | 181 |
| NetworkLatency | 181 |
| OOMKilled | 201 |
| PendingScheduling | 180 |
| Unknown | 21 |
| no_fault | 160 |

## Split Breakdown by Fault Type

### Train

| Fault Type | Count |
|------------|-------|
| ApplicationCrash | 167 |
| CPUThrottle | 145 |
| ConfigError | 146 |
| DependencyFailure | 144 |
| ImagePullError | 145 |
| NetworkLatency | 145 |
| OOMKilled | 161 |
| PendingScheduling | 144 |
| Unknown | 17 |
| no_fault | 128 |

### Validation

| Fault Type | Count |
|------------|-------|
| ApplicationCrash | 21 |
| CPUThrottle | 18 |
| ConfigError | 18 |
| DependencyFailure | 18 |
| ImagePullError | 18 |
| NetworkLatency | 18 |
| OOMKilled | 20 |
| PendingScheduling | 18 |
| Unknown | 2 |
| no_fault | 16 |

### Test

| Fault Type | Count |
|------------|-------|
| ApplicationCrash | 21 |
| CPUThrottle | 18 |
| ConfigError | 18 |
| DependencyFailure | 18 |
| ImagePullError | 18 |
| NetworkLatency | 18 |
| OOMKilled | 20 |
| PendingScheduling | 18 |
| Unknown | 2 |
| no_fault | 16 |

## Skipped Incidents (Real)

Total skipped: 0

*(none)*

## Validation Errors (Dropped from Dataset)

Total dropped: 0

*(none)*

## Notes & Assumptions

- **System prompt** and **user prompt template** taken verbatim from `llm_client.py`.
- **Ground-truth labels** for real incidents come from the `fault_type` column (detector output), NOT from Gemini's `root_cause_category`. These happen to agree for all 16 DB rows, but the rule is enforced.
- **safe_to_auto_remediate** = `true` only when `root_cause_category` ∈ {OOMKilled, ApplicationCrash} AND `confidence` = 'high'.
- **Split** is by `variant_id`, not by example, to prevent leakage. Near-duplicate real incidents (same pod + fault_type + restart_count) are deduplicated.
- **DependencyFailure** is included (it appears in the LLM schema enum). **ReadinessFailure** (eval YAML) is mapped to `Unknown` per user decision.
- **RAG context** is generated using the same lexical retrieval logic as `rag_engine._lexical_retrieve()` (ChromaDB/sentence-transformers not required at generation time). Half of synthetic fault examples have RAG, half do not. Healthy and most ambiguous examples have no RAG.
- **Eval incidents** (k8s/eval/*.yaml) are NOT in this dataset. They represent the 3 novel fault scenarios held out for the controlled evaluation runs.
- **No API keys, tokens, secrets, or .env contents** appear anywhere in the dataset.