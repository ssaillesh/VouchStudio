"""The engine behind hosted VouchStudio: a Vercel Python Function on the standard library.

Only the Next.js API routes call it, server to server, with `Authorization: Bearer
$ENGINE_SECRET`. It runs the same jobboard and user_template code as the local Studio, so a
hosted board score equals what `pipeline.py` reports for the same profile and job.

    POST /api/engine  {"action": "score", "filename": "...", "content_base64": "..." | "text": "...",
                       "index": {"url": "<presigned R2 GET>", "sha256": "..."}}
        -> {"profile": {...}, "scores": [...], "index_sha256": "...", "threshold": 65}
    POST /api/engine  {"action": "header", "header": {...}}  -> {"header": {...}} (validated)
    POST /api/engine  {"action": "constants"}                 -> threshold, levels and limits

`text` is a PDF's text extracted by the caller (Vercel has no pdftotext). The job index is
the requirements-only file scripts/publish-jobs.mjs uploads to R2; it is fetched once per
warm instance and checked against its SHA-256. Profiles are parsed in memory and /tmp,
and are never logged.

Local use with `next dev`:  ENGINE_SECRET=... python3 api/engine.py   (http://127.0.0.1:8787)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jobboard  # noqa: E402
import pipeline  # noqa: E402
import resume_kit  # noqa: E402
import user_template  # noqa: E402

MAX_BODY_BYTES = 4_000_000          # Vercel caps a function request at 4.5 MB
MAX_INDEX_BYTES = 60_000_000
INDEX_TIMEOUT = 30
DEV_PORT = 8787

_index_lock = threading.Lock()
_index: dict[str, Any] = {"sha256": None, "jobs": None}


class EngineError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _authorized(header: str | None, secret: str | None) -> bool:
    if not secret:
        return False
    expected = f"Bearer {secret}"
    return hmac.compare_digest((header or "").encode(), expected.encode())


def fetch_index(url: str, sha256: str, opener: Any = urllib.request.urlopen) -> list[dict[str, Any]]:
    """The published job index, cached per SHA-256 for the life of this instance."""
    with _index_lock:
        if _index["sha256"] == sha256 and _index["jobs"] is not None:
            return _index["jobs"]
    if urllib.parse.urlsplit(url).scheme != "https":
        raise EngineError(400, "The index URL must be https")
    try:
        with opener(urllib.request.Request(url, method="GET"), timeout=INDEX_TIMEOUT) as response:
            data = response.read(MAX_INDEX_BYTES + 1)
    except OSError as exc:
        raise EngineError(502, f"The job index could not be read ({type(exc).__name__})") from None
    if len(data) > MAX_INDEX_BYTES:
        raise EngineError(502, "The job index is too large")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise EngineError(502, "The job index does not match its published SHA-256")
    jobs = json.loads(data)["jobs"]
    with _index_lock:
        _index.update(sha256=sha256, jobs=jobs)
    return jobs


def constants() -> dict[str, Any]:
    return {"threshold": pipeline.FIT_THRESHOLD, "verdict_proceed": pipeline.VERDICT_PROCEED,
            "levels": list(jobboard.LEVELS),
            "min_skill_requirements": jobboard.MIN_SKILL_REQUIREMENTS,
            "max_profile_bytes": jobboard.MAX_UPLOAD_BYTES,
            "index_version": jobboard.INDEX_VERSION}


def score(body: dict[str, Any], opener: Any = urllib.request.urlopen) -> dict[str, Any]:
    filename = str(body.get("filename") or "")
    text = body.get("text")
    try:
        data = base64.b64decode(body.get("content_base64") or "", validate=True)
    except (ValueError, binascii.Error):
        raise EngineError(422, "The upload is not valid base64") from None
    index = body.get("index") or {}
    if not isinstance(index.get("url"), str) or not isinstance(index.get("sha256"), str):
        raise EngineError(400, "index.url and index.sha256 are required")
    if Path(filename).suffix.lower() not in jobboard.PROFILE_EXTENSIONS:
        raise EngineError(422, "Upload a .md, .txt or .pdf file")
    try:
        source = jobboard.readable_text(text) if isinstance(text, str) \
            else jobboard.decode_upload(filename, data)
        profile = jobboard.load_profile(filename, b"", source)
    except jobboard.ProfileError as exc:
        raise EngineError(422, str(exc)) from None
    jobs = fetch_index(index["url"], index["sha256"], opener)
    summary = {**profile.summary(), "is_master": jobboard.is_master(source),
               "has_kit": resume_kit.has_kit(source.splitlines())}
    return {"profile": summary, "scores": jobboard.score_jobs(profile, jobs),
            "index_sha256": index["sha256"], "threshold": pipeline.FIT_THRESHOLD}


def clean_header(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("header")
    if not isinstance(raw, dict):
        raise EngineError(400, "header must be an object")
    try:
        return {"header": user_template.clean_header(raw)}
    except user_template.TemplateInputError as exc:
        raise EngineError(422, str(exc)) from None


ACTIONS = {"score": score, "header": clean_header, "constants": lambda _body: constants()}


def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = ACTIONS.get(str(body.get("action")))
    if action is None:
        raise EngineError(400, f"action is one of: {', '.join(ACTIONS)}")
    return action(body)


class handler(BaseHTTPRequestHandler):  # Vercel looks for a class with this name
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        self._send(200, {"ok": True})

    def do_POST(self) -> None:  # noqa: N802
        if not _authorized(self.headers.get("Authorization"), os.environ.get("ENGINE_SECRET")):
            self._send(401, {"detail": "Unauthorized"})
            return
        length = self.headers.get("Content-Length") or ""
        if not length.isdigit() or int(length) > MAX_BODY_BYTES:
            self._send(413, {"detail": "Request body missing or too large"})
            return
        try:
            body = json.loads(self.rfile.read(int(length)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self._send(400, {"detail": "The body must be a JSON object"})
            return
        try:
            self._send(200, handle(body))
        except EngineError as exc:
            self._send(exc.status, {"detail": exc.detail})
        except Exception as exc:  # noqa: BLE001 - answer JSON; the profile is never echoed
            self._send(500, {"detail": f"Engine error ({type(exc).__name__})"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # request lines only; nothing about the profile is logged


if __name__ == "__main__":
    if not os.environ.get("ENGINE_SECRET"):
        sys.exit("Set ENGINE_SECRET (the same value as the Next.js app's) first.")
    server = ThreadingHTTPServer(("127.0.0.1", DEV_PORT), handler)
    print(f"Engine on http://127.0.0.1:{DEV_PORT}/api/engine  (Ctrl+C to stop)", flush=True)
    server.serve_forever()
