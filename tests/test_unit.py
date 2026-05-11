"""Unit tests — no OpenRouter API key required.

Covers SQLValidator (lexical / structural / semantic / LIMIT injection),
SQLiteExecutor, OpenRouterLLMClient._extract_sql, and token accounting via
a stubbed transport. All tests use the live SQLite DB built by
scripts/gaming_csv_to_db.py if available, otherwise are skipped.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.llm_client import OpenRouterLLMClient
from src.observability import Metrics
from src.pipeline import SQLiteExecutor, SQLValidator
from src.types import SQLValidationOutput


def _build_tiny_db() -> Path:
    """Create a small fixture DB with the same table name the validator expects."""
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="gmh_test_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE gaming_mental_health (age INTEGER, gender TEXT, "
        "addiction_level REAL, anxiety_score REAL)"
    )
    conn.executemany(
        "INSERT INTO gaming_mental_health VALUES (?, ?, ?, ?)",
        [
            (21, "Male", 5.5, 4.2),
            (35, "Female", 2.1, 6.0),
            (28, "Male", 7.0, 7.7),
        ],
    )
    conn.commit()
    conn.close()
    return Path(path)


class SQLValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_path = _build_tiny_db()
        cls.validator = SQLValidator(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.db_path.unlink()
        except FileNotFoundError:
            pass

    def test_select_passes(self) -> None:
        out = self.validator._validate(
            "SELECT gender, AVG(addiction_level) FROM gaming_mental_health GROUP BY gender"
        )
        self.assertTrue(out.is_valid, out.error)
        self.assertIsNotNone(out.validated_sql)

    def test_cte_passes(self) -> None:
        out = self.validator._validate(
            "WITH t AS (SELECT gender, AVG(addiction_level) a FROM gaming_mental_health GROUP BY gender) "
            "SELECT * FROM t"
        )
        self.assertTrue(out.is_valid, out.error)

    def test_delete_rejected(self) -> None:
        out = self.validator._validate("DELETE FROM gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_insert_rejected(self) -> None:
        out = self.validator._validate("INSERT INTO gaming_mental_health VALUES (1,'M',1.0,1.0)")
        self.assertFalse(out.is_valid)

    def test_drop_rejected(self) -> None:
        out = self.validator._validate("DROP TABLE gaming_mental_health")
        self.assertFalse(out.is_valid)

    def test_pragma_rejected(self) -> None:
        out = self.validator._validate("PRAGMA table_info(gaming_mental_health)")
        self.assertFalse(out.is_valid)

    def test_multi_statement_rejected(self) -> None:
        out = self.validator._validate("SELECT 1; SELECT 2")
        self.assertFalse(out.is_valid)

    def test_unknown_column_rejected_via_explain(self) -> None:
        out = self.validator._validate("SELECT zodiac_sign FROM gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIn("planner", (out.error or "").lower())

    def test_limit_injected_when_missing(self) -> None:
        out = self.validator._validate("SELECT age FROM gaming_mental_health WHERE age > 18")
        self.assertTrue(out.is_valid)
        self.assertIn("LIMIT 100", out.validated_sql or "")

    def test_limit_not_added_when_aggregate_present(self) -> None:
        out = self.validator._validate("SELECT COUNT(*) FROM gaming_mental_health")
        self.assertTrue(out.is_valid)
        self.assertNotIn("LIMIT", (out.validated_sql or "").upper())

    def test_none_input(self) -> None:
        out = self.validator._validate(None)
        self.assertFalse(out.is_valid)

    def test_returns_typed_output(self) -> None:
        out = self.validator._validate("SELECT 1")
        self.assertIsInstance(out, SQLValidationOutput)
        self.assertGreater(out.timing_ms, 0.0)


class SQLiteExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_path = _build_tiny_db()
        cls.exec = SQLiteExecutor(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.db_path.unlink()
        except FileNotFoundError:
            pass

    def test_runs_known_query(self) -> None:
        out = self.exec.run("SELECT COUNT(*) AS n FROM gaming_mental_health")
        self.assertIsNone(out.error)
        self.assertEqual(out.rows[0]["n"], 3)

    def test_surfaces_error(self) -> None:
        out = self.exec.run("SELECT bogus FROM gaming_mental_health")
        self.assertIsNotNone(out.error)
        self.assertEqual(out.row_count, 0)

    def test_none_sql_is_safe(self) -> None:
        out = self.exec.run(None)
        self.assertIsNone(out.error)
        self.assertEqual(out.row_count, 0)


class ExtractSqlTests(unittest.TestCase):
    def test_json_envelope(self) -> None:
        s = OpenRouterLLMClient._extract_sql('{"sql": "SELECT 1"}')
        self.assertEqual(s, "SELECT 1")

    def test_json_null(self) -> None:
        s = OpenRouterLLMClient._extract_sql('{"sql": null}')
        self.assertIsNone(s)

    def test_raw_select(self) -> None:
        s = OpenRouterLLMClient._extract_sql("Sure! SELECT a FROM t")
        self.assertEqual(s, "SELECT a FROM t")

    def test_with_cte(self) -> None:
        s = OpenRouterLLMClient._extract_sql("with x as (select 1) select * from x")
        self.assertTrue(s and s.lower().startswith("with"))

    def test_markdown_fence(self) -> None:
        s = OpenRouterLLMClient._extract_sql('```json\n{"sql": "SELECT 1"}\n```')
        self.assertEqual(s, "SELECT 1")

    def test_no_sql(self) -> None:
        s = OpenRouterLLMClient._extract_sql("I have no idea.")
        self.assertIsNone(s)


class TokenAccountingTests(unittest.TestCase):
    """Stub the OpenRouter SDK and verify token counters accumulate."""

    def _build_client_with_stub(self, response_obj):
        # Avoid importing the real SDK; patch the attribute directly.
        client = OpenRouterLLMClient.__new__(OpenRouterLLMClient)
        client.model = "stub/model"
        client._stats = OpenRouterLLMClient._zero_stats()
        client._metrics = Metrics()
        client._client = SimpleNamespace(chat=SimpleNamespace(send=lambda **_: response_obj))
        from src.observability import get_logger

        client._log = get_logger()
        return client

    def _response(self, content: str, prompt: int, completion: int, total: int):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=prompt, completion_tokens=completion, total_tokens=total
            ),
        )

    def test_usage_is_counted(self) -> None:
        client = self._build_client_with_stub(self._response("hi", 10, 5, 15))
        text = client._chat([{"role": "user", "content": "x"}], 0.0, 50)
        self.assertEqual(text, "hi")
        self.assertEqual(client._stats["llm_calls"], 1)
        self.assertEqual(client._stats["prompt_tokens"], 10)
        self.assertEqual(client._stats["completion_tokens"], 5)
        self.assertEqual(client._stats["total_tokens"], 15)
        self.assertEqual(client._metrics.snapshot()["llm_tokens_total"], 15)

    def test_missing_usage_falls_back_to_estimate(self) -> None:
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello world"))],
            usage=None,
        )
        client = self._build_client_with_stub(resp)
        client._chat([{"role": "user", "content": "abcd" * 10}], 0.0, 50)
        self.assertEqual(client._stats["llm_calls"], 1)
        self.assertGreater(client._stats["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
