"""Analytics pipeline: question -> SQL -> validate -> execute -> answer.

Production hardening over the baseline:
  * Schema introspection at startup, cached and injected into every SQL prompt.
  * SQL validator that combines lexical, structural, and semantic checks
    (SELECT-only, single statement, DML/DDL block-list, EXPLAIN against the
    real DB to catch unknown tables/columns) and auto-injects LIMIT 100 on
    non-aggregate queries to protect a 1M-row table.
  * Always executes the *validated* SQL, not the raw model output.
  * Optional one-shot SQL repair on execution error (off by default; toggle
    with `enable_sql_repair=True`).
  * Per-stage structured logging + counters via `src.observability`.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.llm_client import OpenRouterLLMClient, build_default_llm_client
from src.observability import Metrics, get_logger, new_request_id, stage_span
from src.types import (
    AnswerGenerationOutput,
    PipelineOutput,
    SQLExecutionOutput,
    SQLGenerationOutput,
    SQLValidationOutput,
)

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "gaming_mental_health.sqlite"

# Block-list for write/admin keywords. Word-boundary anchored so column names
# like `created_at` won't false-positive.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|"
    r"detach|pragma|vacuum|reindex|grant|revoke)\b",
    re.IGNORECASE,
)
_AGGREGATE_RE = re.compile(r"\b(count|sum|avg|min|max|group\s+by|distinct)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)
_LEADING_KEYWORD_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


class SQLValidationError(Exception):
    pass


class SQLValidator:
    """Multi-layer validator suitable for an analytics pipeline.

    Layers:
      1. Strip comments, normalize whitespace, drop trailing semicolons.
      2. Reject empty / multi-statement SQL.
      3. Require leading SELECT or WITH (CTE).
      4. Block-list dangerous keywords (DML/DDL/PRAGMA/...).
      5. Run `EXPLAIN <sql>` against the DB — catches unknown columns,
         unknown tables, and pure syntax errors using SQLite itself.
      6. Auto-inject `LIMIT 100` on non-aggregate queries to bound cost.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @classmethod
    def validate(cls, sql: str | None, db_path: str | Path | None = None) -> SQLValidationOutput:
        # Class-method form preserves the original baseline call signature
        # used in the public tests path while letting callers provide a DB.
        return cls(db_path or DEFAULT_DB_PATH)._validate(sql)

    def _validate(self, sql: str | None) -> SQLValidationOutput:
        start = time.perf_counter()

        if sql is None:
            return self._fail(start, "No SQL provided")

        normalized = self._normalize(sql)
        if not normalized:
            return self._fail(start, "Empty SQL after normalization")

        # Single-statement check (after stripping a trailing ';').
        if ";" in normalized:
            return self._fail(start, "Multiple statements are not allowed")

        if not _LEADING_KEYWORD_RE.match(normalized):
            return self._fail(start, "Only SELECT/WITH queries are allowed")

        forbidden = _FORBIDDEN_KEYWORDS.search(normalized)
        if forbidden:
            return self._fail(start, f"Forbidden keyword: {forbidden.group(0).upper()}")

        # Semantic check via SQLite's own planner.
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(f"EXPLAIN {normalized}")
        except sqlite3.Error as exc:
            return self._fail(start, f"SQL planner rejected query: {exc}")

        validated = self._enforce_limit(normalized)

        return SQLValidationOutput(
            is_valid=True,
            validated_sql=validated,
            error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )

    @staticmethod
    def _normalize(sql: str) -> str:
        s = _COMMENT_BLOCK_RE.sub(" ", sql)
        s = _COMMENT_LINE_RE.sub(" ", s)
        s = s.strip().rstrip(";").strip()
        # Collapse runs of whitespace; leaves string literals readable enough.
        s = re.sub(r"\s+", " ", s)
        return s

    @staticmethod
    def _enforce_limit(sql: str) -> str:
        if _LIMIT_RE.search(sql) or _AGGREGATE_RE.search(sql):
            return sql
        return f"{sql} LIMIT 100"

    @staticmethod
    def _fail(start: float, msg: str) -> SQLValidationOutput:
        return SQLValidationOutput(
            is_valid=False,
            validated_sql=None,
            error=msg,
            timing_ms=(time.perf_counter() - start) * 1000,
        )


