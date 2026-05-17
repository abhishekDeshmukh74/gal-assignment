"""OpenRouter chat client for SQL generation and answer synthesis.

Production hardening over the baseline:
  * Real token counting from the provider's `usage` field with a safe fallback.
  * Per-call latency + token metrics emitted via the observability layer.
  * Compact, schema-aware system prompts that elicit strict JSON output.
  * Conservative `max_tokens` caps to limit latency and cost.
  * Optional one-shot SQL repair using the executor error message.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from typing import Any

from src.observability import Metrics, get_logger
from src.types import AnswerGenerationOutput, SQLGenerationOutput

DEFAULT_MODEL = "openai/gpt-5-nano"

_LLM_CALL_TIMEOUT_S: float = float(os.getenv("LLM_CALL_TIMEOUT_S", "30"))
_LLM_MAX_RETRIES: int = 2
_RETRYABLE_ERRORS = ("429", "503", "502", "rate limit", "timeout", "overloaded")

_SQL_SYSTEM_PROMPT = (
    "You translate analytics questions into a single SQLite query.\n"
    "Rules:\n"
    '1. Output STRICT JSON: {"sql": "<query>"} or {"sql": null} ONLY if the '
    "question cannot be expressed against the schema (missing columns/tables).\n"
    "2. Use ONLY the table and columns listed below.\n"
    "3. Prefer SELECT/WITH. If the user asks to modify data (DELETE/UPDATE/INSERT/"
    "DROP/etc.), emit the SQL they literally request; a downstream validator will "
    "reject it. Do NOT silently rewrite destructive requests as SELECTs.\n"
    "4. Single statement. No semicolons mid-query.\n"
    "5. The table has ~1M rows; ALWAYS aggregate (GROUP BY / AVG / COUNT / etc.) "
    "or include LIMIT. Never SELECT * without LIMIT.\n"
    '6. For "age groups", bucket the integer `age` column with CASE WHEN '
    "(e.g. <18, 18-24, 25-34, 35-44, 45-54, 55+).\n"
    "7. Round REAL aggregates to 2 decimals.\n"
)

_ANSWER_SYSTEM_PROMPT = (
    "You are a concise analytics assistant. Answer the user's question in 1-3 "
    "sentences using ONLY the SQL result rows provided. Do not invent numbers, "
    "do not mention SQL, and do not list every row verbatim — summarize."
)

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats = self._zero_stats()
        self._metrics = metrics
        self._log = get_logger()

    # ------------------------------------------------------------------ stats
    @staticmethod
    def _zero_stats() -> dict[str, int]:
        return {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def pop_stats(self) -> dict[str, Any]:
        out = dict(self._stats)
        self._stats = self._zero_stats()
        return out

    # --------------------------------------------------------------- transport
    def _send_with_retry(self, send_kwargs: dict[str, Any]) -> Any:
        """Submit an SDK call with wall-clock timeout and exponential-backoff retry."""
        delay = 1.0
        for attempt in range(_LLM_MAX_RETRIES + 1):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self._client.chat.send, **send_kwargs)
                    try:
                        return future.result(timeout=_LLM_CALL_TIMEOUT_S)
                    except concurrent.futures.TimeoutError:
                        raise RuntimeError(
                            f"LLM call timed out after {_LLM_CALL_TIMEOUT_S:.0f}s"
                        )
            except RuntimeError:
                raise  # timeout — don't retry
            except Exception as exc:
                if attempt < _LLM_MAX_RETRIES and any(
                    code in str(exc).lower() for code in _RETRYABLE_ERRORS
                ):
                    self._log.warning(
                        "llm.transient_error_retrying",
                        extra={"attempt": attempt + 1, "delay_s": delay, "error": str(exc)},
                    )
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise
        raise RuntimeError("unreachable")  # satisfies type checker

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        t0 = time.perf_counter()
        # The default model (`openai/gpt-5-nano`) is a reasoning model: it
        # consumes the bulk of `max_tokens` on hidden reasoning. We request
        # minimal reasoning effort so the visible output gets the budget.
        send_kwargs: dict[str, Any] = dict(
            messages=messages,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        try:
            send_kwargs["reasoning"] = {"effort": "minimal"}
            res = self._send_with_retry(send_kwargs)
        except Exception as _reasoning_exc:
            self._log.debug(
                "llm.reasoning_kwarg_unsupported", extra={"error": str(_reasoning_exc)}
            )
            send_kwargs.pop("reasoning", None)
            res = self._send_with_retry(send_kwargs)

        # ---- Token accounting (HARD REQUIREMENT) -------------------------
        # OpenRouter returns OpenAI-compatible usage. Be defensive: providers
        # occasionally omit one or more fields. Fall back to a rough estimate
        # so efficiency scoring never gets zeros silently.
        usage = getattr(res, "usage", None)
        prompt_tokens = self._coerce_int(_get(usage, "prompt_tokens"))
        completion_tokens = self._coerce_int(_get(usage, "completion_tokens"))
        total_tokens = self._coerce_int(_get(usage, "total_tokens"))

        choices = getattr(res, "choices", None) or []
        if not choices:
            raise RuntimeError("OpenRouter response contained no choices.")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter response content is not text.")
        content = content.strip()

        if total_tokens == 0:
            # Rough heuristic: ~4 chars per token. Keeps efficiency metrics
            # non-zero when a provider drops the usage block.
            prompt_chars = sum(len(m.get("content", "")) for m in messages)
            prompt_tokens = prompt_tokens or max(1, prompt_chars // 4)
            completion_tokens = completion_tokens or max(1, len(content) // 4)
            total_tokens = prompt_tokens + completion_tokens

        self._stats["llm_calls"] += 1
        self._stats["prompt_tokens"] += prompt_tokens
        self._stats["completion_tokens"] += completion_tokens
        self._stats["total_tokens"] += total_tokens

        if self._metrics is not None:
            self._metrics.add_llm_usage(total_tokens, calls=1)
            self._metrics.observe("llm.chat.duration_ms", (time.perf_counter() - t0) * 1000)

        return content

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _extract_sql(text: str) -> str | None:
        """Pull a SQL string from a JSON envelope or raw SELECT/WITH text."""
        if not text:
            return None
        s = text.strip()
        # Strip markdown code fences if present.
        if s.startswith("```"):
            s = re.sub(r"^```(?:json|sql)?\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
            s = s.strip()
        # Preferred: JSON envelope.
        m = _JSON_OBJ_RE.search(s)
        if m:
            try:
                parsed = json.loads(m.group(0))
                sql = parsed.get("sql") if isinstance(parsed, dict) else None
                if sql is None:
                    return None
                if isinstance(sql, str) and sql.strip():
                    return sql.strip().rstrip(";").strip()
            except json.JSONDecodeError:
                pass
        # Fallback: locate the first SELECT or WITH.
        lower = s.lower()
        for kw in ("with ", "select "):
            idx = lower.find(kw)
            if idx >= 0:
                return s[idx:].strip().rstrip(";").strip()
        return None

    # -------------------------------------------------------------- generation
    def generate_sql(
        self,
        question: str,
        context: dict[str, Any],
        *,
        prior_error: str | None = None,
        prior_sql: str | None = None,
    ) -> SQLGenerationOutput:
        schema = context.get("schema", "").strip() if isinstance(context, dict) else ""
        system_prompt = _SQL_SYSTEM_PROMPT
        if schema:
            system_prompt = f"{system_prompt}\nSchema:\n{schema}"

        if prior_error and prior_sql:
            user_prompt = (
                f"Question: {question}\n"
                f"The previous SQL failed with: {prior_error}\n"
                f"Previous SQL: {prior_sql}\n"
                'Return a corrected JSON {"sql": ...}.'
            )
        else:
            user_prompt = f'Question: {question}\nReturn JSON {{"sql": ...}}.'

        start = time.perf_counter()
        error: str | None = None
        sql: str | None = None
        intermediate: list[dict[str, Any]] = []

        try:
            text = self._chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=512,
            )
            sql = self._extract_sql(text)
            intermediate.append({"raw": text[:500], "extracted_sql": sql})
        except Exception as exc:
            error = str(exc)

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return SQLGenerationOutput(
            sql=sql,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            intermediate_outputs=intermediate,
            error=error,
        )

    def generate_answer(
        self, question: str, sql: str | None, rows: list[dict[str, Any]]
    ) -> AnswerGenerationOutput:
        if not sql:
            return AnswerGenerationOutput(
                answer=(
                    "I cannot answer this with the available table and schema. "
                    "Please rephrase using known survey fields."
                ),
                timing_ms=0.0,
                llm_stats={**self._zero_stats(), "model": self.model},
                error=None,
            )
        if not rows:
            return AnswerGenerationOutput(
                answer="The query executed successfully but returned no rows.",
                timing_ms=0.0,
                llm_stats={**self._zero_stats(), "model": self.model},
                error=None,
            )

        # Trim the row payload to keep prompts cheap. 20 rows is plenty for
        # the analytics prompts in scope.
        compact_rows = _compact_rows(rows[:20])
        user_prompt = (
            f"Question: {question}\n"
            f"Rows (JSON, up to 20): {json.dumps(compact_rows, ensure_ascii=True)}\n"
            "Answer in 1-3 sentences."
        )

        start = time.perf_counter()
        error: str | None = None
        answer = ""

        try:
            answer = self._chat(
                messages=[
                    {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=512,
            )
        except Exception as exc:
            error = str(exc)
            answer = "Unable to generate an answer at this time."

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _compact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        slim: dict[str, Any] = {}
        for k, v in r.items():
            if isinstance(v, str) and len(v) > 200:
                slim[k] = v[:200] + "…"
            else:
                slim[k] = v
        out.append(slim)
    return out


def build_default_llm_client(metrics: Metrics | None = None) -> OpenRouterLLMClient:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    return OpenRouterLLMClient(api_key=api_key, metrics=metrics)
