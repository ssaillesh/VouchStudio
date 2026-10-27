"""Offline HTTP stand-ins for the fetch_jobs.py tests. No test touches the network."""

from __future__ import annotations

import email.message
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fetch_jobs():
    if "fetch_jobs" in sys.modules:
        return sys.modules["fetch_jobs"]
    spec = importlib.util.spec_from_file_location("fetch_jobs", ROOT / "fetch_jobs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_jobs"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_bytes(name: str) -> bytes:
    """Raw body; use for JSON arrays, which FakeOpener would otherwise read as a sequence."""
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, content_type: str = "application/json"):
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = f"{content_type}; charset=utf-8"

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Routes requests by URL substring to canned bodies, HTTP errors or exceptions."""

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = dict(routes or {})
        self.requests: list[Any] = []

    def __call__(self, request, timeout=None):
        assert timeout is not None, "every request needs an explicit timeout"
        self.requests.append(request)
        url = request.full_url
        for needle, response in self.routes.items():
            if needle in url:
                if isinstance(response, list):  # sequence of responses for retries
                    response = response.pop(0) if len(response) > 1 else response[0]
                return self._respond(url, response)
        raise AssertionError(f"unexpected request to {url}")

    @staticmethod
    def _respond(url: str, response: Any):
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, tuple):  # (status, body, headers)
            status, body, headers = (*response, {})[:3]
            message = email.message.Message()
            for key, value in headers.items():
                message[key] = value
            raise urllib.error.HTTPError(url, status, "error", message,
                                         io.BytesIO(body if isinstance(body, bytes)
                                                    else json.dumps(body).encode()))
        if isinstance(response, bytes):
            return FakeResponse(response)
        return FakeResponse(json.dumps(response).encode())

    def bodies(self) -> list[Any]:
        return [json.loads(r.data.decode()) if r.data else None for r in self.requests]
