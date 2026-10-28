#!/usr/bin/env python3
"""Multi-source job ingestion for the local resume pipeline.

Usage:
    python3 fetch_jobs.py                       # all free sources (never Apify)
    python3 fetch_jobs.py --source greenhouse
    python3 fetch_jobs.py --source jobright --keyword "SRE" --location "Toronto" \
        --limit 10 --allow-apify-spend
    python3 fetch_jobs.py --dry-run --verbose

Retrieves public job postings from Greenhouse and Ashby job boards, the
SimplifyJobs listing repositories, the Jooble REST API and the Jobright Apify
actor. It normalizes them into one record type, deduplicates across sources and
writes Markdown files to target_jobs/ that pipeline.py reads unchanged.

This module is the only part of the project allowed to make network requests,
and only to retrieve job postings. It never touches the master profile, the
template or pipeline outputs. Standard library only.

Exit codes: 0 success (including "nothing new"), 1 fatal configuration or
filesystem error / nothing runnable, 2 at least one selected source failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import socket
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

VERSION = "1.0.0"
USER_AGENT = f"jobauto-fetch-jobs/{VERSION} (local job ingestion; +https://github.com/ssaillesh)"

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_SOURCE_FAILED = 2

SOURCE_LABELS = {
    "greenhouse": "Greenhouse",
    "ashby": "Ashby",
    "simplify": "Simplify",
    "jooble": "Jooble",
    "jobright": "Jobright",
}
SOURCE_KEYS = {label: key for key, label in SOURCE_LABELS.items()}
FREE_SOURCES = ("greenhouse", "ashby", "simplify", "jooble")
# Lower rank wins when the same posting comes from several sources.
SOURCE_RANK = {"Greenhouse": 0, "Ashby": 0, "Jobright": 1, "Jooble": 2, "Simplify": 3}

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"
SIMPLIFY_RAW_URL = "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
JOOBLE_URL = "https://{host}/api/{key}"
APIFY_ACTOR = "jobscrawler~jobright-scraper"
APIFY_URL = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
APIFY_SYNC_LIMIT_SECONDS = 300  # Apify caps synchronous runs; longer runs return 408

DEFAULT_TITLE_KEYWORDS = ["DevOps", "Platform", "SRE", "Infrastructure", "Site Reliability",
                          "Backend"]
DEFAULT_LOCATIONS = ["Toronto", "Canada", "Remote"]          # Jooble search locations
DEFAULT_FILTER_LOCATIONS = ["Toronto", "Canada", "United States"]
DEFAULT_EXCLUDE_TITLES = ["Senior", "Sr", "Staff", "Principal", "Lead", "Director", "Manager",
                          "Head", "VP", "Distinguished",
                          # non-engineering titles that contain "Platform" or "Infrastructure"
                          "Account Executive", "Sales", "Designer", "Strategy",
                          "Research Scientist", "Researcher", "Physicist", "Librarian"]
DEFAULT_JOOBLE_KEYWORDS = ["DevOps Engineer", "Platform Engineer", "Site Reliability Engineer",
                           "Infrastructure Engineer", "Backend Engineer"]

MANIFEST_NAME = ".fetch_manifest.json"
GENERATED_MARKER = "fetch_jobs.py"
MAX_COMPANY_SLUG = 40
MAX_ROLE_SLUG = 60
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_name", "gclid", "fbclid", "msclkid", "dclid", "mc_cid", "mc_eid", "gh_src",
    "ref", "referrer", "trk", "trkinfo", "lever-source", "lever-origin", "_hsenc", "_hsmi",
    "igshid", "source_id", "src_id",
})


class FetchError(Exception):
    """A source-level failure; the message must never contain credentials."""


class ConfigError(Exception):
    """Invalid configuration; fatal for the run."""


class HttpError(FetchError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BudgetExceeded(FetchError):
    pass


# ---------------------------------------------------------------------------
# Unified job record
# ---------------------------------------------------------------------------
@dataclass
class JobRecord:
    source: str                         # Greenhouse | Ashby | Simplify | Jooble | Jobright
    source_job_id: str | None
    company: str
    title: str
    location: str | None
    url: str
    description: str
    description_complete: bool
    completeness_note: str | None = None
    posted_date: str | None = None      # YYYY-MM-DD as published by the source
    updated_date: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    department: str | None = None
    application_url: str | None = None
    salary: str | None = None
    terms: list[str] = field(default_factory=list)
    also_seen_at: list[str] = field(default_factory=list)

    def identity(self) -> str:
        return strong_identity(self.url) or f"url:{canonical_url(self.url)}"


def validate_record(record: JobRecord) -> list[str]:
    """Reasons a record must not be written (empty list means valid)."""
    problems = []
    if record.source not in SOURCE_RANK:
        problems.append(f"unsupported source {record.source!r}")
    if not (record.company or "").strip():
        problems.append("missing company")
    if not (record.title or "").strip():
        problems.append("missing title")
    if not is_http_url(record.url):
        problems.append("missing or invalid posting URL")
    if not (record.description or "").strip() and record.description_complete:
        problems.append("missing description")
    if not record.description_complete and not record.completeness_note:
        problems.append("partial description without a completeness note")
    return problems


def is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urllib.parse.urlsplit(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and " " not in value


# ---------------------------------------------------------------------------
# Secrets and logging
# ---------------------------------------------------------------------------
class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self.secrets = [s for s in secrets if s and len(s) >= 4]

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= 4 and secret not in self.secrets:
            self.secrets.append(secret)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
            text = text.replace(urllib.parse.quote(secret, safe=""), "***")
        return text


# ---------------------------------------------------------------------------
# HTTP client with retries, budgets and an optional file cache
# ---------------------------------------------------------------------------
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60


@dataclass
class HttpStats:
    requests: int = 0
    cache_hits: int = 0


class HttpClient:
    def __init__(self, *, redactor: Redactor, timeout: float = 20.0, max_retries: int = 2,
                 backoff: float = 1.0, budgets: dict[str, int] | None = None,
                 cache_dir: Path | None = None, cache_ttl: int = 3600, refresh: bool = False,
                 opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, jitter: float = 0.25) -> None:
        self.redact = redactor
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.budgets = dict(budgets or {})
        self.cache_dir = cache_dir
        self.cache_ttl = cache_ttl
        self.refresh = refresh
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.clock = clock
        self.jitter = jitter
        self.stats: dict[str, HttpStats] = {}
        self.log: list[str] = []

    def _stats(self, source: str) -> HttpStats:
        return self.stats.setdefault(source, HttpStats())

    def get_json(self, url: str, *, source: str, timeout: float | None = None,
                 max_bytes: int = 5_000_000, headers: dict[str, str] | None = None,
                 cacheable: bool = False) -> Any:
        if cacheable and not headers:
            cached = self._cache_read(url)
            if cached is not None:
                self._stats(source).cache_hits += 1
                self.log.append(f"{source}: cache hit {self.redact(url)}")
                return cached
        text = self._request("GET", url, source=source, timeout=timeout, max_bytes=max_bytes,
                             headers=headers)
        data = self._parse(text, source, url)
        if cacheable and not headers:
            self._cache_write(url, text)
        return data

    def post_json(self, url: str, body: Any, *, source: str, timeout: float | None = None,
                  max_bytes: int = 5_000_000, headers: dict[str, str] | None = None) -> Any:
        payload = json.dumps(body).encode("utf-8")
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        text = self._request("POST", url, source=source, timeout=timeout, max_bytes=max_bytes,
                             headers=all_headers, data=payload)
        return self._parse(text, source, url)

    def _parse(self, text: str, source: str, url: str) -> Any:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise FetchError(f"{source}: malformed JSON from {self.redact(_host(url))}") from exc

    def _request(self, method: str, url: str, *, source: str, timeout: float | None,
                 max_bytes: int, headers: dict[str, str] | None = None,
                 data: bytes | None = None) -> str:
        shown = self.redact(url)
        attempt = 0
        while True:
            stats = self._stats(source)
            budget = self.budgets.get(source)
            if budget is not None and stats.requests >= budget:
                raise BudgetExceeded(f"{source}: request budget of {budget} reached")
            stats.requests += 1
            request = urllib.request.Request(url, data=data, method=method, headers={
                "User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
            self.log.append(f"{source}: {method} {shown}")
            try:
                with self.opener(request, timeout=timeout or self.timeout) as response:
                    status = getattr(response, "status", 200)
                    raw = response.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raise FetchError(f"{source}: response from {_host(url)} exceeds "
                                         f"{max_bytes} bytes")
                    if status != 200:
                        raise HttpError(f"{source}: HTTP {status} from {_host(url)}", status)
                    charset = response.headers.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After")
                                                   if exc.headers else None)
                detail = _safe_error_detail(exc, self.redact)
                exc.close()
                if status in RETRYABLE_STATUS and attempt < self.max_retries:
                    wait = retry_after if retry_after is not None else self._backoff(attempt)
                    if wait <= MAX_RETRY_AFTER_SECONDS:
                        self.log.append(f"{source}: HTTP {status}, retrying in {wait:.1f}s")
                        self.sleep(wait)
                        attempt += 1
                        continue
                raise HttpError(f"{source}: HTTP {status} from {_host(url)}{detail}",
                                status) from None
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                if attempt < self.max_retries:
                    wait = self._backoff(attempt)
                    self.log.append(f"{source}: network error, retrying in {wait:.1f}s")
                    self.sleep(wait)
                    attempt += 1
                    continue
                raise FetchError(f"{source}: network error contacting {_host(url)}: "
                                 f"{self.redact(str(reason))}") from None

    def _backoff(self, attempt: int) -> float:
        return min(30.0, self.backoff * (2 ** attempt)) + random.uniform(0, self.jitter)

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")

    def _cache_read(self, url: str) -> Any:
        path = self._cache_path(url)
        if path is None or self.refresh or not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if self.clock() - float(entry["fetched_at"]) > self.cache_ttl:
                return None
            return json.loads(entry["body"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _cache_write(self, url: str, text: str) -> None:
        path = self._cache_path(url)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps({"url": self.redact(url), "fetched_at": self.clock(),
                                           "body": text}))
        except OSError:
            self.log.append("cache write failed (ignored)")


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or "source"


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = dt.datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())


def _safe_error_detail(exc: urllib.error.HTTPError, redact: Redactor) -> str:
    """Only a short, structured error type from JSON bodies; never the full body."""
    try:
        body = exc.read(4096).decode("utf-8", errors="replace")
        data = json.loads(body)
    except (OSError, ValueError, AttributeError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    if isinstance(kind, str) and re.fullmatch(r"[a-z0-9_.-]{1,60}", kind):
        return f" ({redact(kind)})"
    return ""


# ---------------------------------------------------------------------------
# HTML to readable Markdown (no rewriting of content)
# ---------------------------------------------------------------------------
class _MarkdownBuilder(HTMLParser):
    BLOCK_TAGS = frozenset({"p", "div", "section", "article", "header", "main", "aside",
                            "blockquote", "table", "thead", "tbody", "tr", "dl", "dt", "dd",
                            "figure", "figcaption", "pre", "address", "center"})
    SKIP_TAGS = frozenset({"script", "style", "noscript", "iframe", "svg", "head", "nav",
                           "footer", "form", "button", "select", "template", "object",
                           "textarea", "math"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []   # (kind, text)
        self.buf: list[str] = []
        self.skip = 0
        self.lists: list[dict[str, Any]] = []
        self.links: list[str | None] = []
        self.heading: int | None = None
        self.prefix: str | None = None
        self.bold = 0
        self.italic = 0
        self.in_li = 0
        self.list_group = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.heading = int(tag[1])
        elif tag in self.BLOCK_TAGS:
            self._flush()
        elif tag == "br":
            self._flush(kind="line")
        elif tag in ("ul", "ol"):
            self._flush()
            if not self.lists:
                self.list_group += 1
            self.lists.append({"type": tag, "count": 0})
        elif tag == "li":
            self._flush()
            self.in_li += 1
            depth = max(len(self.lists) - 1, 0)
            if self.lists:
                current = self.lists[-1]
                current["count"] += 1
                marker = f"{current['count']}. " if current["type"] == "ol" else "- "
            else:
                marker = "- "
            self.prefix = "  " * depth + marker
        elif tag in ("strong", "b"):
            if not self.bold:
                self.buf.append("**")
            self.bold += 1
        elif tag in ("em", "i"):
            if not self.italic and not self.bold:
                self.buf.append("_")
            self.italic += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            safe = href if is_http_url(href) else None
            self.links.append(safe)
            if safe:
                self.buf.append("[")
        elif tag in ("td", "th"):
            self.buf.append(" ")
        elif tag == "code":
            self.buf.append("`")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self.skip:
            self._flush(kind="line")
        elif tag not in self.SKIP_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.heading = None
        elif tag in self.BLOCK_TAGS:
            self._flush()
        elif tag == "li":
            self._flush()
            self.in_li = max(0, self.in_li - 1)
            self.prefix = None
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
            # text after a nested list continues the enclosing item
            self.prefix = ("  " * len(self.lists) if self.lists and self.in_li else None)
        elif tag in ("strong", "b"):
            self.bold = max(0, self.bold - 1)
            if not self.bold:
                self.buf.append("**")
        elif tag in ("em", "i"):
            self.italic = max(0, self.italic - 1)
            if not self.italic and not self.bold:
                self.buf.append("_")
        elif tag == "a":
            href = self.links.pop() if self.links else None
            if href:
                self.buf.append(f"]({href})")
        elif tag in ("td", "th"):
            self.buf.append(" ")
        elif tag == "code":
            self.buf.append("`")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.buf.append(data)

    def _flush(self, kind: str | None = None) -> None:
        text = _clean_inline("".join(self.buf))
        self.buf = []
        if self.bold:
            self.buf.append("**")  # re-open bold that spans blocks
        if not text:
            return
        if self.heading is not None:
            level = min(self.heading + 2, 6)
            self.blocks.append(("heading", "#" * level + " " + text.replace("**", "").strip()))
        elif self.prefix is not None:
            self.blocks.append((f"item{self.list_group}", self.prefix + text))
            self.prefix = "  " * max(len(self.lists) - 1, 0) + "  "  # continuation lines
        elif kind == "line":
            self.blocks.append(("line", text))
        else:
            self.blocks.append(("para", text))

    def markdown(self) -> str:
        self._flush()
        out: list[str] = []
        previous = None
        for kind, text in self.blocks:
            if out:
                tight = (kind.startswith("item") and kind == previous) or (
                    previous == "line" and kind in ("line", "para"))
                out.append("\n" if tight else "\n\n")
            out.append(text)
            previous = kind
        return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _clean_inline(text: str) -> str:
    """Collapse whitespace and tidy bold markers so '** Skills**' becomes '**Skills**'."""
    text = re.sub(r"[ \t\r\f\v\n ]+", " ", text)
    parts = text.split("**")
    if len(parts) % 2 == 0:  # unbalanced marker: drop the last one
        tail = parts.pop()
        parts[-1] += tail
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            out.append(part)
            continue
        inner = part.strip()
        lead = " " if part[:1].isspace() else ""
        trail = " " if part[-1:].isspace() else ""
        out.append(f"{lead}**{inner}**{trail}" if inner else lead or trail)
    text = re.sub(r" {2,}", " ", "".join(out))
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    return text.strip()


def html_to_markdown(source: str) -> str:
    """Convert posting HTML to Markdown without summarizing or rewriting it."""
    if not source or not source.strip():
        return ""
    builder = _MarkdownBuilder()
    builder.feed(source)
    builder.close()
    return builder.markdown()


def plain_text(source: str) -> str:
    """Normalize a plain-text description while keeping its paragraphs."""
    text = unicodedata.normalize("NFC", source or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


INVISIBLE_CHARS = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
RESIDUAL_TAG = re.compile(r"[ \t]*</?(?:p|div|span|br|li|ul|ol|strong|b|em|i|u|a|h[1-6]|table|thead|"
                          r"tbody|tr|td|th|font|section|article|hr|img|pre|code|blockquote)"
                          r"\b[^<>]*>[ \t]*", re.IGNORECASE)
RESIDUAL_BLOCK_TAG = re.compile(r"^</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|hr|"
                                r"blockquote)\b", re.IGNORECASE)
RESIDUAL_ENTITY = re.compile(r"&(?:[a-zA-Z]{2,8}|#\d{1,6}|#x[0-9a-fA-F]{1,6});")


def _replace_tag(match: re.Match[str]) -> str:
    """Block tags become a line break; inline tags keep at most one space."""
    tag = match.group(0).strip()
    if RESIDUAL_BLOCK_TAG.match(tag):
        return "\n"
    return " " if match.group(0) != tag else ""


def sanitize_description(text: str) -> str:
    """Final hygiene pass on every description before it is written.

    Source fields can still carry markup after conversion: entities escaped
    twice, tags inside "plain" text, zero-width characters and soft hyphens
    that split keywords (Py<ZWSP>thon). Clean text passes through unchanged.
    """
    if not text:
        return ""
    for _ in range(2):  # a second round handles '&amp;lt;p&amp;gt;'
        text = RESIDUAL_ENTITY.sub(lambda m: html.unescape(m.group(0)), text)
        text = RESIDUAL_TAG.sub(_replace_tag, text)
    text = INVISIBLE_CHARS.sub("", text).replace("\u00a0", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def description_from(value: str | None) -> str:
    if not value:
        return ""
    if re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", value) or "&lt;" in value:
        return html_to_markdown(html.unescape(value) if "&lt;" in value else value)
    return plain_text(html.unescape(value))


# ---------------------------------------------------------------------------
# Canonical URLs and job identity
# ---------------------------------------------------------------------------
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def canonical_url(url: str) -> str:
    """Normalize for comparison only; the original URL is what gets written."""
    parts = urllib.parse.urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((scheme, netloc, path, urllib.parse.urlencode(sorted(query)),
                                    ""))


def strong_identity(url: str) -> str | None:
    """ATS job identity recognizable in a URL, shared across sources."""
    if not is_http_url(url):
        return None
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    query = dict(urllib.parse.parse_qsl(parts.query))
    if query.get("gh_jid", "").isdigit():
        return f"greenhouse:{query['gh_jid']}"
    if host.endswith("greenhouse.io"):
        match = re.search(r"/jobs/(\d+)", parts.path)
        if match:
            return f"greenhouse:{match.group(1)}"
    if host.endswith("ashbyhq.com"):
        match = re.search(rf"/({_UUID})(?:/|$)", parts.path)
        if match:
            return f"ashby:{match.group(1).lower()}"
    if host.endswith("lever.co"):
        match = re.search(rf"/({_UUID})(?:/|$)", parts.path)
        if match:
            return f"lever:{match.group(1).lower()}"
    return None


def _norm_words(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def normalized_company(name: str) -> str:
    words = _norm_words(name).split()
    while words and words[-1] in {"inc", "ltd", "llc", "corp", "corporation", "co", "plc",
                                  "gmbh", "limited", "company"}:
        words.pop()
    return " ".join(words)


def normalized_location(location: str | None) -> str:
    first = (location or "").split(",")[0].split("|")[0].split(";")[0]
    return _norm_words(first)


def dedup_key(company: str | None, title: str | None, location: str | None) -> str | None:
    """SHA-256 of normalized company, title and location; None when any part is empty.

    Stored in the manifest so a posting already written from one source is not
    ingested again from another source in a later run.
    """
    parts = (normalized_company(company or ""), _norm_words(title), normalized_location(location))
    if not all(parts):
        return None
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def weak_identity(record: JobRecord) -> tuple[str, str, str]:
    return (normalized_company(record.company), _norm_words(record.title),
            normalized_location(record.location))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
_TERM_VARIANTS = {"backend": ("backend", "back end", "back-end"),
                  "devops": ("devops", "dev ops", "dev-ops"),
                  "frontend": ("frontend", "front end", "front-end"),
                  "fullstack": ("fullstack", "full stack", "full-stack")}


def term_pattern(term: str) -> re.Pattern[str]:
    """Phrase-aware, case-insensitive match: 'SRE' never matches 'pressure'."""
    key = _norm_words(term).replace(" ", "")
    variants = _TERM_VARIANTS.get(key, (term,))
    body = "|".join(re.escape(v.strip()).replace(r"\ ", r"[\s-]+") for v in variants)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{body})s?(?![A-Za-z0-9])", re.IGNORECASE)


def matches_any(text: str | None, terms: Sequence[str]) -> bool:
    return any(term_pattern(t).search(text or "") for t in terms)


# Region terms understood by filters.locations. Other terms match as phrases.
US_TERMS = frozenset({"us", "usa", "united states", "united states of america", "america"})
CANADA_TERMS = frozenset({"canada", "ca"})
_US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
              "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
_CA_PROVINCES = "ON BC QC AB MB NS NB NL PE SK YT NT NU".split()
US_LOCATION = (
    re.compile(r"united states|(?<![a-z])u\.?s\.?a\.?(?![a-z])|(?<![a-z])u\.s\.(?![a-z])|"
               r"new york|nyc|san francisco|bay area|silicon valley|seattle|boston|austin|"
               r"chicago|los angeles|denver|atlanta|washington,? d\.?c|miami|san jose|"
               r"palo alto|mountain view|menlo park|sunnyvale|san diego|santa clara|redwood city|"
               r"portland|philadelphia|pittsburgh|dallas|houston|salt lake|raleigh|detroit|"
               r"minneapolis|phoenix|nashville|california|texas|new jersey|massachusetts|"
               r"illinois|colorado|florida|virginia|pennsylvania|oregon|utah|arizona|"
               r"north carolina|michigan|minnesota", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])(US|SF|NY|SEA|DC)(?![A-Za-z])"),
    re.compile(r",\s*(" + "|".join(_US_STATES) + r")(?![A-Za-z])"),
)
CANADA_LOCATION = (
    re.compile(r"canada|toronto|ontario|montreal|vancouver|ottawa|calgary|edmonton|waterloo|"
               r"kitchener|mississauga|markham|quebec|british columbia|alberta|manitoba|"
               r"winnipeg|nova scotia|halifax|saskatchewan|new brunswick", re.IGNORECASE),
    re.compile(r",\s*(" + "|".join(_CA_PROVINCES) + r")(?![A-Za-z])"),
)
_REMOTE_ONLY = re.compile(r"^\W*(?:(?:fully|100%)\s+)?(remote|anywhere|worldwide|global|"
                          r"remote[\s-]first|flexible)\W*$", re.IGNORECASE)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def location_matches(location: str, terms: Sequence[str],
                     include_unspecified_remote: bool = True) -> bool:
    """True when a posting location falls inside the configured regions.

    'United States'/'US'/'USA' and 'Canada' are regions: they also match city,
    state and province forms such as 'San Francisco, CA' or 'Vancouver, BC'.
    Other terms match as phrases. A bare 'Remote' with no region passes only
    when include_unspecified_remote is true.
    """
    folded = _fold(location)
    for term in terms:
        key = _norm_words(term)
        if key in US_TERMS and any(p.search(folded) for p in US_LOCATION):
            return True
        if key in CANADA_TERMS and any(p.search(folded) for p in CANADA_LOCATION):
            return True
        if key not in US_TERMS | CANADA_TERMS and term_pattern(term).search(folded):
            return True
    return include_unspecified_remote and bool(_REMOTE_ONLY.match(folded))


@dataclass
class Filters:
    title_keywords: list[str]
    exclude_title_keywords: list[str]
    locations: list[str]
    max_age_days: int | None
    include_unspecified_remote: bool = True

    def check(self, record: JobRecord, company: dict[str, Any] | None = None) -> str | None:
        """None when the record passes, otherwise the reason it was filtered out."""
        include = (company or {}).get("include_keywords") or self.title_keywords
        exclude = [*self.exclude_title_keywords, *((company or {}).get("exclude_keywords") or [])]
        if include and not matches_any(record.title, include):
            return "title does not match the role keywords"
        if exclude and matches_any(record.title, exclude):
            return "title matches an exclude keyword"
        if self.locations and record.location and not location_matches(
                record.location, self.locations, self.include_unspecified_remote):
            return "location outside the configured locations"
        return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COMPANY_KEYS = {"name", "enabled", "greenhouse_board_token", "ashby_board_name",
                "include_keywords", "exclude_keywords", "notes"}
SOURCE_KEYS_ALLOWED = {
    "greenhouse": {"enabled", "timeout_seconds", "max_requests_per_run"},
    "ashby": {"enabled", "timeout_seconds", "max_requests_per_run"},
    "simplify": {"enabled", "timeout_seconds", "max_requests_per_run", "repositories",
                 "max_age_days", "write_metadata_only", "max_response_mb"},
    "jooble": {"enabled", "timeout_seconds", "keywords", "locations", "max_requests_per_run",
               "results_per_page", "pages_per_search", "api_host"},
    "jobright": {"enabled", "timeout_seconds", "default_keyword", "default_location",
                 "default_country", "default_limit", "max_total_charge_usd"},
}
REPO_KEYS = {"name", "url", "branch", "listings_path", "enabled", "category",
             "include_past_terms", "notes"}
TOP_KEYS = {"companies", "sources", "filters", "http", "_comment", "$schema_note"}
FILTER_KEYS = {"title_keywords", "exclude_title_keywords", "locations", "max_age_days",
               "include_unspecified_remote"}
HTTP_KEYS = {"timeout_seconds", "max_retries", "backoff_seconds", "cache_ttl_seconds"}
BOARD_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def default_config() -> dict[str, Any]:
    return {
        "companies": [],
        "filters": {"title_keywords": list(DEFAULT_TITLE_KEYWORDS),
                    "exclude_title_keywords": list(DEFAULT_EXCLUDE_TITLES),
                    "locations": list(DEFAULT_FILTER_LOCATIONS), "max_age_days": 120,
                    "include_unspecified_remote": True},
        "http": {"timeout_seconds": 20, "max_retries": 2, "backoff_seconds": 1.0,
                 "cache_ttl_seconds": 3600},
        "sources": {
            "greenhouse": {"enabled": True, "timeout_seconds": 20, "max_requests_per_run": 50},
            "ashby": {"enabled": True, "timeout_seconds": 20, "max_requests_per_run": 50},
            "simplify": {"enabled": True, "timeout_seconds": 60, "max_requests_per_run": 4,
                         "max_age_days": 120, "write_metadata_only": True,
                         "max_response_mb": 40, "repositories": []},
            "jooble": {"enabled": True, "timeout_seconds": 20, "api_host": "jooble.org",
                       "keywords": list(DEFAULT_JOOBLE_KEYWORDS),
                       "locations": list(DEFAULT_LOCATIONS), "max_requests_per_run": 10,
                       "results_per_page": 20, "pages_per_search": 1},
            "jobright": {"enabled": True, "timeout_seconds": 290,
                         "default_keyword": "Software Engineer",
                         "default_location": "Toronto, Canada", "default_country": "CA",
                         "default_limit": 25, "max_total_charge_usd": 0.5},
        },
    }


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Read and validate the config; a missing file falls back to defaults."""
    notes: list[str] = []
    if not path.exists():
        notes.append(f"config file {path} not found; using built-in defaults with no "
                     f"companies configured")
        return default_config(), notes
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    problems = validate_config(raw)
    if problems:
        raise ConfigError(f"{path} has {len(problems)} problem(s):\n  - " + "\n  - ".join(problems))
    return _merge(default_config(), raw), notes


