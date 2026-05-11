"""Lightweight observability: JSON logging, in-process metrics, request_id tracing."""
from __future__ import annotations
import json
import logging
import os
import sys
import time
import uuid
from threading import Lock
from typing import Any

_LOGGER_NAME = "analytics_pipeline"
_DEFAULT_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class _JsonFormatter(logging.Formatter):
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if getattr(logger, "_configured", False):
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(_DEFAULT_LEVEL)
    logger.propagate = False
    logger._configured = True  # type: ignore[attr-defined]
    return logger


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class Metrics:
    """In-process metrics: counters + per-stage timing buffers. Thread-safe."""

    def __init__(self, history: int = 1000) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = {}
        self._timings: dict[str, list[float]] = {}
        self._tokens_total = 0
        self._llm_calls_total = 0
        self._history = history

    def incr(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + by

    def observe(self, name: str, value_ms: float) -> None:
        with self._lock:
            buf = self._timings.setdefault(name, [])
            buf.append(value_ms)
            if len(buf) > self._history:
                del buf[: len(buf) - self._history]

    def add_llm_usage(self, total_tokens: int, calls: int = 1) -> None:
        with self._lock:
            self._tokens_total += int(total_tokens or 0)
            self._llm_calls_total += int(calls or 0)

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
        return s[idx]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            timings_summary = {
                name: {
                    "count": len(vals),
                    "avg_ms": round(sum(vals) / len(vals), 2) if vals else 0.0,
                    "p50_ms": round(self._percentile(vals, 50), 2),
                    "p95_ms": round(self._percentile(vals, 95), 2),
                }
                for name, vals in self._timings.items()
            }
            return {
                "counters": counters,
                "timings": timings_summary,
                "llm_tokens_total": self._tokens_total,
                "llm_calls_total": self._llm_calls_total,
            }