class SQLiteExecutor:
    def __init__(
        self, db_path: str | Path = DEFAULT_DB_PATH, fetch_limit: int | None = None
    ) -> None:
        fetch_limit = (
            fetch_limit if fetch_limit is not None else int(os.getenv("DB_FETCH_LIMIT", "100"))
        )
        self.db_path = Path(db_path)
        self.fetch_limit = fetch_limit

    def run(self, sql: str | None) -> SQLExecutionOutput:
        start = time.perf_counter()

        if sql is None:
            return SQLExecutionOutput(
                rows=[],
                row_count=0,
                timing_ms=(time.perf_counter() - start) * 1000,
                error=None,
            )

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchmany(self.fetch_limit)]
            return SQLExecutionOutput(
                rows=rows,
                row_count=len(rows),
                timing_ms=(time.perf_counter() - start) * 1000,
                error=None,
            )
        except Exception as exc:
            return SQLExecutionOutput(
                rows=[],
                row_count=0,
                timing_ms=(time.perf_counter() - start) * 1000,
                error=str(exc),
            )


class AnalyticsPipeline:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        llm_client: OpenRouterLLMClient | None = None,
        *,
        enable_sql_repair: bool | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        if enable_sql_repair is None:
            enable_sql_repair = os.getenv("ENABLE_SQL_REPAIR", "false").lower() in (
                "1",
                "true",
                "yes",
            )
        self.db_path = Path(db_path)
        self.metrics = metrics or Metrics()
        self.llm = llm_client or build_default_llm_client(metrics=self.metrics)
        self.executor = SQLiteExecutor(self.db_path)
        self.validator = SQLValidator(self.db_path)
        self.enable_sql_repair = enable_sql_repair
        self._log = get_logger()
        self._schema = self._introspect_schema()

    def _introspect_schema(self) -> str:
        """Build a compact `table(col TYPE, ...)` description for the prompt."""
        lines: list[str] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                tables = [
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                ]
                for tbl in tables:
                    cols = [
                        f"{r['name']} {r['type'] or 'ANY'}"
                        for r in conn.execute(f'PRAGMA table_info("{tbl}")')
                    ]
                    lines.append(f"{tbl}({', '.join(cols)})")
        except sqlite3.Error as exc:
            self._log.warning(
                "schema.introspect_failed", extra={"db": str(self.db_path), "error": str(exc)}
            )
        return "\n".join(lines)

    def get_metrics(self) -> dict[str, Any]:
        return self.metrics.snapshot()

    def run(self, question: str, request_id: str | None = None) -> PipelineOutput:
        rid = request_id or new_request_id()
        start = time.perf_counter()
        self._log.info("pipeline.start", extra={"request_id": rid, "question": question})

        # ---- Stage 1: SQL Generation -------------------------------------
        with stage_span(self._log, self.metrics, request_id=rid, stage="sql_generation"):
            sql_gen_output: SQLGenerationOutput = self.llm.generate_sql(
                question, {"schema": self._schema}
            )
        sql = sql_gen_output.sql

        # ---- Stage 2: SQL Validation -------------------------------------
        with stage_span(self._log, self.metrics, request_id=rid, stage="sql_validation") as span:
            validation_output = self.validator._validate(sql)
            span["valid"] = validation_output.is_valid
            if validation_output.error:
                span["validation_error"] = validation_output.error
        validated_sql = validation_output.validated_sql

        # ---- Stage 3: SQL Execution --------------------------------------
        with stage_span(self._log, self.metrics, request_id=rid, stage="sql_execution") as span:
            execution_output = self.executor.run(validated_sql)
            span["row_count"] = execution_output.row_count
            if execution_output.error:
                span["execution_error"] = execution_output.error

        # Optional one-shot repair if execution fails. Off by default to keep
        # tail latency predictable; flip in deployment when SLA allows.
        if self.enable_sql_repair and execution_output.error and validated_sql is not None:
            with stage_span(self._log, self.metrics, request_id=rid, stage="sql_repair") as span:
                repair = self.llm.generate_sql(
                    question,
                    {"schema": self._schema},
                    prior_error=execution_output.error,
                    prior_sql=validated_sql,
                )
                span["repaired_sql_present"] = bool(repair.sql)
                # Merge stats into Stage 1 so total_llm_stats stays sensible.
                self._merge_llm_stats(sql_gen_output.llm_stats, repair.llm_stats)
                sql_gen_output.intermediate_outputs.append(
                    {
                        "repair_attempt": True,
                        **(repair.intermediate_outputs[0] if repair.intermediate_outputs else {}),
                    }
                )
                if repair.sql:
                    revalidated = self.validator._validate(repair.sql)
                    if revalidated.is_valid:
                        re_exec = self.executor.run(revalidated.validated_sql)
                        if not re_exec.error:
                            validation_output = revalidated
                            execution_output = re_exec
                            validated_sql = revalidated.validated_sql
                            sql = repair.sql

        # ---- Stage 4: Answer Generation ---------------------------------
        # Use the validated SQL for the answer prompt context: it is what
        # actually produced the rows.
        sql_for_answer = validated_sql if validation_output.is_valid else None
        with stage_span(self._log, self.metrics, request_id=rid, stage="answer_generation") as span:
            answer_output: AnswerGenerationOutput = self.llm.generate_answer(
                question, sql_for_answer, execution_output.rows
            )
            span["answer_chars"] = len(answer_output.answer)

        # ---- Status (priority order matters) ----------------------------
        status = self._derive_status(sql_gen_output, validation_output, execution_output)
        self.metrics.incr(f"pipeline.status.{status}")

        # ---- Aggregates --------------------------------------------------
        timings = {
            "sql_generation_ms": sql_gen_output.timing_ms,
            "sql_validation_ms": validation_output.timing_ms,
            "sql_execution_ms": execution_output.timing_ms,
            "answer_generation_ms": answer_output.timing_ms,
            "total_ms": (time.perf_counter() - start) * 1000,
        }
        total_llm_stats = self._aggregate_llm_stats(
            sql_gen_output.llm_stats, answer_output.llm_stats
        )

        self._log.info(
            "pipeline.end",
            extra={
                "request_id": rid,
                "status": status,
                "total_ms": round(timings["total_ms"], 2),
                "total_tokens": total_llm_stats.get("total_tokens", 0),
                "llm_calls": total_llm_stats.get("llm_calls", 0),
            },
        )

        return PipelineOutput(
            status=status,
            question=question,
            request_id=rid,
            sql_generation=sql_gen_output,
            sql_validation=validation_output,
            sql_execution=execution_output,
            answer_generation=answer_output,
            sql=validated_sql if validation_output.is_valid else sql,
            rows=execution_output.rows,
            answer=answer_output.answer,
            timings=timings,
            total_llm_stats=total_llm_stats,
        )

    @staticmethod
    def _derive_status(
        gen: SQLGenerationOutput,
        val: SQLValidationOutput,
        exe: SQLExecutionOutput,
    ) -> str:
        # 1. Transport error during generation.
        if gen.error:
            return "error"
        # 2. Model deliberately returned no SQL (out-of-schema question).
        if gen.sql is None:
            return "unanswerable"
        # 3. SQL was produced but the validator rejected it
        #    (forbidden keyword, unknown column, multi-statement, etc.).
        if not val.is_valid:
            return "invalid_sql"
        # 4. SQL ran but the database errored (rare after EXPLAIN).
        if exe.error:
            return "error"
        return "success"

    @staticmethod
    def _aggregate_llm_stats(*stats_dicts: dict[str, Any]) -> dict[str, Any]:
        agg = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        model = "unknown"
        for s in stats_dicts:
            for k in agg:
                agg[k] += int(s.get(k, 0) or 0)
            if s.get("model"):
                model = s["model"]
        agg["model"] = model
        return agg

    @staticmethod
    def _merge_llm_stats(target: dict[str, Any], extra: dict[str, Any]) -> None:
        for k in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            target[k] = int(target.get(k, 0) or 0) + int(extra.get(k, 0) or 0)
