"""Analytics pipeline: question -> SQL -> validate -> execute -> answer."""
from __future__ import annotations
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.types import SQLValidationOutput

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "gaming_mental_health.sqlite"
_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


class SQLValidationError(Exception):
    pass


class SQLValidator:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @classmethod
    def validate(cls, sql: str | None, db_path: str | Path | None = None) -> SQLValidationOutput:
        return cls(db_path or DEFAULT_DB_PATH)._validate(sql)

    @staticmethod
    def _normalize(sql: str) -> str:
        s = _COMMENT_BLOCK_RE.sub(" ", sql)
        s = _COMMENT_LINE_RE.sub(" ", s)
        s = s.strip().rstrip(";").strip()
        s = re.sub(r"\s+", " ", s)
        return s

    def _validate(self, sql: str | None) -> SQLValidationOutput:
        start = time.perf_counter()
        if sql is None:
            return self._fail(start, "No SQL provided")
        normalized = self._normalize(sql)
        if not normalized:
            return self._fail(start, "Empty SQL after normalization")
        return SQLValidationOutput(
            is_valid=True, validated_sql=normalized, error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )

    @staticmethod
    def _fail(start: float, msg: str) -> SQLValidationOutput:
        return SQLValidationOutput(
            is_valid=False, validated_sql=None, error=msg,
            timing_ms=(time.perf_counter() - start) * 1000,
        )
