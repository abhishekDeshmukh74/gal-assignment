# Solution Notes

Short engineering note for the GenAI-Labs take-home. Pairs with [CHECKLIST.md](CHECKLIST.md).

## What I changed

| Area | Change | File |
|---|---|---|
| Token counting | Read `res.usage` from the OpenRouter response, accumulate into `self._stats`, fall back to `len(text)//4` if the provider omits usage. | [src/llm_client.py](src/llm_client.py) |
| SQL generation | Compact system prompt + injected schema (`table(col TYPE, ...)`) so the model never guesses columns. Strict JSON envelope `{"sql": ...}`. `reasoning={"effort": "minimal"}` and `max_tokens=512` to make gpt-5-nano actually return content. | [src/llm_client.py](src/llm_client.py) |
| SQL parsing | `_extract_sql` now strips markdown fences, parses JSON, falls back to first `SELECT`/`WITH`. | [src/llm_client.py](src/llm_client.py) |
| SQL validation | New 5-layer validator: comment strip, single-statement check, leading `SELECT`/`WITH`, DML/DDL block-list, `EXPLAIN` against the live DB, plus auto-LIMIT injection for non-aggregates. | [src/pipeline.py](src/pipeline.py) |
| Pipeline | Schema introspected once at startup. Always execute the *validated* SQL. Status priority fixed. Each stage wrapped in `stage_span` for structured logs + metrics. Opt-in single-shot SQL repair. | [src/pipeline.py](src/pipeline.py) |
| Observability | New module: JSON-line logger, thread-safe `Metrics` (counters + rolling p50/p95 timings + LLM totals), `stage_span` context manager, `request_id` auto-gen. | [src/observability.py](src/observability.py) |
| Answer generation | Trim rows to 20 + truncate strings >200 chars. Tight 3-rule system prompt. Short-circuits for null-SQL and empty-rows (no LLM call). | [src/llm_client.py](src/llm_client.py) |
| Benchmark | Fixed `result["status"]` crash (dataclass, not dict). Added `avg_tokens_per_request`, `avg_llm_calls_per_request` to the summary. | [scripts/benchmark.py](scripts/benchmark.py) |
| Tests | New `tests/test_unit.py` — 23 tests, no API key required, covers validator, executor, sql extraction, and token accounting via a stubbed transport. `tests/test_public.py` is unmodified per hard requirement #1. | [tests/test_unit.py](tests/test_unit.py) |

## Why I changed it

1. **The baseline did not work on the configured default model.** `openai/gpt-5-nano` is a reasoning model — with `max_tokens=240` it burns the entire budget on hidden reasoning and returns `content=None`. Every pipeline run failed silently with `status=error`, `total_tokens=0`. I raised `max_tokens` and set reasoning effort to `minimal` to get visible output.
2. **Token counting is a hard requirement.** I wired up provider usage with a defensive fallback so efficiency metrics are never silently zero.
3. **Hidden evaluation includes paraphrased / edge prompts.** A schema-aware prompt plus a validator that runs `EXPLAIN` is the cheapest way to keep accuracy high under paraphrase — the model gets the source of truth and the validator catches anything the model fabricates.
4. **A 1M-row SQLite table will happily full-scan for minutes.** Auto-LIMIT injection on non-aggregate queries is the simplest production guardrail; without it a single bad SQL would spike p95.
5. **No observability means no production.** JSON logs + `request_id` correlation + per-stage timing/error counters is the minimum needed to debug a real incident; everything else (Datadog/OTEL/Prometheus) is one constructor swap away.
6. **CI must not require an OpenRouter key.** Unit tests stub the transport so the validator/extractor/token-accounting can be exercised at zero cost on every push.

## Measured impact

3 runs x 12 prompts = 36 samples, gpt-5-nano via OpenRouter, local network (variance is non-trivial — note this for the latency line).

| Metric | README baseline (reference HW) | This solution (local) |
|---|---|---|
| avg latency | ~2900 ms | **3674 ms** |
| p50 latency | ~2500 ms | **3830 ms** |
| p95 latency | ~4700 ms | **4876 ms** |
| tokens / request | ~600 | **791.67** |
| LLM calls / request | (not reported) | **1.97** |
| success rate | (not reported) | **97.22%** |

The README baseline was measured on different hardware **and** on a pipeline that does not return content with gpt-5-nano at the original max_tokens — so its real success rate is 0%. The numbers above are the cost of a pipeline that actually answers questions, with five validation layers and full observability. The ~200 extra tokens/request fund the schema block; without it the model hallucinates column names.

## Tradeoffs

- **Larger `max_tokens` (512 vs 240).** Necessary for a reasoning model; costs ~30% more tokens than the README target but eliminates the empty-content failure mode entirely. With a non-reasoning default model this would drop back to ~240.
- **`EXPLAIN`-based validation runs an extra SQLite call per request.** Trivial cost (sub-millisecond on the warm DB), but it does require the DB to be reachable from the pipeline at validate time. Acceptable since the executor needs it anyway.
- **Schema is introspected once at startup.** Fast, cache-friendly, but does not handle live schema evolution. A TTL-based re-introspection would fix this if the dataset is mutable in production.
- **Single-shot SQL repair is implemented but OFF by default.** Adding it would push success rate higher on edge prompts but adds one LLM call to the slow path and worsens p95. The toggle is `enable_sql_repair=True` on `AnalyticsPipeline`.
- **Optional multi-turn support is not built.** The single-turn pipeline was the higher-leverage place to spend the timebox; the design sketch is in `CHECKLIST.md` under the optional section.
- **Reasoning effort `minimal` not `disabled`.** Setting `{"enabled": false}` on gpt-5-nano produced empty content again, so we kept minimal effort.

## Next steps (in priority order)

1. Switch default model to a non-reasoning free model (e.g. `meta-llama/llama-3.3-70b-instruct:free`) — projected to halve both latency and tokens with similar correctness. Gate behind an `OPENROUTER_MODEL` override so the assignment default is preserved.
2. Wire OpenRouter prompt caching for the system prompt + schema block (identical across requests) — expected ~30-40% prompt-token reduction.
3. Turn the in-process `Metrics` counters into `prometheus_client` Counter / Histogram and expose `/metrics`.
4. Replace `stage_span` with an OpenTelemetry tracer; emit spans to an OTLP collector.
5. Add a tiny semantic test set with golden answers and an LLM-judge to score answer quality across model changes (the current validator only proves the SQL is sane).
6. Implement multi-turn support per the sketch in `CHECKLIST.md`.
7. Enable `enable_sql_repair=True` once SLA allows the extra LLM hop on the slow path.
