#!/usr/bin/env python3
"""CS Platform, HTTP API + static UI host.

Dependency-free (Python stdlib only) so it runs anywhere with no pip install,
ideal for a hackathon demo. Serves the single-pane-of-glass UI and a small JSON API
backed by the same WoW orchestration engine used by the CLI and the agent harness.

Run:  python3 platform/server.py           # http://localhost:8787
Endpoints:
  GET /api/portfolio            -> summary + accounts (health) + task queue + suppressed
  GET /api/accounts             -> accounts with health
  GET /api/accounts/{id}        -> full account detail (signals, health, tasks, suppressed)
  GET /api/tasks                -> prioritised task queue
  GET /api/suppressed           -> suppressed multi-instance signals
  GET /                         -> the CSM dashboard UI
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

UI_PATH = Path(__file__).resolve().parent / "ui" / "index.html"
PORT = int(os.environ.get("CS_PORT", "8787"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def log_message(self, *args):  # quiet console
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                if UI_PATH.exists():
                    self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(200, b"<h1>CS Platform</h1><p>UI not found.</p>", "text/html")
                return
            if path == "/api/portfolio":
                self._json(200, engine.portfolio()); return
            if path == "/api/accounts":
                self._json(200, engine.portfolio()["accounts"]); return
            if path == "/api/tasks":
                self._json(200, engine.portfolio()["tasks"]); return
            if path == "/api/suppressed":
                self._json(200, engine.portfolio()["suppressed"]); return
            if path == "/api/kpis":
                self._json(200, engine.kpis()); return
            if path == "/api/lifecycle":
                self._json(200, engine.lifecycle()); return
            if path == "/api/integrations":
                self._json(200, engine.integrations()); return
            if path.startswith("/api/accounts/"):
                acct = path.rsplit("/", 1)[-1]
                try:
                    self._json(200, engine.account_detail(acct))
                except KeyError:
                    self._json(404, {"error": f"unknown account {acct}"})
                return
            self._json(404, {"error": "not found", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        print(f"Could not bind port {port}: {exc}")
        print(f"  Another server may be running. Try a different port:  python3 platform/server.py 8790")
        print(f"  Or free it:  lsof -nP -iTCP:{port} -sTCP:LISTEN   then  kill <PID>")
        return 1
    print(f"CS Platform running: http://localhost:{port}")
    print(f"  API: http://localhost:{port}/api/portfolio")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
