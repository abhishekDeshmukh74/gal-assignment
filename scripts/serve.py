"""Local HTTP server for the benchmark dashboard.

Endpoints
---------
GET  /             → redirect to /dashboard
GET  /dashboard    → serve interactive HTML (auto-generate from samples if file missing)
POST /benchmark    → trigger a fresh benchmark run + dashboard regeneration in a background thread
GET  /status       → JSON: last run info, sample count, success rate
GET  /samples      → JSON array of raw per-sample data

Usage
-----
    OPENROUTER_API_KEY=sk-or-... PYTHONPATH=. python3 scripts/serve.py
    PYTHONPATH=. python3 scripts/serve.py --port 9000

No third-party deps — uses stdlib http.server only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SAMPLES_PATH = PROJECT_ROOT / "data" / "samples.jsonl"
DASHBOARD_PATH = PROJECT_ROOT / "reports" / "dashboard.html"

# Shared state (protected by _lock)
_lock = threading.Lock()
_state: dict = {
    "benchmark_running": False,
    "last_run_ts": None,
    "last_run_samples": 0,
    "last_run_success_rate": None,
    "error": None,
}


def _load_samples() -> list[dict]:
    if not SAMPLES_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in SAMPLES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _ensure_dashboard() -> bool:
    """Generate dashboard HTML if it doesn't exist or samples are newer. Returns True on success."""
    if not SAMPLES_PATH.exists():
        return False
    if DASHBOARD_PATH.exists() and DASHBOARD_PATH.stat().st_mtime >= SAMPLES_PATH.stat().st_mtime:
        return True
    try:
        subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "gen_dashboard.py"),
                str(SAMPLES_PATH),
                "--out",
                str(DASHBOARD_PATH),
                "--no-open",
            ],
            check=True,
            cwd=str(PROJECT_ROOT),
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _run_benchmark_thread(runs: int) -> None:
    with _lock:
        _state["benchmark_running"] = True
        _state["error"] = None

    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "benchmark.py"),
                "--runs",
                str(runs),
                "--save-samples",
                str(SAMPLES_PATH),
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            with _lock:
                _state["error"] = result.stderr[-500:] if result.stderr else "unknown error"
        else:
            samples = _load_samples()
            success_rate = (
                sum(1 for s in samples if s.get("status") == "success") / len(samples) * 100
                if samples
                else 0.0
            )
            with _lock:
                _state["last_run_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _state["last_run_samples"] = len(samples)
                _state["last_run_success_rate"] = round(success_rate, 2)
            _ensure_dashboard()
    finally:
        with _lock:
            _state["benchmark_running"] = False


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # silence default Apache-style logs
        print(f"  {self.address_string()} {fmt % args}")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, data: dict | list) -> None:
        body = json.dumps(data, indent=2).encode()
        self._send(code, "application/json", body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path in ("", "/"):
            self._redirect("/dashboard")

        elif path == "/dashboard":
            if not _ensure_dashboard():
                msg = (
                    b"<html><body style='font-family:sans-serif;padding:2rem;background:#0f172a;color:#e2e8f0'>"
                    b"<h2>No data yet</h2>"
                    b"<p>Run the benchmark first:</p>"
                    b"<pre style='background:#1e293b;padding:1rem;border-radius:8px'>"
                    b"curl -X POST http://localhost:8765/benchmark</pre>"
                    b"</body></html>"
                )
                self._send(200, "text/html; charset=utf-8", msg)
            else:
                body = DASHBOARD_PATH.read_bytes()
                self._send(200, "text/html; charset=utf-8", body)

        elif path == "/status":
            with _lock:
                payload = dict(_state)
            payload["samples_path"] = str(SAMPLES_PATH)
            payload["dashboard_path"] = str(DASHBOARD_PATH)
            payload["dashboard_exists"] = DASHBOARD_PATH.exists()
            self._send_json(200, payload)

        elif path == "/samples":
            samples = _load_samples()
            self._send_json(200, samples)

        else:
            self._send_json(404, {"error": f"Unknown route: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")

        if path == "/benchmark":
            content_length = int(self.headers.get("Content-Length", 0))
            body_raw = self.rfile.read(content_length) if content_length else b"{}"
            try:
                body = json.loads(body_raw) if body_raw else {}
            except json.JSONDecodeError:
                body = {}
            runs = int(body.get("runs", 3))

            with _lock:
                already_running = _state["benchmark_running"]

            if already_running:
                self._send_json(409, {"error": "A benchmark run is already in progress."})
                return

            thread = threading.Thread(target=_run_benchmark_thread, args=(runs,), daemon=True)
            thread.start()
            self._send_json(
                202,
                {
                    "message": f"Benchmark started ({runs} run(s)). Poll /status for progress.",
                    "runs": runs,
                },
            )

        else:
            self._send_json(404, {"error": f"Unknown route: {path}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the benchmark dashboard locally.")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("DASHBOARD_PORT", "8765")),
        help="Port to listen on (default: $DASHBOARD_PORT or 8765)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        help="Host to bind to (default: $DASHBOARD_HOST or 127.0.0.1)",
    )
    args = parser.parse_args()

    # Pre-generate dashboard if samples already exist
    _ensure_dashboard()

    server = HTTPServer((args.host, args.port), DashboardHandler)
    print(f"\n  Dashboard server running at http://{args.host}:{args.port}")
    print("  GET  /dashboard  — interactive HTML dashboard")
    print('  POST /benchmark  — trigger benchmark run  (body: {"runs": 3})')
    print("  GET  /status     — JSON run status")
    print("  GET  /samples    — raw sample data")
    print("\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
