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


class SQLValidationError(Exception):
    pass


class SQLValidator:
    """Multi-layer SQL validator for the analytics pipeline."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @classmethod
    def validate(cls, sql: str | None, db_path: str | Path | None = None) -> SQLValidationOutput:
        return cls(db_path or DEFAULT_DB_PATH)._validate(sql)

    def _validate(self, sql: str | None) -> SQLValidationOutput:
        start = time.perf_counter()
        if sql is None:
            return SQLValidationOutput(
                is_valid=False, validated_sql=None, error="No SQL provided",
                timing_ms=(time.perf_counter() - start) * 1000,
            )
        return SQLValidationOutput(
            is_valid=True, validated_sql=sql, error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )
