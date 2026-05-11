"""Generate a single self-contained interactive HTML dashboard from benchmark JSONL.

Usage:
    python3 scripts/gen_dashboard.py data/samples.jsonl
    python3 scripts/gen_dashboard.py data/samples.jsonl --out reports/dashboard.html

No extra Python deps — Plotly is loaded from CDN inside the HTML.
Open the output file directly in any browser.
"""

from __future__ import annotations

import argparse
import json
import statistics
import webbrowser
from pathlib import Path


def load(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def build_html(samples: list[dict]) -> str:
    total_ms = [s["total_ms"] for s in samples]
    statuses = [s["status"] for s in samples]
    tokens = [s["total_tokens"] for s in samples]
    calls = [s["llm_calls"] for s in samples]
    prompts = [s["prompt"] for s in samples]
    runs = [s["run"] for s in samples]

    stage_cols = [
        "sql_generation_ms",
        "sql_validation_ms",
        "sql_execution_ms",
        "answer_generation_ms",
    ]
    stage_labels = ["SQL Generation", "SQL Validation", "SQL Execution", "Answer Generation"]

    success_rate = statuses.count("success") / len(statuses) * 100
    avg_ms = statistics.mean(total_ms)
    p50 = pct(total_ms, 50)
    p95 = pct(total_ms, 95)
    avg_tokens = statistics.mean(tokens)
    avg_calls = statistics.mean(calls)

    # Status counts
    status_set = sorted(set(statuses))
    status_counts = {s: statuses.count(s) for s in status_set}

    # Stage averages
    stage_avgs = [statistics.mean(s[c] for s in samples) for c in stage_cols]

    # Per-run latency series
    run_ids = sorted(set(runs))
    run_series = {}
    for rid in run_ids:
        run_samples = [s for s in samples if s["run"] == rid]
        run_series[rid] = [s["total_ms"] for s in run_samples]

    # Stacked bar data (first 30)
    top30 = samples[:30]
    stacked_x = list(range(len(top30)))
    stacked_prompts = [
        s["prompt"][:50] + "…" if len(s["prompt"]) > 50 else s["prompt"] for s in top30
    ]
    stacked_stages = {col: [s[col] for s in top30] for col in stage_cols}

    # Embed all data as JSON for Plotly
    data_json = json.dumps(
        {
            "total_ms": total_ms,
            "statuses": statuses,
            "tokens": tokens,
            "prompts": prompts,
            "runs": runs,
            "status_counts": status_counts,
            "stage_avgs": stage_avgs,
            "stage_labels": stage_labels,
            "run_series": {str(k): v for k, v in run_series.items()},
            "stacked_x": stacked_x,
            "stacked_prompts": stacked_prompts,
            "stacked_stages": stacked_stages,
            "stage_cols": stage_cols,
        }
    )

    status_color_map = {
        "success": "#22c55e",
        "invalid_sql": "#ef4444",
        "unanswerable": "#f97316",
        "error": "#a855f7",
    }
    status_colors_json = json.dumps(status_color_map)

    kpi_cards = f"""
        <div class="kpi-grid">
          <div class="kpi"><div class="kpi-val">{success_rate:.1f}%</div><div class="kpi-label">Success Rate</div></div>
          <div class="kpi"><div class="kpi-val">{avg_ms:,.0f} ms</div><div class="kpi-label">Avg Latency</div></div>
          <div class="kpi"><div class="kpi-val">{p50:,.0f} ms</div><div class="kpi-label">p50 Latency</div></div>
          <div class="kpi"><div class="kpi-val">{p95:,.0f} ms</div><div class="kpi-label">p95 Latency</div></div>
          <div class="kpi"><div class="kpi-val">{avg_tokens:,.0f}</div><div class="kpi-label">Avg Tokens / Req</div></div>
          <div class="kpi"><div class="kpi-val">{avg_calls:.2f}</div><div class="kpi-label">Avg LLM Calls / Req</div></div>
          <div class="kpi"><div class="kpi-val">{len(samples)}</div><div class="kpi-label">Total Samples</div></div>
        </div>
    """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GenAI Analytics Pipeline — Benchmark Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.32.0.min.js" charset="utf-8"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
    }}
    header {{
      padding: 2rem 2.5rem 1rem;
      border-bottom: 1px solid #1e293b;
    }}
    header h1 {{
      font-size: 1.5rem;
      font-weight: 700;
      color: #f8fafc;
      letter-spacing: -0.02em;
    }}
    header p {{
      color: #94a3b8;
      margin-top: 0.25rem;
      font-size: 0.875rem;
    }}
    .badge {{
      display: inline-block;
      background: #22c55e22;
      color: #22c55e;
      border: 1px solid #22c55e44;
      border-radius: 9999px;
      font-size: 0.75rem;
      padding: 0.15rem 0.65rem;
      margin-left: 0.75rem;
      vertical-align: middle;
    }}
    main {{ padding: 2rem 2.5rem; max-width: 1400px; margin: 0 auto; }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }}
    .kpi {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 1.25rem 1rem;
      text-align: center;
    }}
    .kpi-val {{
      font-size: 1.6rem;
      font-weight: 700;
      color: #38bdf8;
      letter-spacing: -0.02em;
    }}
    .kpi-label {{
      font-size: 0.75rem;
      color: #94a3b8;
      margin-top: 0.3rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .charts-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1.25rem;
    }}
    .chart-card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 1.25rem;
    }}
    .chart-card.full {{ grid-column: 1 / -1; }}
    .chart-title {{
      font-size: 0.85rem;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .plotly-chart {{ width: 100%; }}
    footer {{
      text-align: center;
      color: #475569;
      font-size: 0.75rem;
      padding: 2rem;
    }}
    @media (max-width: 768px) {{
      .charts-grid {{ grid-template-columns: 1fr; }}
      .chart-card.full {{ grid-column: 1; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>GenAI Analytics Pipeline — Benchmark Dashboard
      <span class="badge">Live Data</span>
    </h1>
    <p>model: openai/gpt-5-nano &nbsp;·&nbsp; dataset: gaming_mental_health (1M rows) &nbsp;·&nbsp; generated May 2026</p>
  </header>
  <main>
    {kpi_cards}
    <div class="charts-grid">
      <div class="chart-card">
        <div class="chart-title">Request Latency Distribution</div>
        <div id="chart-latency" class="plotly-chart"></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">Average Time per Pipeline Stage</div>
        <div id="chart-stages" class="plotly-chart"></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">Status Breakdown</div>
        <div id="chart-status" class="plotly-chart"></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">Tokens vs Latency</div>
        <div id="chart-scatter" class="plotly-chart"></div>
      </div>
      <div class="chart-card full">
        <div class="chart-title">Latency per Prompt across Runs</div>
        <div id="chart-timeline" class="plotly-chart"></div>
      </div>
      <div class="chart-card full">
        <div class="chart-title">Per-Request Stage Breakdown (first 30 samples)</div>
        <div id="chart-stacked" class="plotly-chart"></div>
      </div>
    </div>
  </main>
  <footer>GenAI-Labs Take-Home Assignment — Abhishek Deshmukh — 2026</footer>

  <script>
  (function() {{
    const D = {data_json};
    const SC = {status_colors_json};
    const LAYOUT_BASE = {{
      paper_bgcolor: "transparent",
      plot_bgcolor: "transparent",
      font: {{ color: "#94a3b8", family: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif", size: 12 }},
      margin: {{ t: 10, r: 10, b: 40, l: 50 }},
      xaxis: {{ gridcolor: "#1e3a5f", zerolinecolor: "#1e3a5f" }},
      yaxis: {{ gridcolor: "#1e3a5f", zerolinecolor: "#1e3a5f" }},
    }};
    const CONFIG = {{ displayModeBar: false, responsive: true }};

    // 1. Latency distribution
    const p50 = D.total_ms.slice().sort((a,b)=>a-b)[Math.round(D.total_ms.length*0.50)-1];
    const p95 = D.total_ms.slice().sort((a,b)=>a-b)[Math.round(D.total_ms.length*0.95)-1];
    Plotly.newPlot("chart-latency", [
      {{ x: D.total_ms, type: "histogram", nbinsx: 20, marker: {{ color: "#38bdf8", opacity: 0.85 }}, name: "Requests" }},
      {{ x: [p50, p50], y: [0, 10], mode: "lines", line: {{ color: "#f97316", dash: "dash", width: 2 }}, name: `p50 ${{Math.round(p50)}} ms` }},
      {{ x: [p95, p95], y: [0, 10], mode: "lines", line: {{ color: "#ef4444", dash: "dash", width: 2 }}, name: `p95 ${{Math.round(p95)}} ms` }},
    ], Object.assign({{}}, LAYOUT_BASE, {{
      xaxis: {{ ...LAYOUT_BASE.xaxis, title: "Total latency (ms)" }},
      yaxis: {{ ...LAYOUT_BASE.yaxis, title: "Requests" }},
      legend: {{ orientation: "h", y: 1.1 }},
      bargap: 0.05,
    }}), CONFIG);

    // 2. Stage breakdown bar
    const stagePalette = ["#38bdf8","#22c55e","#f97316","#a855f7"];
    Plotly.newPlot("chart-stages", [{{
      x: D.stage_labels,
      y: D.stage_avgs,
      type: "bar",
      marker: {{ color: stagePalette }},
      text: D.stage_avgs.map(v => Math.round(v) + " ms"),
      textposition: "outside",
      textfont: {{ color: "#e2e8f0" }},
    }}], Object.assign({{}}, LAYOUT_BASE, {{
      yaxis: {{ ...LAYOUT_BASE.yaxis, title: "Average ms" }},
      bargap: 0.3,
    }}), CONFIG);

    // 3. Status pie
    const statusKeys = Object.keys(D.status_counts);
    Plotly.newPlot("chart-status", [{{
      labels: statusKeys,
      values: statusKeys.map(k => D.status_counts[k]),
      type: "pie",
      hole: 0.45,
      marker: {{ colors: statusKeys.map(k => SC[k] || "#64748b") }},
      textinfo: "label+percent",
      textfont: {{ color: "#f8fafc", size: 13 }},
    }}], Object.assign({{}}, LAYOUT_BASE, {{
      margin: {{ t: 10, r: 10, b: 10, l: 10 }},
      showlegend: false,
    }}), CONFIG);

    // 4. Tokens vs latency scatter
    const colorArr = D.statuses.map(s => SC[s] || "#64748b");
    const legendAdded = {{}};
    const scatterTraces = [...new Set(D.statuses)].map(status => {{
      const idx = D.statuses.map((s,i) => s===status ? i : -1).filter(i=>i>=0);
      return {{
        x: idx.map(i => D.tokens[i]),
        y: idx.map(i => D.total_ms[i]),
        text: idx.map(i => D.prompts[i]),
        mode: "markers",
        type: "scatter",
        name: status,
        marker: {{ color: SC[status] || "#64748b", size: 8, opacity: 0.8 }},
        hovertemplate: "<b>%{{text}}</b><br>Tokens: %{{x}}<br>Latency: %{{y:.0f}} ms<extra></extra>",
      }};
    }});
    Plotly.newPlot("chart-scatter", scatterTraces, Object.assign({{}}, LAYOUT_BASE, {{
      xaxis: {{ ...LAYOUT_BASE.xaxis, title: "Total tokens" }},
      yaxis: {{ ...LAYOUT_BASE.yaxis, title: "Latency (ms)" }},
      legend: {{ orientation: "h", y: 1.1 }},
    }}), CONFIG);

    // 5. Latency over time (per run)
    const runColors = ["#38bdf8","#22c55e","#f97316","#a855f7","#ec4899"];
    const timelineTraces = Object.entries(D.run_series).map(([rid, vals], i) => ({{
      x: vals.map((_,j) => j),
      y: vals,
      mode: "lines+markers",
      name: `Run ${{rid}}`,
      line: {{ color: runColors[i % runColors.length], width: 2 }},
      marker: {{ size: 5 }},
    }}));
    Plotly.newPlot("chart-timeline", timelineTraces, Object.assign({{}}, LAYOUT_BASE, {{
      xaxis: {{ ...LAYOUT_BASE.xaxis, title: "Prompt index within run" }},
      yaxis: {{ ...LAYOUT_BASE.yaxis, title: "Total latency (ms)" }},
      legend: {{ orientation: "h", y: 1.05 }},
    }}), CONFIG);

    // 6. Stacked bar
    const stackedTraces = D.stage_cols.map((col, i) => ({{
      x: D.stacked_x,
      y: D.stacked_stages[col],
      text: D.stacked_prompts,
      name: D.stage_labels[i],
      type: "bar",
      marker: {{ color: stagePalette[i] }},
      hovertemplate: "<b>%{{text}}</b><br>" + D.stage_labels[i] + ": %{{y:.0f}} ms<extra></extra>",
    }}));
    Plotly.newPlot("chart-stacked", stackedTraces, Object.assign({{}}, LAYOUT_BASE, {{
      barmode: "stack",
      xaxis: {{ ...LAYOUT_BASE.xaxis, title: "Sample index", tickangle: 0 }},
      yaxis: {{ ...LAYOUT_BASE.yaxis, title: "Latency (ms)" }},
      legend: {{ orientation: "h", y: 1.05 }},
      bargap: 0.15,
    }}), CONFIG);
  }})();
  </script>
</body>
</html>
"""
    return html


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate interactive HTML dashboard from benchmark JSONL."
    )
    parser.add_argument("samples", help="Path to JSONL file from benchmark.py --save-samples")
    parser.add_argument(
        "--out",
        default="reports/dashboard.html",
        help="Output HTML path (default: reports/dashboard.html)",
    )
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open in browser.")
    args = parser.parse_args()

    samples_path = Path(args.samples)
    if not samples_path.exists():
        raise SystemExit(
            f"File not found: {samples_path}\n"
            "Run first:\n"
            "  OPENROUTER_API_KEY=sk-or-... PYTHONPATH=. "
            "python3 scripts/benchmark.py --runs 3 --save-samples data/samples.jsonl"
        )

    samples = load(samples_path)
    html = build_html(samples)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Dashboard written to: {out_path}")

    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())
        print("Opening in browser…")


if __name__ == "__main__":
    main()
