#!/usr/bin/env python3
"""Local job board app: python3 app.py, then open http://127.0.0.1:8765

Serves the static front end in web/ and a small JSON API:

    GET  /api/jobs      every indexed job (no requirements, no scores)
    GET  /api/meta      counts and index time
    POST /api/score     {"filename": "...", "content_base64": "..."} -> profile + scores
    POST /api/reindex   rebuild the index after fetch_jobs.py (local requests only)

The server is stateless: an uploaded resume is parsed in memory for one request
and never written to disk or logged. That shape is what a hosted back end
(Render, Fly, etc.) would expose later, with web/ served from Vercel. Standard
library only.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jobboard

WEB_FILES = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css",
             "/app.js": "app.js"}
MAX_BODY_BYTES = 3_000_000  # base64 of a 2 MB upload plus JSON overhead
DRAIN_LIMIT_BYTES = 12_000_000  # beyond this, the connection is simply closed


class BoardState:
    """Index held in memory; rebuilt only on request."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.lock = threading.Lock()
        self.index = jobboard.load_or_build_index(workspace)

    def reindex(self) -> None:
        index = jobboard.load_or_build_index(self.workspace)
        with self.lock:
            self.index = index

    def jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.index["jobs"])


def make_handler(state: BoardState, web_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "jobauto"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # never log request bodies
            sys.stderr.write(f"{self.command} {self.path.split('?')[0]} -> "
                             f"{args[1] if len(args) > 1 else ''}\n")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                             "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _drain(self, remaining: int) -> None:
            """Read and discard a rejected body so the client receives the error reply."""
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in WEB_FILES:
                file = web_dir / WEB_FILES[path]
                kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                self._send(HTTPStatus.OK, file.read_bytes(),
                           f"{kind}; charset=utf-8" if kind.startswith("text/") or
                           kind.endswith("javascript") else kind)
            elif path == "/api/jobs":
                self._json(HTTPStatus.OK, [jobboard.public_job(j) for j in state.jobs()])
            elif path == "/api/meta":
                jobs = state.jobs()
                self._json(HTTPStatus.OK, {
                    "jobs": len(jobs),
                    "scorable": sum(1 for j in jobs if jobboard.has_enough_detail(j)),
                    "not_enough_detail": sum(1 for j in jobs if j["requirements"]
                                             and not jobboard.has_enough_detail(j)),
                    "min_skill_requirements": jobboard.MIN_SKILL_REQUIREMENTS,
                    "generated": state.index.get("generated"),
                    "threshold": jobboard.pipeline.FIT_THRESHOLD,
                    "levels": list(jobboard.LEVELS)})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/reindex":
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "reindex is local only"})
                    return
                state.reindex()
                self._json(HTTPStatus.OK, {"jobs": len(state.jobs())})
                return
            if path != "/api/score":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY_BYTES:
                if length > MAX_BODY_BYTES:
                    self._drain(min(length, DRAIN_LIMIT_BYTES))
                    self.close_connection = True
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > MAX_BODY_BYTES
                           else HTTPStatus.BAD_REQUEST, {"error": "upload is empty or too large"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                filename = str(body["filename"])[:200]
                data = base64.b64decode(body["content_base64"], validate=True)
            except (ValueError, KeyError, TypeError, binascii.Error):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "expected filename and "
                                                             "content_base64"})
                return
            try:
                profile = jobboard.load_profile(filename, data)
            except jobboard.ProfileError as exc:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                return
            scores = jobboard.score_jobs(profile, state.jobs())
            self._json(HTTPStatus.OK, {"profile": profile.summary(), "scores": scores,
                                       "threshold": jobboard.pipeline.FIT_THRESHOLD})

    return Handler


def serve(workspace: Path, host: str, port: int) -> ThreadingHTTPServer:
    state = BoardState(workspace)
    server = ThreadingHTTPServer((host, port), make_handler(state, workspace / "web"))
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.py", description="Local job board app")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1: this computer only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    print("Loading the job index (first run indexes every job; later runs are quick)...")
    server = serve(args.workspace.resolve(), args.host, args.port)
    print(f"Job board running at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