def _check_str_list(value: Any, where: str, problems: list[str]) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        problems.append(f"{where} must be a list of non-empty strings")


def _check_positive(value: Any, where: str, problems: list[str], allow_zero: bool = False,
                    allow_float: bool = False) -> None:
    types = (int, float) if allow_float else (int,)
    too_small = value < 0 if allow_zero else value <= 0
    if isinstance(value, bool) or not isinstance(value, types) or too_small:
        problems.append(f"{where} must be a {'non-negative' if allow_zero else 'positive'} "
                        f"{'number' if allow_float else 'integer'}")


def validate_config(raw: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in sorted(set(raw) - TOP_KEYS):
        problems.append(f"unknown top-level key '{key}'")
    companies = raw.get("companies", [])
    if not isinstance(companies, list):
        problems.append("'companies' must be a list")
        companies = []
    seen: set[str] = set()
    for index, company in enumerate(companies):
        where = f"companies[{index}]"
        if not isinstance(company, dict):
            problems.append(f"{where} must be an object")
            continue
        for key in sorted(set(company) - COMPANY_KEYS):
            problems.append(f"{where}: unknown key '{key}'")
        name = company.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}: 'name' must be a non-empty string")
        elif name.strip().lower() in seen:
            problems.append(f"{where}: duplicate company name '{name}'")
        else:
            seen.add(name.strip().lower())
        if "enabled" in company and not isinstance(company["enabled"], bool):
            problems.append(f"{where}: 'enabled' must be true or false")
        for key in ("greenhouse_board_token", "ashby_board_name"):
            value = company.get(key)
            if value is not None and (not isinstance(value, str) or not BOARD_TOKEN.match(value)):
                problems.append(f"{where}: '{key}' must be null or a board identifier "
                                f"(letters, digits, '.', '_', '-')")
        for key in ("include_keywords", "exclude_keywords"):
            if key in company:
                _check_str_list(company[key], f"{where}.{key}", problems)
    filters = raw.get("filters", {})
    if not isinstance(filters, dict):
        problems.append("'filters' must be an object")
        filters = {}
    for key in sorted(set(filters) - FILTER_KEYS):
        problems.append(f"filters: unknown key '{key}'")
    for key in ("title_keywords", "exclude_title_keywords", "locations"):
        if key in filters and not (filters[key] == [] or isinstance(filters[key], list)):
            problems.append(f"filters.{key} must be a list")
        elif key in filters and filters[key]:
            _check_str_list(filters[key], f"filters.{key}", problems)
    if filters.get("max_age_days") is not None:
        _check_positive(filters["max_age_days"], "filters.max_age_days", problems)
    if "include_unspecified_remote" in filters and not isinstance(
            filters["include_unspecified_remote"], bool):
        problems.append("filters.include_unspecified_remote must be true or false")
    http_cfg = raw.get("http", {})
    if not isinstance(http_cfg, dict):
        problems.append("'http' must be an object")
        http_cfg = {}
    for key in sorted(set(http_cfg) - HTTP_KEYS):
        problems.append(f"http: unknown key '{key}'")
    for key in ("timeout_seconds", "backoff_seconds"):
        if key in http_cfg:
            _check_positive(http_cfg[key], f"http.{key}", problems, allow_float=True)
    for key in ("max_retries", "cache_ttl_seconds"):
        if key in http_cfg:
            _check_positive(http_cfg[key], f"http.{key}", problems, allow_zero=True)
    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        problems.append("'sources' must be an object")
        sources = {}
    for name in sorted(set(sources) - set(SOURCE_KEYS_ALLOWED)):
        problems.append(f"sources: unknown source '{name}'")
    for name, allowed in SOURCE_KEYS_ALLOWED.items():
        cfg = sources.get(name, {})
        if not isinstance(cfg, dict):
            problems.append(f"sources.{name} must be an object")
            continue
        for key in sorted(set(cfg) - allowed):
            problems.append(f"sources.{name}: unknown key '{key}'")
        if "enabled" in cfg and not isinstance(cfg["enabled"], bool):
            problems.append(f"sources.{name}.enabled must be true or false")
        for key in ("timeout_seconds", "max_total_charge_usd"):
            if key in cfg:
                _check_positive(cfg[key], f"sources.{name}.{key}", problems, allow_float=True)
        for key in ("max_requests_per_run", "results_per_page", "pages_per_search",
                    "default_limit", "max_age_days", "max_response_mb"):
            if key in cfg:
                _check_positive(cfg[key], f"sources.{name}.{key}", problems)
        for key in ("keywords", "locations"):
            if key in cfg:
                _check_str_list(cfg[key], f"sources.{name}.{key}", problems)
        if "api_host" in cfg and not (isinstance(cfg["api_host"], str) and re.fullmatch(
                r"([a-z]{2}\.)?jooble\.org", cfg["api_host"])):
            problems.append(f"sources.{name}.api_host must be jooble.org or <country>.jooble.org")
    simplify = sources.get("simplify", {}) if isinstance(sources.get("simplify"), dict) else {}
    repos = simplify.get("repositories", [])
    if not isinstance(repos, list):
        problems.append("sources.simplify.repositories must be a list")
        repos = []
    for index, repo in enumerate(repos):
        where = f"sources.simplify.repositories[{index}]"
        if not isinstance(repo, dict):
            problems.append(f"{where} must be an object")
            continue
        for key in sorted(set(repo) - REPO_KEYS):
            problems.append(f"{where}: unknown key '{key}'")
        if not _github_repo(repo.get("url")):
            problems.append(f"{where}: 'url' must be https://github.com/<owner>/<repo>")
        if repo.get("category") not in ("new_grad", "internship"):
            problems.append(f"{where}: 'category' must be 'new_grad' or 'internship'")
        for key in ("branch", "listings_path"):
            if key in repo and not (isinstance(repo[key], str) and re.fullmatch(
                    r"[A-Za-z0-9._/-]{1,200}", repo[key]) and ".." not in repo[key]):
                problems.append(f"{where}: '{key}' has invalid characters")
        for key in ("enabled", "include_past_terms"):
            if key in repo and not isinstance(repo[key], bool):
                problems.append(f"{where}: '{key}' must be true or false")
    return problems


