"""OpenRouter chat client for SQL generation and answer synthesis."""
from __future__ import annotations
import os
import re
import time
from typing import Any

DEFAULT_MODEL = "openai/gpt-5-nano"

_SQL_SYSTEM_PROMPT = (
    "You translate analytics questions into a single SQLite query.\n"
    "Rules:\n"
    '1. Output STRICT JSON: {"sql": "<query>"} or {"sql": null}.\n'
    "2. Use ONLY the table and columns listed below.\n"
    "3. Single statement. No semicolons mid-query.\n"
)

_ANSWER_SYSTEM_PROMPT = (
    "You are a concise analytics assistant. Answer in 1-3 sentences "
    "using ONLY the SQL result rows provided."
)

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


class OpenRouterLLMClient:
    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats = self._zero_stats()

    @staticmethod
    def _zero_stats() -> dict[str, int]:
        return {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    def pop_stats(self) -> dict[str, Any]:
        out = dict(self._stats)
        self._stats = self._zero_stats()
        return out

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        res = self._client.chat.send(
            messages=messages,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
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
        return content
