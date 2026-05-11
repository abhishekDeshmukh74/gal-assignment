# Production Readiness Checklist

**Instructions:** Complete all sections below. Check the box when an item is implemented, and provide descriptions where requested. This checklist is a required deliverable.

---

## Approach

Describe how you approached this assignment and what key problems you identified and solved.

- [x] **System works correctly end-to-end**

**What were the main challenges you identified?**
```
1. The baseline pipeline did not actually work on the default model.
   `openai/gpt-5-nano` is a reasoning model: with `max_tokens=240` it spends
   the entire budget on hidden reasoning tokens and returns `content=None`,
   so SQL generation always errored and every benchmark sample failed.
2. Token counting was a stub. The HARD requirement in the README is that
   `total_llm_stats` carry real prompt/completion/total token counts.
3. The SQL generation prompt had no schema. The model could not know which
   columns exist, so even when it answered it hallucinated columns
   (e.g. `zodiac_sign`).
4. `SQLValidator.validate` was a no-op. Any string the model returned was
   accepted, including DELETE/DROP. Public test 3 demands that
   "delete all rows" be rejected with status `invalid_sql`.
5. Pipeline status logic relied on a non-existent error attribute path, so
   true error paths surfaced as `unanswerable`.
6. The benchmark script crashed at `result["status"]` (`PipelineOutput` is
   a dataclass, not a dict).
7. Observability was absent — no logs, no metrics, no request correlation.
8. No protection against a runaway query on a 1M-row table.
```

**What was your approach?**
```
Treat the system as a four-stage pipeline (generate -> validate -> execute
-> answer) wrapped in an observability span per stage. Every stage produces
a typed output (`src/types.py` unchanged) and is independently testable.

Concrete moves:
* Implemented real token counting in `OpenRouterLLMClient._chat` against
  `res.usage`, with a chars/4 fallback so efficiency metrics never
  silently zero out.
* Configured the reasoning model with `reasoning={"effort": "minimal"}`
  and raised `max_tokens` to 512 so the model has budget for both
  reasoning and output. (Disabling reasoning entirely broke gpt-5-nano on
  OpenRouter.)
* Introspected the SQLite schema once at pipeline init via PRAGMA and
  injected a compact `table(col TYPE, ...)` block into the system prompt,
  so the model always generates SQL against real columns.
* Built a multi-layer `SQLValidator`: comment-strip -> single-statement
  -> SELECT/WITH-only -> DML/DDL block-list -> `EXPLAIN` against the live
  DB to catch unknown columns/tables -> auto-inject `LIMIT 100` on
  non-aggregate queries.
* Always execute the *validated* SQL (not the raw model output) so the
  LIMIT cap is what actually runs.
* Wrote a stdlib-only observability module (`src/observability.py`) with
  JSON-line logging, a thread-safe `Metrics` counter+timing buffer, and a
  `stage_span` context manager.
* Auto-generated `request_id` (12 hex chars) propagated to every log line.
* Made the answer prompt cheap: trim row payload to 20 rows, truncate any
  string > 200 chars, use a tight 3-rule system prompt.
* Built `tests/test_unit.py` (23 tests, no API key) covering validator,
  executor, sql extraction, and token accounting against a stubbed
  OpenRouter response.
* Fixed the `benchmark.py` `result["status"]` crash and extended the
  summary with `avg_tokens_per_request` + `avg_llm_calls_per_request`.
```

---

## Observability

- [x] **Logging**
  - Description: stdlib-`logging` based JSON-line logger in `src/observability.py:get_logger()`. Every record is one JSON object with `ts`, `level`, `logger`, `msg`, and any structured extras (`request_id`, `stage`, `duration_ms`, `valid`, `row_count`, `total_tokens`, etc.). Logger writes to stderr; level controlled by `LOG_LEVEL` env var. No third-party deps — drops straight into a JSON-ingesting log shipper (Datadog, Loki, CloudWatch).

- [x] **Metrics**
  - Description: thread-safe `src/observability.py:Metrics` exposes counters (`stage.<name>.ok`, `stage.<name>.error`, `pipeline.status.<status>`), timing histograms with rolling p50/p95 windows (`stage.<name>.duration_ms`, `llm.chat.duration_ms`), and global LLM totals (`llm_tokens_total`, `llm_calls_total`). Reachable via `AnalyticsPipeline.get_metrics()`. The API maps 1:1 onto `prometheus_client.Counter` / `Histogram` for a real deployment.

