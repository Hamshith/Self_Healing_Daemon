"""
eval/analyze_results.py — Statistical analysis of harness output.

Reads the JSONL file produced by eval/harness.py and computes:

  H1 — Diagnosis accuracy
       Per-method per-scenario accuracy (fraction of trials where
       predicted_category == ground_truth_category), with 95% CI
       via Wilson score interval.  Also overall accuracy and a
       confusion matrix per method.

  H2 — Remediation quality
       Unsafe-action rate (fraction of trials where recommended action
       is not in the allow-list), auto-remediate-safe rate.

  H3 — Novel fault accuracy
       Same accuracy metrics split by is_novel_fault = True/False.
       The expected pattern: baselines ≈ 0% on novel, LLM methods > 0%.

  H4 — Cost
       Median and p95 latency (ms), total and per-incident API cost.

Usage
─────
    python -m eval.analyze_results eval/results/run_001.jsonl
    python -m eval.analyze_results eval/results/run_001.jsonl --format md
    python -m eval.analyze_results eval/results/run_001.jsonl --format csv
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional


# ── Statistical helpers ───────────────────────────────────────────────────

def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    95% Wilson score confidence interval for a proportion.
    Returns (lower, upper) as fractions ∈ [0, 1].
    """
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    sorted_v = sorted(values)
    idx = (pct / 100) * (len(sorted_v) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (idx - lo)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return float("nan")
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


# ── Data loading ──────────────────────────────────────────────────────────

def load_results(path: str) -> tuple[dict, list[dict]]:
    """Return (meta_dict, list_of_result_dicts) from a JSONL harness output."""
    meta = {}
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "meta":
                meta = obj
            else:
                rows.append(obj)
    return meta, rows


# ── H1: Diagnosis accuracy ────────────────────────────────────────────────

def h1_accuracy(rows: list[dict]) -> dict:
    """
    Per-method accuracy table.

    Returns {method: {"n": int, "correct": int, "accuracy": float,
                       "ci_lo": float, "ci_hi": float,
                       "by_scenario": {scenario: {...}}}}
    """
    by_method: dict = defaultdict(lambda: {"n": 0, "correct": 0, "by_scenario": defaultdict(lambda: {"n": 0, "correct": 0})})

    for row in rows:
        m = row["method"]
        s = row["fault_scenario"]
        by_method[m]["n"] += 1
        by_method[m]["by_scenario"][s]["n"] += 1
        if row["category_correct"]:
            by_method[m]["correct"] += 1
            by_method[m]["by_scenario"][s]["correct"] += 1

    result = {}
    for method, d in by_method.items():
        n, c = d["n"], d["correct"]
        lo, hi = wilson_ci(c, n)
        result[method] = {
            "n": n,
            "correct": c,
            "accuracy": c / n if n else 0,
            "ci_lo": lo,
            "ci_hi": hi,
            "by_scenario": {
                s: {
                    "n": sd["n"],
                    "correct": sd["correct"],
                    "accuracy": sd["correct"] / sd["n"] if sd["n"] else 0,
                }
                for s, sd in d["by_scenario"].items()
            },
        }
    return result


def h1_confusion(rows: list[dict], method: str) -> dict[str, dict[str, int]]:
    """
    Build a confusion matrix for *method*:
      { actual_category: { predicted_category: count } }
    """
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        if row["method"] != method:
            continue
        matrix[row["ground_truth_category"]][row["predicted_category"]] += 1
    return {k: dict(v) for k, v in matrix.items()}


# ── H2: Remediation quality ───────────────────────────────────────────────

def h2_remediation(rows: list[dict]) -> dict:
    """
    Per-method remediation quality.

    Returns {method: {"unsafe_action_rate": float, "auto_safe_rate": float,
                       "action_distribution": {action: count}}}
    """
    by_method: dict = defaultdict(lambda: {"n": 0, "unsafe": 0, "auto_safe": 0, "actions": defaultdict(int)})

    for row in rows:
        m = row["method"]
        by_method[m]["n"] += 1
        by_method[m]["actions"][row["recommended_action"]] += 1
        if row.get("unsafe_action"):
            by_method[m]["unsafe"] += 1
        if row.get("safe_to_auto_remediate"):
            by_method[m]["auto_safe"] += 1

    return {
        method: {
            "unsafe_action_rate": d["unsafe"] / d["n"] if d["n"] else 0,
            "auto_safe_rate":     d["auto_safe"] / d["n"] if d["n"] else 0,
            "action_distribution": dict(d["actions"]),
        }
        for method, d in by_method.items()
    }


# ── H3: Novel fault accuracy ──────────────────────────────────────────────

def h3_novel(rows: list[dict]) -> dict:
    """
    Accuracy split by is_novel_fault.

    Returns {method: {"known": {...accuracy fields}, "novel": {...}}}
    """
    by_method: dict = defaultdict(lambda: {
        True:  {"n": 0, "correct": 0},
        False: {"n": 0, "correct": 0},
    })
    for row in rows:
        m = row["method"]
        novel = bool(row.get("is_novel_fault"))
        by_method[m][novel]["n"] += 1
        if row["category_correct"]:
            by_method[m][novel]["correct"] += 1

    result = {}
    for method, splits in by_method.items():
        out: dict = {}
        for is_novel, label in [(False, "known"), (True, "novel")]:
            n = splits[is_novel]["n"]
            c = splits[is_novel]["correct"]
            lo, hi = wilson_ci(c, n)
            out[label] = {"n": n, "correct": c,
                           "accuracy": c / n if n else 0,
                           "ci_lo": lo, "ci_hi": hi}
        result[method] = out
    return result


# ── H4: Cost ─────────────────────────────────────────────────────────────

def h4_cost(rows: list[dict]) -> dict:
    """
    Per-method latency (ms) and API cost statistics.

    Returns {method: {"latency_median_ms", "latency_p95_ms", "latency_mean_ms",
                       "latency_std_ms", "api_cost_total_usd",
                       "api_cost_per_incident_usd", "n"}}
    """
    by_method: dict = defaultdict(lambda: {"latencies": [], "costs": [], "n": 0})
    for row in rows:
        m = row["method"]
        by_method[m]["latencies"].append(row["latency_ms"])
        by_method[m]["costs"].append(row["api_cost_usd"])
        by_method[m]["n"] += 1

    return {
        method: {
            "n": d["n"],
            "latency_median_ms":       _percentile(d["latencies"], 50),
            "latency_p95_ms":          _percentile(d["latencies"], 95),
            "latency_mean_ms":         _mean(d["latencies"]),
            "latency_std_ms":          _std(d["latencies"]),
            "api_cost_total_usd":      sum(d["costs"]),
            "api_cost_per_incident_usd": _mean(d["costs"]),
        }
        for method, d in by_method.items()
    }


# ── Formatting ────────────────────────────────────────────────────────────

def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def format_markdown(meta: dict, h1: dict, h2: dict, h3: dict, h4: dict,
                    confusion_matrices: dict) -> str:
    lines: list[str] = []

    lines.append("# Evaluation Results\n")
    if meta:
        lines.append(f"**Model**: `{meta.get('model', 'N/A')}`  ")
        lines.append(f"**Trials per scenario**: {meta.get('n_trials', 'N/A')}  ")
        lines.append(f"**Methods**: {', '.join(meta.get('methods', []))}  ")
        lines.append(f"**Run started**: {meta.get('started_at', 'N/A')}\n")

    # H1 table
    lines.append("## H1 — Diagnosis Accuracy\n")
    lines.append("| Method | N | Correct | Accuracy | 95% CI |")
    lines.append("|--------|---|---------|----------|--------|")
    for method in sorted(h1):
        d = h1[method]
        ci = f"[{_pct(d['ci_lo'])}, {_pct(d['ci_hi'])}]"
        lines.append(f"| {method} | {d['n']} | {d['correct']} | {_pct(d['accuracy'])} | {ci} |")

    lines.append("\n### Per-scenario accuracy\n")
    # Collect all scenario names
    all_scenarios = sorted({s for d in h1.values() for s in d["by_scenario"]})
    hdr = "| Method | " + " | ".join(all_scenarios) + " |"
    sep = "|--------|" + "|".join(["--------"] * len(all_scenarios)) + "|"
    lines.append(hdr)
    lines.append(sep)
    for method in sorted(h1):
        cells = []
        for s in all_scenarios:
            sd = h1[method]["by_scenario"].get(s, {})
            acc = sd.get("accuracy", float("nan"))
            cells.append(_pct(acc) if not math.isnan(acc) else "—")
        lines.append(f"| {method} | " + " | ".join(cells) + " |")

    # H2 table
    lines.append("\n## H2 — Remediation Quality\n")
    lines.append("| Method | Unsafe-action rate | Auto-safe rate |")
    lines.append("|--------|-------------------|----------------|")
    for method in sorted(h2):
        d = h2[method]
        lines.append(f"| {method} | {_pct(d['unsafe_action_rate'])} | {_pct(d['auto_safe_rate'])} |")

    # H3 table
    lines.append("\n## H3 — Novel Fault Accuracy\n")
    lines.append("| Method | Known accuracy | Novel accuracy | Delta |")
    lines.append("|--------|---------------|----------------|-------|")
    for method in sorted(h3):
        d = h3[method]
        known_acc = d.get("known", {}).get("accuracy", float("nan"))
        novel_acc = d.get("novel", {}).get("accuracy", float("nan"))
        delta = novel_acc - known_acc if not (math.isnan(known_acc) or math.isnan(novel_acc)) else float("nan")
        lines.append(
            f"| {method} | {_pct(known_acc)} | {_pct(novel_acc)} | "
            f"{'+' if delta >= 0 else ''}{_pct(delta) if not math.isnan(delta) else '—'} |"
        )

    # H4 table
    lines.append("\n## H4 — Cost & Latency\n")
    lines.append("| Method | Median latency (ms) | p95 latency (ms) | Mean cost / incident (USD) | Total cost (USD) |")
    lines.append("|--------|---------------------|------------------|---------------------------|-----------------|")
    for method in sorted(h4):
        d = h4[method]
        lines.append(
            f"| {method} | {d['latency_median_ms']:.1f} | {d['latency_p95_ms']:.1f} | "
            f"${d['api_cost_per_incident_usd']:.6f} | ${d['api_cost_total_usd']:.4f} |"
        )

    # Confusion matrices
    lines.append("\n## Confusion Matrices\n")
    for method, matrix in confusion_matrices.items():
        lines.append(f"### {method}\n")
        all_labels = sorted({k for row in matrix.values() for k in row} | set(matrix.keys()))
        hdr2 = "| actual \\ predicted | " + " | ".join(all_labels) + " |"
        sep2 = "|---------------------|" + "|".join(["---"] * len(all_labels)) + "|"
        lines.append(hdr2)
        lines.append(sep2)
        for actual in all_labels:
            row_data = matrix.get(actual, {})
            cells2 = [str(row_data.get(pred, 0)) for pred in all_labels]
            lines.append(f"| {actual} | " + " | ".join(cells2) + " |")
        lines.append("")

    return "\n".join(lines)


def format_json_summary(meta: dict, h1: dict, h2: dict, h3: dict, h4: dict) -> str:
    return json.dumps({
        "meta": meta,
        "h1_accuracy": h1,
        "h2_remediation": h2,
        "h3_novel": h3,
        "h4_cost": h4,
    }, indent=2)


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Analyze Self-Healing Daemon evaluation results"
    )
    p.add_argument("input", help="Path to JSONL results file from eval/harness.py")
    p.add_argument(
        "--format", choices=["md", "json", "both"], default="md",
        help="Output format (default: md)",
    )
    p.add_argument(
        "--output", default=None,
        help="Write output to this file instead of stdout",
    )
    args = p.parse_args(argv)

    meta, rows = load_results(args.input)
    if not rows:
        print("[analyze] No result rows found in input file.", file=sys.stderr)
        sys.exit(1)

    print(f"[analyze] Loaded {len(rows)} result rows from {args.input}")

    h1 = h1_accuracy(rows)
    h2 = h2_remediation(rows)
    h3 = h3_novel(rows)
    h4 = h4_cost(rows)
    confusion = {m: h1_confusion(rows, m) for m in h1}

    if args.format in ("md", "both"):
        md = format_markdown(meta, h1, h2, h3, h4, confusion)
        if args.output:
            out_path = args.output if args.format == "md" else args.output.replace(".md", "_summary.md")
            Path(out_path).write_text(md, encoding="utf-8")
            print(f"[analyze] Markdown report written → {out_path}")
        else:
            print(md)

    if args.format in ("json", "both"):
        js = format_json_summary(meta, h1, h2, h3, h4)
        if args.output:
            out_path = args.output if args.format == "json" else args.output.replace(".jsonl", "_summary.json")
            Path(out_path).write_text(js, encoding="utf-8")
            print(f"[analyze] JSON summary written → {out_path}")
        else:
            print(js)


if __name__ == "__main__":
    main()