def _github_repo(url: Any) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?", url)
    return (match.group(1), match.group(2)) if match else None


# ---------------------------------------------------------------------------
# Adapter plumbing
# ---------------------------------------------------------------------------
@dataclass
class RunContext:
    config: dict[str, Any]
    client: HttpClient
    filters: Filters
    redact: Redactor
    today: dt.date
    env: dict[str, str]
    allow_apify_spend: bool = False
    keyword: str | None = None
    location: str | None = None
    country: str | None = None
    limit: int | None = None
    explicit_source: bool = False


@dataclass
class SourceResult:
    source: str
    status: str = "ok"               # ok | partial | failed | skipped
    reason: str = ""
    records: list[JobRecord] = field(default_factory=list)
    boards: int = 0
    raw: int = 0
    rejected: int = 0
    filtered: int = 0
    stale: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.errors.append(message)


def _admit(result: SourceResult, record: JobRecord, ctx: RunContext,
           company: dict[str, Any] | None = None) -> None:
    record.description = sanitize_description(record.description)
    problems = validate_record(record)
    if problems:
        result.rejected += 1
        result.warnings.append(f"rejected {record.source} record "
                               f"{(record.title or '?')[:50]!r}: {', '.join(problems)}")
        return
    if ctx.filters.check(record, company):
        result.filtered += 1
        return
    result.records.append(record)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return collapse(value)
    return None


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _date_part(value: Any) -> str | None:
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return None


