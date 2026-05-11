from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.gaming_csv_to_db import (
    DEFAULT_CSV_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_TABLE_NAME,
    csv_to_sqlite,
)
from src.pipeline import AnalyticsPipeline


def _ensure_gaming_db() -> Path:
    """Ensure gaming mental health DB exists; create from CSV if missing."""
    if not DEFAULT_DB_PATH.exists():
        csv_to_sqlite(DEFAULT_CSV_PATH, DEFAULT_DB_PATH, DEFAULT_TABLE_NAME, if_exists="replace")
    return DEFAULT_DB_PATH


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runs",
        type=int,
        default=int(os.getenv("BENCHMARK_RUNS", "3")),
        help="Number of full prompt-set repetitions (default: $BENCHMARK_RUNS or 3).",
    )
    parser.add_argument(
        "--save-samples",
        metavar="FILE",
        default=None,
        help="Write per-sample JSONL to this path for later visualisation.",
    )
    args = parser.parse_args()

    db_path = _ensure_gaming_db()
    root = Path(__file__).resolve().parents[1]
    prompts_path = root / "tests" / "public_prompts.json"

    pipeline = AnalyticsPipeline(db_path=db_path)
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))

    totals: list[float] = []
    success = 0
    count = 0
    total_tokens = 0
    total_calls = 0
    samples: list[dict] = []

    for run_idx in range(args.runs):
        for prompt in prompts:
            result = pipeline.run(prompt)
            total_ms = result.timings["total_ms"]
            tokens = int(result.total_llm_stats.get("total_tokens", 0))
            calls = int(result.total_llm_stats.get("llm_calls", 0))
            totals.append(total_ms)
            success += int(result.status == "success")
            total_tokens += tokens
            total_calls += calls
            count += 1
            samples.append(
                {
                    "run": run_idx,
                    "prompt": prompt[:80],
                    "status": result.status,
                    "total_ms": round(total_ms, 2),
                    "sql_generation_ms": round(result.timings.get("sql_generation_ms", 0), 2),
                    "sql_validation_ms": round(result.timings.get("sql_validation_ms", 0), 2),
                    "sql_execution_ms": round(result.timings.get("sql_execution_ms", 0), 2),
                    "answer_generation_ms": round(result.timings.get("answer_generation_ms", 0), 2),
                    "total_tokens": tokens,
                    "llm_calls": calls,
                }
            )

    summary = {
        "runs": args.runs,
        "samples": count,
        "success_rate": round(success / count, 4) if count else 0.0,
        "avg_ms": round(statistics.fmean(totals), 2) if totals else 0.0,
        "p50_ms": round(percentile(totals, 50), 2),
        "p95_ms": round(percentile(totals, 95), 2),
        "avg_tokens_per_request": round(total_tokens / count, 2) if count else 0.0,
        "avg_llm_calls_per_request": round(total_calls / count, 2) if count else 0.0,
    }
    print(json.dumps(summary, indent=2))

    if args.save_samples:
        out = Path(args.save_samples)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for s in samples:
                fh.write(json.dumps(s) + "\n")
        print(f"\nSamples saved to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