- [x] **Tracing**
  - Description: `stage_span(...)` context manager in `src/observability.py` opens a structured span around each pipeline stage, logs `stage.start` / `stage.end` / `stage.error`, records timing into the metrics buffer, and increments per-stage success/error counters. A 12-character `request_id` is auto-generated when the caller does not supply one (`new_request_id()`) and threaded through every log line and the `PipelineOutput.request_id` field — so a request can be reconstructed end-to-end from the JSON log stream. Mapping to OpenTelemetry spans is one constructor swap away.

---

## Validation & Quality Assurance

- [x] **SQL validation**
  - Description: `src/pipeline.py:SQLValidator` runs five gates in order: (1) normalize (strip `--` and `/* */` comments, trim trailing `;`, collapse whitespace); (2) reject multi-statement (any remaining `;`); (3) require leading `SELECT`/`WITH`; (4) regex block-list for DML/DDL/PRAGMA/ATTACH/VACUUM/GRANT/... with word boundaries so `created_at` does not false-positive; (5) `EXPLAIN <sql>` against the live SQLite DB to catch unknown columns, unknown tables, and pure syntax errors using SQLite's own planner. Final step auto-injects `LIMIT 100` when the query has no aggregate function and no `LIMIT`. Output is `SQLValidationOutput(is_valid, validated_sql, error, timing_ms)`.

- [x] **Answer quality**
  - Description: Three guards. (1) When the model returns no SQL the answer stage short-circuits with a fixed *"I cannot answer this with the available table and schema..."* response — no extra LLM call, satisfies the `unanswerable` contract. (2) When SQL ran but rows are empty, a deterministic "no rows" message is returned, again with no LLM call. (3) When rows exist, the answer prompt is system-pinned to *"Use ONLY the provided rows, do not invent data, 1-3 sentences"* and the payload is capped at 20 rows / 200-char string truncation. Public test `test_unanswerable_prompt_is_handled` exercises path (1).

- [x] **Result consistency**
  - Description: The pipeline always executes `validation_output.validated_sql` (with the auto-injected LIMIT), never the raw model output, so the SQL we report in `PipelineOutput.sql` is exactly the SQL that produced `PipelineOutput.rows`. SQL prompt instructs the model to round REAL aggregates to 2 decimals to keep numeric answers consistent across re-runs. `temperature=0.0` for SQL generation, `temperature=0.2` for answer generation.

- [x] **Error handling**
  - Description: Each stage returns a typed output with an optional `error: str`. `AnalyticsPipeline._derive_status` then picks the final status with priority: generation transport error -> `error`, model returned null SQL -> `unanswerable`, validator rejected -> `invalid_sql`, executor errored -> `error`, else `success`. Exceptions inside `stage_span` are logged with `stage.error` + duration + traceback and re-raised. There is an opt-in single-shot SQL repair (`enable_sql_repair=True`) that feeds the executor error back to the model and tries one more time — off by default to keep tail latency predictable.

---

## Maintainability

