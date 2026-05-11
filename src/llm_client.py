"""OpenRouter chat client for SQL generation and answer synthesis."""
from __future__ import annotations
import os
import re
from typing import Any

DEFAULT_MODEL = "openai/gpt-5-nano"

_SQL_SYSTEM_PROMPT = (
    "You translate analytics questions into a single SQLite query.\n"
    "Rules:\n"
    '1. Output STRICT JSON: {"sql": "<query>"} or {"sql": null} ONLY.\n'
    "2. Use ONLY the table and columns listed below.\n"
    "3. Single statement. No semicolons mid-query.\n"
)

_ANSWER_SYSTEM_PROMPT = (
    "You are a concise analytics assistant. Answer the user's question in 1-3 "
    "sentences using ONLY the SQL result rows provided."
)

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
