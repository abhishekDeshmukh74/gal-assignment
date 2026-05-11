"""Visualise benchmark samples produced by:
    python3 scripts/benchmark.py --runs 3 --save-samples data/samples.jsonl

Usage:
    python3 scripts/visualize.py data/samples.jsonl
    python3 scripts/visualize.py data/samples.jsonl --out reports/

Requires: matplotlib, pandas (pip install matplotlib pandas)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import pandas as pd
except ImportError as exc:
    raise SystemExit("Missing dependencies. Run:\n  pip install matplotlib pandas") from exc

STAGE_COLS = [
    "sql_generation_ms",
    "sql_validation_ms",
    "sql_execution_ms",
    "answer_generation_ms",
]
STAGE_LABELS = ["SQL gen", "Validation", "Execution", "Answer gen"]
STATUS_COLORS = {
    "success": "#4caf50",
    "invalid_sql": "#f44336",
    "unanswerable": "#ff9800",
    "error": "#9c27b0",
}


def load(path: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return pd.DataFrame(rows)


def save(fig: plt.Figure, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / name
    fig.savefig(p, dpi=150, bbox_inches="tight")
    print(f"  Saved {p}")


def plot_latency_distribution(df: pd.DataFrame, out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(df["total_ms"], bins=20, color="#1976d2", edgecolor="white", linewidth=0.4)
    for p, color, label in [
        (df["total_ms"].quantile(0.50), "#ff9800", "p50"),
        (df["total_ms"].quantile(0.95), "#f44336", "p95"),
    ]:
        ax.axvline(p, color=color, linewidth=1.6, linestyle="--", label=f"{label} {p:.0f} ms")
    ax.set_xlabel("Total latency (ms)")
    ax.set_ylabel("Requests")
    ax.set_title("Request latency distribution")
    ax.legend(fontsize=9)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    fig.tight_layout()
    save(fig, out_dir, "latency_distribution.png")
    plt.close(fig)


def plot_stage_breakdown(df: pd.DataFrame, out_dir: Path) -> None:
    means = [df[c].mean() for c in STAGE_COLS]
    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(STAGE_LABELS, means, color=["#1976d2", "#43a047", "#fb8c00", "#8e24aa"])
    ax.bar_label(bars, fmt="%.0f ms", padding=3, fontsize=8)
    ax.set_ylabel("Average time (ms)")
    ax.set_title("Average time per pipeline stage")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    fig.tight_layout()
    save(fig, out_dir, "stage_breakdown.png")
    plt.close(fig)


def plot_status_breakdown(df: pd.DataFrame, out_dir: Path) -> None:
    counts = df["status"].value_counts()
    colors = [STATUS_COLORS.get(s, "#90a4ae") for s in counts.index]
    fig, ax = plt.subplots(figsize=(5, 5))
    wedges, texts, autotexts = ax.pie(
        counts.values,
        labels=counts.index,
        colors=colors,
        autopct="%1.1f%%",
        startangle=140,
        wedgeprops={"linewidth": 0.8, "edgecolor": "white"},
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.set_title("Request status breakdown")
    fig.tight_layout()
    save(fig, out_dir, "status_breakdown.png")
    plt.close(fig)


def plot_tokens_vs_latency(df: pd.DataFrame, out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 4))
    color_map = [STATUS_COLORS.get(s, "#90a4ae") for s in df["status"]]
    ax.scatter(df["total_tokens"], df["total_ms"], c=color_map, alpha=0.7, s=40, edgecolors="none")
    ax.set_xlabel("Total tokens")
    ax.set_ylabel("Total latency (ms)")
    ax.set_title("Tokens vs latency (coloured by status)")
    # legend patches
    from matplotlib.patches import Patch

    seen = df["status"].unique()
    legend_elements = [Patch(facecolor=STATUS_COLORS.get(s, "#90a4ae"), label=s) for s in seen]
    ax.legend(handles=legend_elements, fontsize=8)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    fig.tight_layout()
    save(fig, out_dir, "tokens_vs_latency.png")
    plt.close(fig)


def plot_latency_over_time(df: pd.DataFrame, out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 4))
    for run_id, group in df.groupby("run"):
        ax.plot(
            range(len(group)),
            group["total_ms"].values,
            marker="o",
            markersize=3,
            linewidth=1,
            label=f"Run {run_id}",
        )
    ax.set_xlabel("Prompt index within run")
    ax.set_ylabel("Total latency (ms)")
    ax.set_title("Latency per prompt across runs")
    ax.legend(fontsize=8)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    fig.tight_layout()
    save(fig, out_dir, "latency_over_time.png")
    plt.close(fig)


def plot_stacked_stages(df: pd.DataFrame, out_dir: Path) -> None:
    """Stacked bar: each request's latency broken down by stage."""
    sample_df = df.head(min(30, len(df))).copy().reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(12, 4))
    bottom = pd.Series([0.0] * len(sample_df))
    palette = ["#1976d2", "#43a047", "#fb8c00", "#8e24aa"]
    for col, label, color in zip(STAGE_COLS, STAGE_LABELS, palette, strict=True):
        ax.bar(sample_df.index, sample_df[col], bottom=bottom, label=label, color=color, width=0.8)
        bottom = bottom + sample_df[col]
    ax.set_xlabel("Sample index")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("Per-request stage breakdown (first 30 samples)")
    ax.legend(fontsize=8, loc="upper right")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    fig.tight_layout()
    save(fig, out_dir, "stacked_stages.png")
    plt.close(fig)


def print_summary(df: pd.DataFrame) -> None:
    print(f"\n{'─'*48}")
    print(f"  Samples          : {len(df)}")
    print(f"  Success rate     : {(df['status']=='success').mean()*100:.1f}%")
    print(f"  avg latency      : {df['total_ms'].mean():,.0f} ms")
    print(f"  p50 latency      : {df['total_ms'].quantile(.50):,.0f} ms")
    print(f"  p95 latency      : {df['total_ms'].quantile(.95):,.0f} ms")
    print(f"  avg tokens/req   : {df['total_tokens'].mean():,.0f}")
    print(f"  avg calls/req    : {df['llm_calls'].mean():.2f}")
    print(f"{'─'*48}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Visualise benchmark JSONL samples.")
    parser.add_argument("samples", help="Path to the JSONL file from benchmark.py --save-samples")
    parser.add_argument(
        "--out", default="reports", help="Output directory for PNG charts (default: reports/)"
    )
    args = parser.parse_args()

    samples_path = Path(args.samples)
    if not samples_path.exists():
        raise SystemExit(
            f"File not found: {samples_path}\n"
            "Run first:\n"
            "  OPENROUTER_API_KEY=sk-or-... PYTHONPATH=. "
            "python3 scripts/benchmark.py --runs 3 --save-samples data/samples.jsonl"
        )

    out_dir = Path(args.out)
    df = load(samples_path)
    print_summary(df)

    print("Generating charts …")
    plot_latency_distribution(df, out_dir)
    plot_stage_breakdown(df, out_dir)
    plot_status_breakdown(df, out_dir)
    plot_tokens_vs_latency(df, out_dir)
    plot_latency_over_time(df, out_dir)
    plot_stacked_stages(df, out_dir)
    print(f"\nAll charts written to {out_dir}/")


if __name__ == "__main__":
    main()