- [x] **Code organization**
  - Description: Four modules with clear responsibilities. `src/types.py` — dataclass contracts (untouched, satisfies hard requirement #4). `src/observability.py` — logging, metrics, span context. `src/llm_client.py` — transport, token accounting, SQL / answer prompts. `src/pipeline.py` — `SQLValidator`, `SQLiteExecutor`, `AnalyticsPipeline` orchestration. No circular imports. The pipeline accepts an injected `OpenRouterLLMClient` and `Metrics` for testability.

- [x] **Configuration**
  - Description: All knobs are env-driven, not hard-coded. `OPENROUTER_API_KEY` (required), `OPENROUTER_MODEL` (default `openai/gpt-5-nano`), `LOG_LEVEL` (default `INFO`). DB path, fetch limit, repair toggle and metrics history size are constructor arguments. `.env` is gitignored.

- [x] **Error handling**
  - Description: see *Validation -> Error handling* above. Highlights: every exception path returns a populated dataclass instead of bubbling up; transport-layer fallback when the OpenRouter SDK rejects an optional `reasoning` kwarg; defensive token-usage parsing that tolerates `None`, missing fields, and non-int values.

- [x] **Documentation**
  - Description: Module docstrings at the top of `pipeline.py`, `llm_client.py`, and `observability.py` explain intent and design choices. Public methods have docstrings. `SOLUTION_NOTES.md` summarizes design, measured impact, and tradeoffs. This `CHECKLIST.md` documents what was implemented and where.

---

## LLM Efficiency

- [x] **Token usage optimization**
  - Description: (1) Compact, schema-aware system prompt — one paragraph + a single-line `table(col TYPE, ...)` block — pasted in front of every SQL request so the model never wastes tokens guessing column names. (2) Answer prompt trims rows to 20 and any string > 200 chars, then JSON-serializes with `ensure_ascii=True` for minimal escaping. (3) `reasoning={"effort": "minimal"}` cuts gpt-5-nano hidden-reasoning consumption. (4) `max_tokens=512` is the smallest setting that reliably leaves room for both minimal reasoning and structured JSON output on this model. Measured: ~792 tokens / request, 1.97 LLM calls / request across 36 samples.

- [x] **Efficient LLM requests**
  - Description: One LLM call for SQL, one for the answer — no chain-of-thought, no planner, no critic. The answer call is short-circuited to a deterministic string when SQL is null or rows are empty, so unanswerable questions cost exactly **one** LLM call instead of two (saving ~50% on the worst case). Both calls run at `stream=False` since the consumer needs the full text before the next stage. Optional single-shot repair adds one more call only on executor errors (off by default).

---

## Testing

- [x] **Unit tests**
  - Description: `tests/test_unit.py` (23 tests, **no API key required**). Coverage: SQLValidator — SELECT passes, CTE passes, DELETE / INSERT / DROP / PRAGMA rejected, multi-statement rejected, unknown column rejected via EXPLAIN, LIMIT injection on non-aggregates, LIMIT not added when aggregate present, `None` input handled, typed return. SQLiteExecutor — known query, error surfacing, `None` SQL. `_extract_sql` — JSON envelope, JSON null, raw SELECT, raw WITH, markdown fence stripping, no-SQL text. Token accounting — usage fields counted, missing-usage fallback estimate. Each test sets up its own tempfile SQLite fixture so it works on any machine.

- [x] **Integration tests**
  - Description: `tests/test_public.py` is unmodified (hard requirement #1). All 5 tests pass against the real OpenRouter API: `test_answerable_prompt_returns_sql_and_answer`, `test_unanswerable_prompt_is_handled`, `test_invalid_sql_is_rejected`, `test_timings_exist`, `test_output_contract_is_internal_eval_compatible`. Run time ~12 s on local hardware.

- [x] **Performance tests**
  - Description: `scripts/benchmark.py` (now functional — the `result["status"]` bug is fixed) runs all 12 prompts x N repetitions and reports `avg_ms`, `p50_ms`, `p95_ms`, `success_rate`, `avg_tokens_per_request`, `avg_llm_calls_per_request`. 36 samples per run gives stable p50; p95 is jitter-dominated but useful as a regression alarm.

- [x] **Edge case coverage**
  - Description: Empty result set -> fixed "no rows" answer. Null SQL from model -> "cannot answer" template. Multi-statement / DML SQL -> rejected pre-execution. Unknown column -> caught by EXPLAIN before hitting the table. OpenRouter response missing `usage` -> token estimate fallback. OpenRouter `reasoning` kwarg unsupported -> silently retried without it. 1M-row scan attempts -> bounded by auto-injected `LIMIT 100`.

---

## Optional: Multi-Turn Conversation Support

*Not implemented — kept the work inside the 4-6 hour timebox and focused on production hardening of the single-turn pipeline.*

Design sketch (not built):

- Add a `SessionStore` (in-memory dict; Redis in prod) keyed by a caller-supplied `session_id`, storing the last N tuples of `(question, validated_sql, compact_row_summary)`.
- Pass a `ConversationHistory:` block into the SQL system prompt only when history exists; instruct the model to either edit the previous SQL (for "what about males", "sort by anxiety") or generate fresh SQL.
- An "intent detection" classifier is not needed — letting the SQL model decide whether to reuse or regenerate is cheaper and avoids an extra LLM hop. The validator + EXPLAIN already protect against ambiguous-reference failures.
- Ambiguity resolution lives in the model: providing the prior SQL gives it the surface to disambiguate; if it cannot, it returns `{"sql": null}` and the user gets the "cannot answer" path.

- [ ] **Intent detection for follow-ups**
  - Description: *not implemented (see sketch above)*
- [ ] **Context-aware SQL generation**
  - Description: *not implemented (see sketch above)*
- [ ] **Context persistence**
  - Description: *not implemented (see sketch above)*
- [ ] **Ambiguity resolution**
  - Description: *not implemented (see sketch above)*

**Approach summary:**
```
Not implemented — chose to invest the hours in correctness, validation,
observability, and tests for the required single-turn path.
```

---

## Production Readiness Summary

**What makes your solution production-ready?**
```
- Real token accounting wired into both metrics and per-request outputs.
- Strict input contract preserved (PipelineOutput + per-stage dataclasses).
- Defense-in-depth on SQL: LLM is told what is in scope, validator rejects
  anything dangerous or unknown, executor still runs with a fetch limit,
  and an auto-LIMIT protects the 1M-row table from full scans.
- One JSON log line per stage with a stable request_id makes incident
  response and replay tractable.
- 23 unit tests run without an API key, so the pipeline is testable in
  CI/CD without provisioning a real OpenRouter key.
- All 5 public integration tests pass on the live API.
- 97.2% success rate measured across 36 benchmarked samples.
```

**Key improvements over baseline:**
```
- Baseline was non-functional on the default model (max_tokens too small
  for a reasoning model; empty content; 0% success). Our pipeline ships
  at 97.2% success on the same prompts.
- Baseline shipped no validation. Ours has five validation layers
  including a semantic check against the actual DB schema.
- Baseline shipped no observability. Ours has JSON logs, metrics with
  p50/p95, and request-correlation IDs.
- Baseline token counter was a TODO. Ours reads `res.usage`, supports a
  fallback estimate, and aggregates per request + globally.
- Baseline benchmark script crashed at `result["status"]`. Fixed and
  extended with token/call efficiency lines.
```

**Known limitations or future work:**
```
- gpt-5-nano spends ~200-400 tokens per request on hidden reasoning even
  at `effort="minimal"`. A non-reasoning model (e.g. llama-3.3-70b-free
  on OpenRouter) would likely halve latency and tokens, but the README
  pins the default to gpt-5-nano so we kept it.
- Prompt caching with OpenRouter is not yet wired. The system prompt is
  identical across requests and could be cached for a ~30-40% prompt-token
  reduction.
- No retry-with-backoff on transient OpenRouter 5xx — relies on the SDK's
  built-in retries. Production should pin `RetryConfig` explicitly.
- Schema is introspected once at startup. A schema-change watcher (or
  TTL-based re-introspection) is needed if the DB evolves at runtime.
- Multi-turn / follow-up support is not implemented (see "Optional"
  section for sketch).
- Single-shot SQL repair is implemented but off by default. With a
  stricter latency budget it should stay off; with a stricter accuracy
  budget it should be turned on.
```

---

## Benchmark Results

Include your before/after benchmark results here.

**Baseline (per README reference, on reference hardware):**
- Average latency: `~2900 ms`
- p50 latency: `~2500 ms`
- p95 latency: `~4700 ms`
- Success rate: *not reported — baseline is non-functional on default model (see notes)*

**Your solution** (3 runs x 12 prompts = 36 samples, openai/gpt-5-nano via OpenRouter):
- Average latency: `3674 ms`
- p50 latency: `3830 ms`
- p95 latency: `4876 ms`
- Success rate: `97.22 %`

**LLM efficiency:**
- Average tokens per request: `791.67`
- Average LLM calls per request: `1.97`

*Note on comparison:* the README baseline numbers were measured on different hardware and against a pipeline that does not actually return content with gpt-5-nano (max_tokens too low for a reasoning model — 0% real success). The numbers above are the cost of a **working** pipeline with five validation layers, schema-aware generation, full observability, and 97% measured success. The extra ~200 tokens vs. baseline pay for the schema block injected into every SQL prompt — without it the model hallucinates columns. See `SOLUTION_NOTES.md` for the full tradeoff discussion.

---

**Completed by:** Abhishek Deshmukh
**Date:** 2026-05-11
**Time spent:** ~4 hours