def _enabled_companies(config: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [c for c in config.get("companies", [])
            if c.get("enabled", True) and c.get(key)]


def _finish(result: SourceResult, attempted: int, failed: int) -> SourceResult:
    if attempted and failed == attempted:
        result.status = "failed"
    elif failed:
        result.status = "partial"
    return result


# ---------------------------------------------------------------------------
# Greenhouse (public Job Board API)
# ---------------------------------------------------------------------------
def fetch_greenhouse(ctx: RunContext) -> SourceResult:
    result = SourceResult("greenhouse")
    cfg = ctx.config["sources"]["greenhouse"]
    companies = _enabled_companies(ctx.config, "greenhouse_board_token")
    result.boards = len(companies)
    if not companies:
        result.status, result.reason = "skipped", "no enabled companies with a greenhouse_board_token"
        return result
    failed = 0
    for company in companies:
        token = company["greenhouse_board_token"]
        url = GREENHOUSE_URL.format(token=urllib.parse.quote(token, safe=""))
        try:
            data = ctx.client.get_json(url, source="greenhouse", timeout=cfg["timeout_seconds"],
                                       max_bytes=30_000_000, cacheable=True)
        except HttpError as exc:
            failed += 1
            hint = " (unknown board token?)" if exc.status == 404 else ""
            result.fail(f"{company['name']}: {exc}{hint}")
            continue
        except FetchError as exc:
            failed += 1
            result.fail(f"{company['name']}: {exc}")
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            failed += 1
            result.fail(f"{company['name']}: unexpected Greenhouse response shape (no 'jobs' list)")
            continue
        for job in jobs:
            result.raw += 1
            if not isinstance(job, dict):
                result.rejected += 1
                continue
            content = job.get("content") or ""
            description = html_to_markdown(html.unescape(content)) if content else ""
            departments = [d.get("name") for d in job.get("departments") or []
                           if isinstance(d, dict) and d.get("name")]
            location = (job.get("location") or {}).get("name") if isinstance(
                job.get("location"), dict) else None
            record = JobRecord(
                source="Greenhouse",
                source_job_id=str(job["id"]) if job.get("id") is not None else None,
                company=_text(job.get("company_name")) or company["name"],
                title=_text(job.get("title")) or "",
                location=_text(location),
                url=(job.get("absolute_url") or "").strip(),
                description=description,
                description_complete=bool(description),
                completeness_note=None if description else
                "Greenhouse returned no description content for this posting.",
                posted_date=_date_part(job.get("first_published")),
                updated_date=_date_part(job.get("updated_at")),
                department=", ".join(departments) or None,
            )
            _admit(result, record, ctx, company)
    return _finish(result, len(companies), failed)


# ---------------------------------------------------------------------------
# Ashby (public Job Postings API)
# ---------------------------------------------------------------------------
ASHBY_WORKPLACE = {"OnSite": "On-site", "Hybrid": "Hybrid", "Remote": "Remote"}
ASHBY_EMPLOYMENT = {"FullTime": "Full-time", "PartTime": "Part-time", "Intern": "Internship",
                    "Contract": "Contract", "Temporary": "Temporary"}


def fetch_ashby(ctx: RunContext) -> SourceResult:
    result = SourceResult("ashby")
    cfg = ctx.config["sources"]["ashby"]
    companies = _enabled_companies(ctx.config, "ashby_board_name")
    result.boards = len(companies)
    if not companies:
        result.status, result.reason = "skipped", "no enabled companies with an ashby_board_name"
        return result
    failed = 0
    for company in companies:
        board = company["ashby_board_name"]
        url = ASHBY_URL.format(board=urllib.parse.quote(board, safe=""))
        try:
            data = ctx.client.get_json(url, source="ashby", timeout=cfg["timeout_seconds"],
                                       max_bytes=30_000_000, cacheable=True)
        except FetchError as exc:
            failed += 1
            hint = " (unknown board name?)" if isinstance(exc, HttpError) and exc.status == 404 \
                else ""
            result.fail(f"{company['name']}: {exc}{hint}")
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            failed += 1
            result.fail(f"{company['name']}: unexpected Ashby response shape (no 'jobs' list)")
            continue
        for job in jobs:
            result.raw += 1
            if not isinstance(job, dict):
                result.rejected += 1
                continue
            if job.get("isListed") is False:
                result.stale += 1  # unlisted postings are not public vacancies
                continue
            html_desc = job.get("descriptionHtml") or ""
            description = html_to_markdown(html_desc) if html_desc.strip() else \
                plain_text(job.get("descriptionPlain") or "")
            workplace = ASHBY_WORKPLACE.get(job.get("workplaceType") or "")
            if workplace is None and job.get("isRemote") is True:
                workplace = "Remote eligible (remote vs hybrid not specified)"
            record = JobRecord(
                source="Ashby",
                source_job_id=_text(job.get("id")),
                company=company["name"],
                title=_text(job.get("title")) or "",
                location=_ashby_location(job),
                url=(job.get("jobUrl") or "").strip(),
                description=description,
                description_complete=bool(description),
                completeness_note=None if description else
                "Ashby returned no description for this posting.",
                posted_date=_date_part(job.get("publishedAt")),
                employment_type=ASHBY_EMPLOYMENT.get(job.get("employmentType") or "",
                                                     _text(job.get("employmentType"))),
                workplace_type=workplace,
                department=" / ".join(x for x in (_text(job.get("department")),
                                                   _text(job.get("team"))) if x) or None,
                application_url=job.get("applyUrl") if is_http_url(job.get("applyUrl")) else None,
            )
            _admit(result, record, ctx, company)
    return _finish(result, len(companies), failed)


def _ashby_location(job: dict[str, Any]) -> str | None:
    primary = _text(job.get("location"))
    others = [_text(x.get("location")) for x in job.get("secondaryLocations") or []
              if isinstance(x, dict)]
    others = [x for x in others if x and x != primary]
    if not others:
        return primary
    shown = ", ".join(others[:6]) + (f" (+{len(others) - 6} more)" if len(others) > 6 else "")
    return f"{primary or 'Not specified'}; also {shown}"


# ---------------------------------------------------------------------------
# SimplifyJobs listing repositories
# ---------------------------------------------------------------------------
TERM_START_MONTH = {"winter": 1, "spring": 5, "summer": 5, "fall": 9, "autumn": 9}


def term_has_started(term: str, today: dt.date) -> bool | None:
    """True when an internship term such as 'Summer 2026' has already begun."""
    match = re.fullmatch(r"\s*(winter|spring|summer|fall|autumn)\s+(\d{4})\s*", term, re.I)
    if not match:
        return None
    start = dt.date(int(match.group(2)), TERM_START_MONTH[match.group(1).lower()], 1)
    return start <= today


def fetch_simplify(ctx: RunContext) -> SourceResult:
    result = SourceResult("simplify")
    cfg = ctx.config["sources"]["simplify"]
    repos = [r for r in cfg.get("repositories", []) if r.get("enabled", True)]
    result.boards = len(repos)
    if not repos:
        result.status, result.reason = "skipped", "no enabled SimplifyJobs repositories"
        return result
    max_age = cfg.get("max_age_days") or ctx.filters.max_age_days
    failed = 0
    for repo in repos:
        owner, name = _github_repo(repo["url"])  # validated in load_config
        raw_url = SIMPLIFY_RAW_URL.format(owner=owner, repo=name,
                                          branch=repo.get("branch", "dev"),
                                          path=repo.get("listings_path",
                                                        ".github/scripts/listings.json"))
        try:
            data = ctx.client.get_json(raw_url, source="simplify", timeout=cfg["timeout_seconds"],
                                       max_bytes=cfg["max_response_mb"] * 1_000_000,
                                       cacheable=True)
        except FetchError as exc:
            failed += 1
            result.fail(f"{repo.get('name', name)}: {exc}")
            continue
        if not isinstance(data, list):
            failed += 1
            result.fail(f"{repo.get('name', name)}: listings file is not a JSON list")
            continue
        internship = repo.get("category") == "internship"
        for item in data:
            result.raw += 1
            if not isinstance(item, dict):
                result.rejected += 1
                continue
            if item.get("active") is not True or item.get("is_visible") is False:
                result.stale += 1
                continue
            posted = _epoch_date(item.get("date_posted"))
            if max_age and posted and (ctx.today - dt.date.fromisoformat(posted)).days > max_age:
                result.stale += 1
                continue
            terms = [t for t in item.get("terms") or [] if isinstance(t, str)]
            if internship and not repo.get("include_past_terms", False) and terms:
                started = [term_has_started(t, ctx.today) for t in terms]
                if all(s is True for s in started):
                    result.stale += 1
                    continue
            locations = [l for l in item.get("locations") or [] if isinstance(l, str)]
            record = JobRecord(
                source="Simplify",
                source_job_id=_text(item.get("id")),
                company=_text(item.get("company_name")) or "",
                title=_text(item.get("title")) or "",
                location="; ".join(locations) or None,
                url=(item.get("url") or "").strip(),
                description="",
                description_complete=False,
                completeness_note=(f"Listing metadata only: the SimplifyJobs "
                                   f"{repo.get('name', name)} repository does not include the "
                                   f"job description. Read the full posting at the URL above."),
                posted_date=posted,
                updated_date=_epoch_date(item.get("date_updated")),
                terms=terms,
            )
            _admit(result, record, ctx)
    if not cfg.get("write_metadata_only", True):
        result.warnings.append("write_metadata_only is false: Simplify listings are used for "
                               "deduplication only and are not written")
    return _finish(result, len(repos), failed)


def _epoch_date(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Jooble REST API (POST, JSON body, key in the path)
# ---------------------------------------------------------------------------
def jooble_searches(ctx: RunContext) -> list[tuple[str, str, int]]:
    """Deterministic (keyword, location, page) plan, keyword-major."""
    cfg = ctx.config["sources"]["jooble"]
    keywords = [ctx.keyword] if ctx.keyword else cfg["keywords"]
    locations = [ctx.location] if ctx.location else cfg["locations"]
    pages = cfg["pages_per_search"]
    return [(k, l, p) for k in keywords for l in locations for p in range(1, pages + 1)]


def fetch_jooble(ctx: RunContext) -> SourceResult:
    result = SourceResult("jooble")
    cfg = ctx.config["sources"]["jooble"]
    key = ctx.env.get("JOOBLE_API_KEY", "").strip()
    if not key:
        result.status, result.reason = "skipped", "JOOBLE_API_KEY is not set"
        return result
    ctx.redact.add(key)
    plan = jooble_searches(ctx)
    budget = cfg["max_requests_per_run"]
    if len(plan) > budget:
        result.warnings.append(f"request budget {budget} covers {budget} of {len(plan)} planned "
                               f"searches; the rest were not run (raise "
                               f"sources.jooble.max_requests_per_run to search more)")
    url = JOOBLE_URL.format(host=cfg["api_host"], key=urllib.parse.quote(key, safe=""))
    per_page = cfg["results_per_page"]
    failed = attempted = 0
    exhausted: set[tuple[str, str]] = set()
    for keyword, location, page in plan[:budget]:
        if (keyword, location) in exhausted:
            continue
        attempted += 1
        body = {"keywords": keyword, "location": location, "page": page,
                "ResultOnPage": per_page}
        try:
            data = ctx.client.post_json(url, body, source="jooble",
                                        timeout=cfg["timeout_seconds"])
        except HttpError as exc:
            failed += 1
            hint = (" (invalid key, or a key from a different Jooble country domain)"
                    if exc.status == 403 else "")
            result.fail(f"search {keyword!r} in {location!r}: {ctx.redact(str(exc))}{hint}")
            if exc.status == 403:
                break  # the key will not work for any other search either
            continue
        except FetchError as exc:
            failed += 1
            result.fail(f"search {keyword!r} in {location!r}: {ctx.redact(str(exc))}")
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            failed += 1
            result.fail(f"search {keyword!r} in {location!r}: unexpected response shape")
            continue
        if len(jobs) < per_page:
            exhausted.add((keyword, location))
        for job in jobs:
            result.raw += 1
            if not isinstance(job, dict):
                result.rejected += 1
                continue
            snippet = description_from(job.get("snippet"))
            record = JobRecord(
                source="Jooble",
                source_job_id=str(job["id"]) if job.get("id") is not None else None,
                company=_text(job.get("company")) or "",
                title=_text(job.get("title")) or "",
                location=_text(job.get("location")),
                url=(job.get("link") or "").strip(),
                description=snippet,
                description_complete=False,
                completeness_note=("Partial description: Jooble returns only a search snippet, "
                                   "not the full posting. Read the full posting at the URL "
                                   "above."),
                updated_date=_date_part(job.get("updated")),
                employment_type=_text(job.get("type")),
                salary=_text(job.get("salary")),
            )
            _admit(result, record, ctx)
    return _finish(result, attempted, failed)


# ---------------------------------------------------------------------------
# Jobright through the Apify actor jobscrawler/jobright-scraper
# ---------------------------------------------------------------------------
JOBRIGHT_FULL_DESCRIPTION_CHARS = 600


def apify_input(ctx: RunContext) -> tuple[dict[str, Any], int]:
    """Translate CLI options into the actor's documented input schema."""
    cfg = ctx.config["sources"]["jobright"]
    limit = ctx.limit or cfg["default_limit"]
    if not 1 <= limit <= 1000:
        raise ConfigError("--limit must be between 1 and 1000 for the Jobright actor")
    return ({
        "keyword": ctx.keyword or cfg["default_keyword"],
        "location": ctx.location or cfg["default_location"],
        "country": ctx.country or cfg["default_country"],
        "maxItems": limit,
        "includeCompanyDetails": False,
        "includeSalary": True,
    }, limit)


def fetch_jobright(ctx: RunContext) -> SourceResult:
    result = SourceResult("jobright")
    cfg = ctx.config["sources"]["jobright"]
    token = ctx.env.get("APIFY_TOKEN", "").strip()
    if not ctx.allow_apify_spend:
        result.status = "skipped"
        result.reason = ("Apify runs are billable; pass --allow-apify-spend to run the Jobright "
                         "actor")
        return result
    if not token:
        result.status, result.reason = "skipped", "APIFY_TOKEN is not set"
        return result
    ctx.redact.add(token)
    actor_input, limit = apify_input(ctx)
    timeout = min(int(cfg["timeout_seconds"]), APIFY_SYNC_LIMIT_SECONDS - 5)
    query = urllib.parse.urlencode({
        "timeout": timeout, "maxItems": limit, "limit": limit, "format": "json",
        "clean": "true", "maxTotalChargeUsd": cfg["max_total_charge_usd"]})
    result.boards = 1
    try:
        data = ctx.client.post_json(f"{APIFY_URL}?{query}", actor_input, source="jobright",
                                    timeout=APIFY_SYNC_LIMIT_SECONDS + 10,
                                    headers={"Authorization": f"Bearer {token}"})
    except HttpError as exc:
        hints = {400: " (input rejected by the actor)", 401: " (invalid APIFY_TOKEN)",
                 402: " (Apify account needs credit or the token is missing)",
                 403: " (token lacks permission to run this actor)",
                 408: " (actor run exceeded Apify's synchronous wait; lower --limit)"}
        result.fail(f"actor {APIFY_ACTOR}: {ctx.redact(str(exc))}{hints.get(exc.status, '')}")
        result.status = "failed"
        return result
    except FetchError as exc:
        result.fail(f"actor {APIFY_ACTOR}: {ctx.redact(str(exc))}")
        result.status = "failed"
        return result
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        result.fail(f"actor {APIFY_ACTOR} returned an error: "
                    f"{ctx.redact(str(data['error'].get('type', 'unknown')))}")
        result.status = "failed"
        return result
    if not isinstance(data, list):
        result.fail(f"actor {APIFY_ACTOR}: dataset is not a JSON list")
        result.status = "failed"
        return result
    if len(data) > limit:
        result.warnings.append(f"actor returned {len(data)} items; kept the first {limit}")
        data = data[:limit]
    for item in data:
        result.raw += 1
        if not isinstance(item, dict):
            result.rejected += 1
            continue
        _admit(result, normalize_jobright(item), ctx)
    return result


def normalize_jobright(item: dict[str, Any]) -> JobRecord:
    """Map one actor dataset item; company fields never become job fields."""
    company = item.get("companyName") or item.get("company")
    if isinstance(company, dict):
        company = company.get("name") or company.get("companyName")
    location = item.get("location")
    if isinstance(location, list):
        location = "; ".join(str(x) for x in location if x)
    elif isinstance(location, dict):
        location = location.get("name") or location.get("city")
    url = next((item.get(k) for k in ("jobUrl", "url", "applyUrl", "link")
                if is_http_url(item.get(k))), "")
    description = description_from(item.get("description") if isinstance(
        item.get("description"), str) else "")
    requirements = [collapse(str(r)) for r in item.get("requirements") or []
                    if isinstance(r, (str, int, float)) and str(r).strip()]
    if requirements and not all(r[:40] in description for r in requirements):
        description = (description + "\n\n" if description else "") + "#### Requirements\n\n" + \
            "\n".join(f"- {r}" for r in requirements)
    complete = len(description) >= JOBRIGHT_FULL_DESCRIPTION_CHARS
    salary = _text(item.get("salary")) if isinstance(item.get("salary"), str) else None
    return JobRecord(
        source="Jobright",
        source_job_id=_text(str(item.get("id") or item.get("jobId") or "")) or None,
        company=_text(company) or "",
        title=_text(item.get("jobTitle") or item.get("title")) or "",
        location=_text(location),
        url=url.strip(),
        description=description,
        description_complete=complete,
        completeness_note=None if complete else (
            "Partial description: the Jobright dataset item has under "
            f"{JOBRIGHT_FULL_DESCRIPTION_CHARS} characters of description, so it may be a "
            "summary. Read the full posting at the URL above."),
        posted_date=_date_part(item.get("postedDate")),
        employment_type=_text(item.get("jobType")),
        salary=salary,
    )


# ---------------------------------------------------------------------------
# Cross-source deduplication
# ---------------------------------------------------------------------------
@dataclass
class DedupeResult:
    kept: list[JobRecord]
    duplicates: list[dict[str, str]]
    possible: list[dict[str, Any]]


def _priority(record: JobRecord) -> tuple:
    """Full descriptions first, then official ATS sources, then longer text."""
    return (0 if record.description_complete else 1, SOURCE_RANK.get(record.source, 9),
            -len(record.description), record.source, record.source_job_id or "", record.url)


def _merge_group(group: list[JobRecord], reason: str,
                 duplicates: list[dict[str, str]]) -> JobRecord:
    best = min(group, key=_priority)
    for other in group:
        if other is best:
            continue
        for url in (other.url, *other.also_seen_at):
            if url != best.url and url not in best.also_seen_at:
                best.also_seen_at.append(url)
        duplicates.append({"kept": f"{best.source}: {best.url}",
                           "dropped": f"{other.source}: {other.url}", "reason": reason})
    return best


def deduplicate(records: Sequence[JobRecord]) -> DedupeResult:
    duplicates: list[dict[str, str]] = []
    strong: dict[str, list[JobRecord]] = {}
    for record in records:
        strong.setdefault(record.identity(), []).append(record)
    merged = [_merge_group(group, "same posting URL or ATS job id", duplicates)
              if len(group) > 1 else group[0] for group in strong.values()]
    weak: dict[tuple[str, str, str], list[JobRecord]] = {}
    for record in merged:
        weak.setdefault(weak_identity(record), []).append(record)
    kept: list[JobRecord] = []
    for key, group in weak.items():
        distinct_sources = len({r.source for r in group}) == len(group)
        ats_ids = sum(1 for r in group if strong_identity(r.url))
        if len(group) > 1 and all(key) and distinct_sources and ats_ids <= 1:
            kept.append(_merge_group(group, "same company, title and location from different "
                                            "sources", duplicates))
        else:
            kept.extend(group)
    by_title: dict[tuple[str, str], list[JobRecord]] = {}
    for record in kept:
        by_title.setdefault(weak_identity(record)[:2], []).append(record)
    possible = [{"company": group[0].company, "title": group[0].title,
                 "records": [f"{r.source}: {r.location or 'no location'}: {r.url}"
                             for r in group]}
                for group in by_title.values() if len(group) > 1]
    kept.sort(key=lambda r: (SOURCE_RANK.get(r.source, 9), normalized_company(r.company),
                             _norm_words(r.title), canonical_url(r.url)))
    return DedupeResult(kept, duplicates, possible)


# ---------------------------------------------------------------------------
# Markdown output and safe writing
# ---------------------------------------------------------------------------
def _slug(text: str, separator: str, limit: int) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", text)
    slug = separator.join(words)[:limit].strip(separator)
    return slug


def base_filename(record: JobRecord) -> str:
    source = SOURCE_KEYS.get(record.source, "unknown")
    company = _slug(record.company, "", MAX_COMPANY_SLUG) or "unknowncompany"
    role = _slug(record.title, "_", MAX_ROLE_SLUG) or "role"
    return f"{source}_{company}_{role}"


def _md_value(text: str | None) -> str:
    """Single-line metadata value; strips Markdown control characters at the start."""
    value = collapse(text or "")
    return value.lstrip("#>*-| ") or "Not specified"


def render_markdown(record: JobRecord, ingested: str) -> str:
    lines = [f"# {_md_value(record.title)} - {_md_value(record.company)}", "",
             f"**Company**: {_md_value(record.company)}",
             f"**Role**: {_md_value(record.title)}",
             f"**Source**: {record.source}",
             f"**Location**: {_md_value(record.location)}",
             f"**URL**: {record.url}",
             f"**Date Ingested**: {ingested}"]
    optional = (("Posted", record.posted_date), ("Updated", record.updated_date),
                ("Employment Type", record.employment_type),
                ("Workplace Type", record.workplace_type), ("Department", record.department),
                ("Salary", record.salary), ("Terms", ", ".join(record.terms) or None),
                ("Application URL", record.application_url))
    for label, value in optional:
        if value:
            lines.append(f"**{label}**: {value if label == 'Application URL' else _md_value(value)}")
    completeness = "Full" if record.description_complete else \
        f"Partial - {_md_value(record.completeness_note)}"
    lines += [f"**Description Completeness**: {completeness}",
              f"**Generated By**: {GENERATED_MARKER} {VERSION}", "", "## Description", ""]
    if record.description.strip():
        lines.append(record.description.strip())
    else:
        lines.append(f"_{_md_value(record.completeness_note)}_")
    return "\n".join(lines).rstrip() + "\n"


def body_hash(markdown: str) -> str:
    stable = "\n".join(line for line in markdown.split("\n")
                       if not line.startswith("**Date Ingested**:"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def safe_target(target_dir: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,140}\.md", name):
        raise ConfigError(f"refusing unsafe file name {name!r}")
    root = target_dir.resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ConfigError(f"refusing to write outside {root}")
    return path


def load_manifest(target_dir: Path) -> dict[str, Any]:
    path = target_dir / MANIFEST_NAME
    if not path.exists():
        return {"version": 1, "generator": GENERATED_MARKER, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is unreadable ({exc}); fix or remove it so generated files "
                          f"can be told apart from hand-written ones") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ConfigError(f"{path} has an unexpected structure")
    return data


@dataclass
class WriteResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped_manual: list[str] = field(default_factory=list)
    skipped_modified: list[str] = field(default_factory=list)
    skipped_duplicate: list[str] = field(default_factory=list)
    not_written: int = 0
    planned: list[str] = field(default_factory=list)


METADATA_FIELD = re.compile(r"^\*\*(Company|Role|Location)\*\*: (.*)$", re.MULTILINE)


def _file_dedup_key(path: Path) -> str | None:
    """Dedup key of a generated file written before the manifest stored one."""
    try:
        head = path.read_text(encoding="utf-8").split("## Description", 1)[0]
    except OSError:
        return None
    fields = dict(METADATA_FIELD.findall(head))
    location = fields.get("Location")
    return dedup_key(fields.get("Company"), fields.get("Role"),
                     None if location == "Not specified" else location)


def _cross_source_twin(record: JobRecord, names: Sequence[str],
                       files: dict[str, Any]) -> str | None:
    """A file already ingested for the same job from another source, if any.

    Mirrors the in-run rule: the same source never merges, two different ATS
    job ids never merge, and a full description is never dropped for a file
    that only holds a partial one.
    """
    for name in names:
        entry = files[name]
        if entry.get("source") == record.source:
            continue
        if strong_identity(entry.get("url", "")) and strong_identity(record.url):
            continue
        if record.description_complete and not entry.get("description_complete"):
            continue
        return name
    return None


def write_records(records: Sequence[JobRecord], target_dir: Path, today: dt.date,
                  dry_run: bool, write_metadata_only: bool) -> WriteResult:
    """Create or refresh generated files; never touch files the manifest does not own."""
    outcome = WriteResult()
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(target_dir)
    files: dict[str, Any] = manifest["files"]
    by_identity = {entry.get("identity"): name for name, entry in sorted(files.items())}
    by_key: dict[str, list[str]] = {}
    for name, entry in sorted(files.items()):
        if "dedup_key" not in entry:  # backfill entries from before dedup keys existed
            entry["dedup_key"] = _file_dedup_key(target_dir / name)
        if entry["dedup_key"]:
            by_key.setdefault(entry["dedup_key"], []).append(name)
    used: set[str] = set()
    ingested = today.isoformat()
    for record in records:
        if record.source == "Simplify" and not write_metadata_only:
            outcome.not_written += 1
            continue
        identity = record.identity()
        name = by_identity.get(identity)
        key = dedup_key(record.company, record.title, record.location)
        twin = _cross_source_twin(record, by_key.get(key, []), files) \
            if name is None and key else None
        if twin is not None:
            outcome.skipped_duplicate.append(f"{record.source}: {record.url} (already ingested "
                                             f"as {twin})")
            seen = files[twin].setdefault("also_seen_at", [])
            if not dry_run and record.url != files[twin].get("url") and record.url not in seen:
                seen.append(record.url)
            continue
        if name is None or name in used:
            stem = base_filename(record)
            name = f"{stem}.md"
            owner = files.get(name, {}).get("identity")
            if name in used or (owner is not None and owner != identity):
                name = f"{stem}_{hashlib.sha256(identity.encode()).hexdigest()[:8]}.md"
        used.add(name)
        path = safe_target(target_dir, name)
        text = render_markdown(record, ingested)
        entry = files.get(name)
        outcome.planned.append(name)
        if path.exists():
            if entry is None:
                outcome.skipped_manual.append(name)
                continue
            if file_hash(path) != entry.get("file_sha256"):
                outcome.skipped_modified.append(name)
                continue
            if body_hash(text) == entry.get("body_sha256"):
                outcome.unchanged.append(name)
                if not dry_run:
                    entry["last_seen"] = ingested
                continue
            bucket = outcome.updated
        else:
            bucket = outcome.created
        bucket.append(name)
        if dry_run:
            continue
        atomic_write(path, text)
        files[name] = {
            "identity": identity, "source": record.source, "url": record.url,
            "canonical_url": canonical_url(record.url), "also_seen_at": record.also_seen_at,
            "description_complete": record.description_complete,
            "body_sha256": body_hash(text), "file_sha256": file_hash(path), "dedup_key": key,
            "first_ingested": (entry or {}).get("first_ingested", ingested),
            "last_ingested": ingested, "last_seen": ingested,
        }
        if key and name not in by_key.setdefault(key, []):
            by_key[key].append(name)
    if not dry_run:
        manifest["files"] = dict(sorted(files.items()))
        atomic_write(target_dir / MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
    return outcome


# ---------------------------------------------------------------------------
# Orchestration, summary and CLI
# ---------------------------------------------------------------------------
ADAPTERS: dict[str, Callable[[RunContext], SourceResult]] = {
    "greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "simplify": fetch_simplify,
    "jooble": fetch_jooble, "jobright": fetch_jobright,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_jobs.py",
        description=("Fetch public job postings into target_jobs/ as Markdown that "
                     "pipeline.py can analyze. Default: every free source; the billable "
                     "Jobright/Apify source only runs with --allow-apify-spend."),
        epilog=("Credentials come only from the environment: JOOBLE_API_KEY (Jooble), "
                "APIFY_TOKEN (Jobright via Apify). Exit codes: 0 success, 1 fatal "
                "configuration/filesystem error or nothing runnable, 2 a source failed."))
    parser.add_argument("--source", default="all", choices=["all", *ADAPTERS],
                        help="source to fetch (default: all)")
    parser.add_argument("--keyword", help="search keyword for Jooble and Jobright")
    parser.add_argument("--location", help="search location for Jooble and Jobright")
    parser.add_argument("--country", help="country code for the Jobright actor (default CA)")
    parser.add_argument("--limit", type=int, help="maximum Jobright results (1-1000)")
    parser.add_argument("--allow-apify-spend", action="store_true",
                        help="permit a billable Apify actor run for --source jobright/all")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and normalize, report planned files, write nothing")
    parser.add_argument("--verbose", action="store_true",
                        help="show requests (credentials redacted), warnings and files")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached public responses and fetch fresh copies")
    parser.add_argument("--config", type=Path, help="config path (default config/companies.json)")
    parser.add_argument("--target-dir", type=Path, help="output directory (default target_jobs/)")
    parser.add_argument("--workspace", type=Path, help="workspace root (default: script dir)")
    return parser


def run(argv: Sequence[str] | None = None, *, env: dict[str, str] | None = None,
        opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
        today: dt.date | None = None, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ if env is None else env)
    workspace = (args.workspace or Path(__file__).resolve().parent).resolve()
    config_path = args.config or workspace / "config" / "companies.json"
    target_dir = (args.target_dir or workspace / "target_jobs").resolve()
    redact = Redactor([env.get("JOOBLE_API_KEY", ""), env.get("APIFY_TOKEN", "")])
    today = today or dt.date.today()
    try:
        if args.limit is not None and not 1 <= args.limit <= 1000:
            raise ConfigError("--limit must be between 1 and 1000")
        config, notes = load_config(config_path)
        http_cfg = config["http"]
        budgets = {name: cfg["max_requests_per_run"] for name, cfg in config["sources"].items()
                   if "max_requests_per_run" in cfg}
        budgets["jobright"] = 1
        client = HttpClient(redactor=redact, timeout=http_cfg["timeout_seconds"],
                            max_retries=http_cfg["max_retries"],
                            backoff=http_cfg["backoff_seconds"], budgets=budgets,
                            cache_dir=workspace / ".cache" / "fetch_jobs",
                            cache_ttl=http_cfg["cache_ttl_seconds"], refresh=args.refresh,
                            opener=opener, sleep=sleep)
        f = config["filters"]
        ctx = RunContext(config=config, client=client, redact=redact, today=today, env=env,
                         filters=Filters(f["title_keywords"], f["exclude_title_keywords"],
                                         f["locations"], f.get("max_age_days"),
                                         f.get("include_unspecified_remote", True)),
                         allow_apify_spend=args.allow_apify_spend, keyword=args.keyword,
                         location=args.location, country=args.country, limit=args.limit,
                         explicit_source=args.source != "all")
        selected = list(ADAPTERS) if args.source == "all" else [args.source]
        results: list[SourceResult] = []
        for name in selected:
            if not config["sources"][name].get("enabled", True):
                results.append(SourceResult(name, "skipped", "disabled in config"))
                continue
            try:
                results.append(ADAPTERS[name](ctx))
            except BudgetExceeded as exc:
                results.append(SourceResult(name, "failed", errors=[redact(str(exc))]))
            except ConfigError:
                raise
            except Exception as exc:  # a bug or odd payload fails only this source
                results.append(SourceResult(name, "failed", errors=[
                    f"internal error while processing {SOURCE_LABELS[name]}: "
                    f"{type(exc).__name__}: {redact(str(exc))[:200]}"]))
        records = [r for res in results for r in res.records]
        deduped = deduplicate(records)
        write_meta = config["sources"]["simplify"].get("write_metadata_only", True)
        written = write_records(deduped.kept, target_dir, today, args.dry_run, write_meta)
    except ConfigError as exc:
        out(f"ERROR: {redact(str(exc))}")
        return EXIT_FATAL
    except OSError as exc:
        out(f"ERROR: filesystem problem: {redact(str(exc))}")
        return EXIT_FATAL
    summary = build_summary(results, deduped, written, client, notes, args, target_dir,
                            workspace, redact)
    print_summary(summary, args.verbose, out)
    if not args.dry_run:
        try:
            outputs = workspace / "outputs"
            outputs.mkdir(parents=True, exist_ok=True)
            atomic_write(outputs / "fetch_summary.json",
                         redact(json.dumps(summary, indent=2, ensure_ascii=False)) + "\n")
        except OSError as exc:
            out(f"warning: could not save outputs/fetch_summary.json: {redact(str(exc))}")
    if any(r.status in ("failed", "partial") for r in results):
        return EXIT_SOURCE_FAILED
    if all(r.status == "skipped" for r in results) and ctx.explicit_source:
        return EXIT_FATAL
    return EXIT_OK


def build_summary(results: Sequence[SourceResult], deduped: DedupeResult, written: WriteResult,
                  client: HttpClient, notes: list[str], args: argparse.Namespace,
                  target_dir: Path, workspace: Path, redact: Redactor) -> dict[str, Any]:
    per_source = []
    for res in results:
        stats = client.stats.get(res.source, HttpStats())
        per_source.append({
            "source": SOURCE_LABELS[res.source], "status": res.status, "reason": res.reason,
            "boards_or_searches": res.boards, "requests": stats.requests,
            "cache_hits": stats.cache_hits, "retrieved": res.raw,
            "normalized": len(res.records), "rejected": res.rejected,
            "filtered_out": res.filtered, "stale_or_unlisted": res.stale,
            "partial_descriptions": sum(1 for r in res.records if not r.description_complete),
            "errors": [redact(e) for e in res.errors],
            "warnings": [redact(w) for w in res.warnings],
        })
    try:
        shown_target = target_dir.relative_to(workspace).as_posix()
    except ValueError:
        shown_target = str(target_dir)
    return {
        "fetch_jobs_version": VERSION,
        "dry_run": bool(args.dry_run),
        "target_dir": shown_target,
        "sources": per_source,
        "totals": {
            "retrieved": sum(r.raw for r in results),
            "normalized": sum(len(r.records) for r in results),
            "rejected": sum(r.rejected for r in results),
            "duplicates_removed": len(deduped.duplicates),
            "unique_jobs": len(deduped.kept),
            "partial_descriptions": sum(1 for r in deduped.kept if not r.description_complete),
            "files_created": len(written.created),
            "files_updated": len(written.updated),
            "files_unchanged": len(written.unchanged),
            "skipped_manual_files": len(written.skipped_manual),
            "skipped_hand_edited_files": len(written.skipped_modified),
            "skipped_already_ingested_duplicates": len(written.skipped_duplicate),
            "metadata_only_not_written": written.not_written,
        },
        "files": {"created": written.created, "updated": written.updated,
                  "unchanged": written.unchanged, "skipped_manual": written.skipped_manual,
                  "skipped_hand_edited": written.skipped_modified,
                  "skipped_already_ingested": written.skipped_duplicate},
        "duplicates": deduped.duplicates,
        "possible_duplicates": deduped.possible,
        "notes": [redact(n) for n in notes],
        "request_log": [redact(line) for line in client.log],
    }


def print_summary(summary: dict[str, Any], verbose: bool, out: Callable[[str], None]) -> None:
    mode = "DRY RUN (nothing written)" if summary["dry_run"] else f"target: {summary['target_dir']}"
    out(f"fetch_jobs {summary['fetch_jobs_version']} - {mode}")
    for note in summary["notes"]:
        out(f"  note: {note}")
    out(f"  {'source':<11} {'status':<8} {'req':>4} {'got':>6} {'valid':>6} {'filt':>6} "
        f"{'stale':>6} {'rej':>4} {'part':>5}")
    for src in summary["sources"]:
        out(f"  {src['source']:<11} {src['status']:<8} {src['requests']:>4} "
            f"{src['retrieved']:>6} {src['normalized']:>6} {src['filtered_out']:>6} "
            f"{src['stale_or_unlisted']:>6} {src['rejected']:>4} "
            f"{src['partial_descriptions']:>5}"
            + (f"  ({src['reason']})" if src["reason"] else ""))
        for error in src["errors"]:
            out(f"      error: {error}")
        if verbose:
            for warning in src["warnings"][:20]:
                out(f"      warning: {warning}")
    t = summary["totals"]
    verb = "would create" if summary["dry_run"] else "created"
    out(f"  totals: {t['unique_jobs']} unique jobs after removing {t['duplicates_removed']} "
        f"duplicates; {verb} {t['files_created']}, updated {t['files_updated']}, unchanged "
        f"{t['files_unchanged']}; skipped {t['skipped_manual_files']} hand-written and "
        f"{t['skipped_hand_edited_files']} hand-edited files; {t['partial_descriptions']} "
        f"partial descriptions")
    if t["skipped_already_ingested_duplicates"]:
        out(f"  skipped {t['skipped_already_ingested_duplicates']} posting(s) already ingested "
            f"from another source (same company, title and location)")
    if summary["possible_duplicates"]:
        out(f"  {len(summary['possible_duplicates'])} group(s) look similar but were kept "
            f"separate (see possible_duplicates in outputs/fetch_summary.json)")
    if verbose:
        for line in summary["request_log"]:
            out(f"    {line}")
        for name in summary["files"]["created"] + summary["files"]["updated"]:
            out(f"    file: {summary['target_dir']}/{name}")


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
