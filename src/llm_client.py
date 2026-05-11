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


class OpenRouterLLMClient:
    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats: dict[str, int] = {
            "llm_calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0,
        }

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        res = self._client.chat.send(
            messages=messages,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        choices = getattr(res, "choices", None) or []
        if not choices:
            raise RuntimeError("OpenRouter response contained no choices.")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter response content is not text.")
        self._stats["llm_calls"] += 1
        return content.strip()
