#!/usr/bin/env python3
"""Vouch Studio: the web app over pipeline.py, fetch_jobs.py and jobboard.py.

    ./run_ui.sh                       # creates .venv, starts http://127.0.0.1:8000
    ./run_ui.sh --make-admin EMAIL    # give your account the workspace tools
    ./run_ui.sh --process-builds      # run Pro builds queued on the hosted app, then exit

The engine stays on the standard library; FastAPI and uvicorn are used only here
(see requirements-ui.txt). Every route calls the engine's own functions in-process,
so scores, gates, provenance and briefings are exactly what the CLI produces.

Pages: / (home), /signup, /login, and /app (the board; needs an account).
Accounts (accounts.py) keep each user's uploaded profile; the board scores every job
against it. The admin account also gets the workspace: tailoring, fetching and the
master profile and template editors, which run on the owner's own files and are never
shown to other accounts.

    POST /api/auth/signup|login|logout  accounts; the session is an HttpOnly cookie
    GET  /api/me, /api/me/profile       who is signed in; their stored profile as text
    POST /api/me/profile[/delete]       upload or replace it ({filename, content_base64})
    POST /api/me/delete                 delete the account and its profile (password)
    GET  /api/stats                     public board counts for the home page
    GET  /api/meta                      board facts (and tools and tasks for the admin)
    GET  /api/jobs                      filtered, sorted, paginated jobs scored for you
    GET  /api/jobs/{job_id}             one job: description and your score
  admin only:
    POST /api/jobs/fetch                run fetch_jobs.run as a task (streams its log)
    POST /api/jobs/upload               save a pasted or uploaded description
    POST /api/tailor/{job_id}           run pipeline.run as a task (streams compiles)
    GET  /api/tailor/{job_id}           latest analysis, build gates and files
    GET  /api/provenance/{job_id}       provenance map plus the master profile lines
    GET  /api/fit/{job_id}              requirements, profile fit, resume fit, hard flags
    GET  /api/briefing/{job_id}         interview briefing and gap analysis
    GET  /api/files/{job_id}/{name}     resume PDF, .tex, logs, briefing and run report
    GET  /api/tasks/{task_id}[/events]  task state, or its log as Server-Sent Events
    GET  /api/profile, /api/template    editable sources with a parsed summary
    POST /api/profile, /api/template    validate, back up, then save atomically

The server binds to 127.0.0.1. Requests must name a local Host (DNS rebinding),
and state-changing requests must carry the X-Vouch-Studio header, which a
cross-site page cannot send without a CORS preflight this server never grants; the
session cookie is also SameSite=Lax. One task runs at a time, and the profile and
template cannot be saved while it runs: CLAUDE.md forbids editing them during a
pipeline run.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import accounts
import fetch_jobs
import jobboard
import pipeline
import resume_kit
import storage
import user_template

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
STUDIO_HEADER = "x-vouch-studio"
SESSION_COOKIE = "vouch_session"
SCORE_CACHE_SIZE = 16  # parsed profiles and their board scores, least recently used out
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_BODY_BYTES = 4_000_000
MAX_PROFILE_BYTES = 2_000_000
MAX_TEMPLATE_BYTES = 500_000
BACKUPS_KEPT = 10
MAX_TASK_LINES = 20_000
FINISHED_TASKS_KEPT = 20
SSE_KEEPALIVE_SECONDS = 15
PAGE_SIZE_MAX = 200
LOG_TAIL_LINES = 80
TOOL_DIRS = ("/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin")
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
ENGINES = frozenset({"pdflatex", "xelatex", "lualatex"})
FETCH_SOURCES = ("free", "all", *fetch_jobs.ADAPTERS)
SERVED_FILES = {"resume.pdf": "application/pdf", "resume.tex": "text/plain",
                "resume.log": "text/plain", "resume.failed.tex": "text/plain",
                "resume.failed.log": "text/plain", "resume.unverified.tex": "text/plain",
                "briefing.md": "text/markdown", "report.md": "text/markdown",
                "job.json": "application/json", "fit_report.md": "text/markdown"}
EXIT_MEANINGS = {
    pipeline.EXIT_OK: "verified",
    pipeline.EXIT_INVALID_INPUT: "invalid input",
    pipeline.EXIT_LOW_FIT: "low fit",
    pipeline.EXIT_MISSING_PREREQ: "missing prerequisite",
    pipeline.EXIT_FAILED: "failed",
    pipeline.EXIT_PAGE_LIMIT: "page limit",
}
FETCH_EXIT_MEANINGS = {fetch_jobs.EXIT_OK: "ok", fetch_jobs.EXIT_FATAL: "fatal error",
                       fetch_jobs.EXIT_SOURCE_FAILED: "a source failed"}


def find_tool(name: str) -> str | None:
    """shutil.which, plus the TeX and Homebrew folders a GUI-launched shell may lack."""
    found = shutil.which(name)
    if found:
        return found
    for folder in TOOL_DIRS:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Background tasks with a streamable log
# ---------------------------------------------------------------------------
class Task:
    def __init__(self, kind: str, label: str, job_id: str | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.job_id = job_id
        self.status = "running"
        self.exit_code: int | None = None
        self.outcome = ""
        self.result: dict[str, Any] | None = None
        self.lines: list[str] = []
        self.started = time.time()
        self.finished: float | None = None
        self._cond = threading.Condition()

    def emit(self, text: str) -> None:
        with self._cond:
            for line in str(text).splitlines() or [""]:
                if len(self.lines) < MAX_TASK_LINES:
                    self.lines.append(line)
            self._cond.notify_all()

    def finish(self, status: str, exit_code: int | None, outcome: str,
               result: dict[str, Any] | None) -> None:
        with self._cond:
            self.status, self.exit_code, self.outcome, self.result = (status, exit_code,
                                                                      outcome, result)
            self.finished = time.time()
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float) -> tuple[list[str], bool]:
        with self._cond:
            if len(self.lines) <= seen and self.status == "running":
                self._cond.wait(timeout)
            return self.lines[seen:], self.status != "running"

    def view(self, lines: bool = True) -> dict[str, Any]:
        with self._cond:
            data: dict[str, Any] = {
                "id": self.id, "kind": self.kind, "label": self.label, "job_id": self.job_id,
                "status": self.status, "exit_code": self.exit_code, "outcome": self.outcome,
                "started": self.started, "finished": self.finished,
                "line_count": len(self.lines), "result": self.result}
            if lines:
                data["lines"] = list(self.lines)
            return data


class LoggingRunner:
    """Wraps pipeline's subprocess runner to narrate every compile and PDF check live."""

    def __init__(self, emit: Callable[[str], None],
                 runner: Callable[..., subprocess.CompletedProcess]) -> None:
        self.emit = emit
        self.runner = runner
        self.compiles = 0

    def __call__(self, command, **kwargs):
        exe = Path(command[0]).name
        if exe in ENGINES:
            self.compiles += 1
            self.emit(f"[{exe} #{self.compiles}] compiling {Path(command[-1]).name}")
        try:
            result = self.runner(command, **kwargs)
        except subprocess.TimeoutExpired:
            self.emit(f"[{exe}] timed out")
            raise
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        if exe in ENGINES:
            # TeX wraps its console at 79 columns; unwrap to read "(2 pages, 70123 bytes)".
            written = re.search(r"Output written on .*?\((\d+ pages?, \d+ bytes)\)",
                                stdout.replace("\n", ""))
            self.emit(f"[{exe} #{self.compiles}] exit {result.returncode}"
                      + (f" - {written.group(1)}" if written else ""))
            if result.returncode != 0:
                for line in stdout.strip().splitlines()[-15:]:
                    self.emit(f"    {line}")
        elif exe == "pdfinfo":
            pages = re.search(r"^Pages:\s+(\d+)", stdout, re.MULTILINE)
            self.emit(f"[pdfinfo] {pages.group(1) if pages else '?'} page(s)")
        elif exe == "pdftotext":
            self.emit(f"[pdftotext] {stdout.count(chr(12))} form feed(s), "
                      f"{len(stdout)} characters of text")
        return result


class LoggingOpener:
    """Wraps fetch_jobs' HTTP opener to report each request by host only.

    Only the host is shown: a Jooble key travels in the URL path, so paths are
    never logged here (fetch_jobs redacts its own messages).
    """

    def __init__(self, emit: Callable[[str], None], opener: Callable[..., Any] | None) -> None:
        self.emit = emit
        self.opener = opener or fetch_jobs.urllib.request.urlopen

    def __call__(self, request, timeout=None):
        host = urllib.parse.urlsplit(request.full_url).hostname or "?"
        self.emit(f"[http] {request.get_method()} {host}")
        return self.opener(request, timeout=timeout)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class FetchRequest(BaseModel):
    source: str = "free"
    query: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    limit: int | None = Field(default=None, ge=1, le=1000)
    allow_apify_spend: bool = False
    dry_run: bool = False
    refresh: bool = False


class UploadRequest(BaseModel):
    text: str = Field(max_length=pipeline.MAX_JD_BYTES)
    filename: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, max_length=160)


class ProfileUpload(BaseModel):
    filename: str = Field(max_length=200)
    content_base64: str = Field(max_length=MAX_BODY_BYTES)


class SignupRequest(BaseModel):
    email: str = Field(max_length=accounts.EMAIL_MAX)
    password: str = Field(max_length=accounts.PASSWORD_MAX)
    name: str = Field(default="", max_length=accounts.NAME_MAX)


class LoginRequest(BaseModel):
    email: str = Field(max_length=accounts.EMAIL_MAX)
    password: str = Field(max_length=accounts.PASSWORD_MAX)


class PasswordConfirm(BaseModel):
    password: str = Field(max_length=accounts.PASSWORD_MAX)


class EducationItem(BaseModel):
    school: str = Field(default="", max_length=200)
    degree: str = Field(default="", max_length=200)
    dates: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=200)


class ResumeHeader(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=200)
    links: list[str] = Field(default_factory=list, max_length=10)
    education: list[EducationItem] = Field(default_factory=list, max_length=10)


class ProRequest(BaseModel):
    note: str = Field(default="", max_length=accounts.NOTE_MAX)


class BuildRequest(BaseModel):
    override_low_fit: bool = False


class PlanChange(BaseModel):
    plan: str = Field(pattern=r"^(basic|pro)$")


class TailorRequest(BaseModel):
    override_low_fit: bool = False
    strict_no: bool = False
    analyze_gap: bool = False
    no_cache: bool = False


class SourceUpdate(BaseModel):
    text: str = Field(max_length=MAX_PROFILE_BYTES)
    base_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SourceCheck(BaseModel):
    text: str = Field(max_length=MAX_PROFILE_BYTES)


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------
class Busy(Exception):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


ENGINE_SOURCES: dict[str, str] = {}  # engine module -> sha256 of the source Studio is running


def _engine_modules() -> list[Any]:
    """Engine modules in import order (pipeline imports resume_kit)."""
    return [m for m in (sys.modules.get("resume_kit"), pipeline) if m is not None]


def remember_engine() -> None:
    for module in _engine_modules():
        ENGINE_SOURCES[module.__name__] = _sha(Path(module.__file__).read_bytes())


def refresh_engine(emit: Callable[[str], None]) -> bool:
    """Reload engine modules edited since Studio loaded them; True when any was reloaded.

    Studio runs the engine in-process, so without this an edited pipeline.py would only
    take effect after a restart and a run could silently use the old code.
    """
    changed = [m.__name__ for m in _engine_modules()
               if _sha(Path(m.__file__).read_bytes()) != ENGINE_SOURCES.get(m.__name__)]
    if not changed:
        return False
    for module in _engine_modules():
        importlib.reload(module)
    remember_engine()
    emit(f"Reloaded {', '.join(f'{name}.py' for name in changed)}: changed on disk since "
         f"Studio loaded it")
    return True


remember_engine()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tail(path: Path, count: int = LOG_TAIL_LINES) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


class Studio:
    def __init__(self, workspace: Path, *, which: Callable[[str], str | None] = find_tool,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 opener: Callable[..., Any] | None = None, env: dict[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 backup_dir: Path | None = None, db_path: Path | str | None = None,
                 admin_emails: frozenset[str] | None = None) -> None:
        self.workspace = workspace.resolve()
        self.target_dir = self.workspace / "target_jobs"
        self.outputs = self.workspace / pipeline.OUTPUT_DIR_NAME
        self.backup_dir = backup_dir or self.workspace / ".studio" / "backups"
        self.which = which
        self.runner = runner
        self.opener = opener
        self.env = env
        self.sleep = sleep
        self.lock = threading.RLock()
        self.tasks: dict[str, Task] = {}
        self.active: Task | None = None
        self._index: dict[str, Any] | None = None
        # Board scores per profile file and index: key -> (rows, profile summary).
        self._scores: collections.OrderedDict[tuple, tuple[dict[str, dict[str, Any]],
                                                           dict[str, Any]]] | None = None
        # Accounts live in Postgres when DATABASE_URL is set (Neon), else in SQLite here.
        db = db_path or (self.env or os.environ).get("DATABASE_URL") or \
            self.workspace / ".studio" / "vouch.db"
        if admin_emails is None:  # never in git: .studio/admins.txt or VOUCH_ADMIN_EMAILS
            admin_emails = accounts.read_admin_emails(self.workspace / ".studio" / "admins.txt",
                                                      self.env if self.env is not None else None)
        # Profiles and build PDFs go to Cloudflare R2 when R2_* is set (shared with Vercel).
        store = None if db_path else storage.from_env(self.env if self.env is not None else None)
        self.accounts = accounts.Accounts(db, admin_emails=admin_emails, store=store)
        self.build_lock = threading.Lock()  # one Pro build compiles at a time

    # -- paths ---------------------------------------------------------------
    def master_path(self) -> Path:
        return pipeline.find_master(self.workspace, None)

    def template_path(self) -> Path:
        return self.workspace / pipeline.TEMPLATE_NAME

    def job_path(self, job_id: str) -> Path:
        if not JOB_ID.match(job_id):
            raise HTTPException(404, "Unknown job")
        for extension in pipeline.JD_EXTENSIONS:
            path = (self.target_dir / f"{job_id}{extension}").resolve()
            if path.parent == self.target_dir.resolve() and path.is_file():
                return path
        raise HTTPException(404, f"No job file target_jobs/{job_id}.md")

    def display_path(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    # -- index and scores ----------------------------------------------------
    def index(self) -> dict[str, Any]:
        with self.lock:
            if self._index is None:
                self._index = jobboard.load_or_build_index(self.workspace)
            return self._index

    def reindex(self) -> dict[str, Any]:
        index = jobboard.load_or_build_index(self.workspace)
        with self.lock:
            self._index = index
            self._scores = None
        return index

    def _profile_file(self, user: dict[str, Any] | None, workspace: bool = False
                      ) -> tuple[str, bytes, str] | None:
        """(filename, bytes, origin) of the profile that scores the board for `user`.

        Every account is scored against the file it uploaded. Only the admin can fall back
        to the workspace master profile (or ask for it with `workspace`); no other account
        is ever scored against, or shown, that file.
        """
        if user is not None and not workspace:
            stored = self.accounts.profile(user["id"])
            if stored is not None:
                return stored["filename"], stored["content"], "account"
        if user is not None and user.get("is_admin"):
            try:
                master = self.master_path()
                return master.name, master.read_bytes(), "workspace"
            except (pipeline.PipelineError, OSError):
                return None
        return None

    def scores(self, user: dict[str, Any] | None, workspace: bool = False
               ) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
        """Board scores for `user`'s profile, cached per profile file and index."""
        found = self._profile_file(user, workspace)
        if found is None:
            return {}, None
        filename, data, origin = found
        index = self.index()
        key = (_sha(data), filename, id(index), len(index["jobs"]))
        with self.lock:
            cached = (self._scores or collections.OrderedDict()).get(key)
            if cached is not None:
                self._scores.move_to_end(key)
                return cached[0], {**cached[1], "source": origin}
        try:
            profile = jobboard.load_profile(filename, data)
        except jobboard.ProfileError as exc:
            return {}, {"error": str(exc), "filename": filename, "source": origin}
        rows = {row["id"]: row for row in jobboard.score_jobs(profile, index["jobs"])}
        summary = profile.summary()
        with self.lock:
            if self._scores is None:
                self._scores = collections.OrderedDict()
            self._scores[key] = (rows, summary)
            while len(self._scores) > SCORE_CACHE_SIZE:
                self._scores.popitem(last=False)
        return rows, {**summary, "source": origin}

    def score_counts(self, user: dict[str, Any]) -> dict[str, Any]:
        scores, profile = self.scores(user)
        ranked = [s for s in scores.values() if not s["insufficient"]]
        return {"summary": profile, "threshold": pipeline.FIT_THRESHOLD,
                "scored": len(ranked),
                "proceed": sum(1 for s in ranked if s["score"] >= pipeline.FIT_THRESHOLD),
                "thin": sum(1 for s in scores.values() if s["insufficient"])}

    # -- accounts' own profiles -----------------------------------------------
    def my_profile(self, user: dict[str, Any]) -> dict[str, Any]:
        """The account's stored file as editable text (`stored`), plus how the profile
        that scores its board reads (`summary`) and what it scores."""
        stored = self.accounts.profile(user["id"])
        if stored is None:
            return {"stored": None, **self.score_counts(user)}
        try:
            text = jobboard.decode_upload(stored["filename"], stored["content"])
        except jobboard.ProfileError as exc:
            text = None
            error = str(exc)
        else:
            error = None
        editable = Path(stored["filename"]).suffix.lower() in (".md", ".txt")
        return {"stored": {"filename": stored["filename"], "text": text, "editable": editable,
                            "error": error, "sha256": stored["sha256"],
                            "updated": dt.datetime.fromtimestamp(stored["updated_at"]).isoformat(
                                timespec="seconds")},
                **self.score_counts(user)}

    def save_my_profile(self, user: dict[str, Any], body: ProfileUpload) -> dict[str, Any]:
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, "The upload is not valid base64") from None
        if len(data) > MAX_PROFILE_BYTES:
            raise HTTPException(413, "Profiles are limited to 2 MB")
        try:
            jobboard.load_profile(body.filename, data)  # refuse what cannot be scored
        except jobboard.ProfileError as exc:
            raise HTTPException(422, str(exc)) from None
        self.accounts.save_profile(user["id"], body.filename, data)
        return self.my_profile(user)

    def delete_my_profile(self, user: dict[str, Any]) -> dict[str, Any]:
        self.accounts.delete_profile(user["id"])
        return self.my_profile(user)

    # -- Pro: a tailored resume built from the account's own profile ------------------
    def build_requirements(self, user: dict[str, Any]) -> dict[str, Any]:
        """What a build needs, and what is still missing, for this account."""
        missing: list[str] = []
        stored = self.accounts.profile(user["id"])
        text = None
        if stored is None:
            missing.append("profile")
        else:
            try:
                text = jobboard.decode_upload(stored["filename"], stored["content"])
            except jobboard.ProfileError:
                missing.append("profile")
        if text is not None and not jobboard.is_master(text):
            missing.append("master")
        elif text is not None and not resume_kit.has_kit(text.splitlines()):
            missing.append("kit")
        header = self.accounts.header(user["id"])
        if not header or not header.get("name"):
            missing.append("header")
        return {"pro": user["is_pro"], "missing": missing, "ready": user["is_pro"] and not missing,
                "text": text, "header": header}

    def build_resume(self, user: dict[str, Any], job_id: str, override_low_fit: bool = False
                     ) -> dict[str, Any]:
        """Run the real pipeline on this account's master profile and a template generated
        from its own resume kit and header; keep the result (PDF included) in its account."""
        if not user["is_pro"]:
            raise HTTPException(403, "Tailored resumes are a Pro feature. Request Pro on your "
                                     "profile page.")
        path = self.job_path(job_id)
        needs = self.build_requirements(user)
        messages = {"profile": "Upload your master profile first.",
                    "master": "Building a resume needs a tagged master profile ([V] and [NO] "
                              "lines), not a plain resume.",
                    "kit": "Your master profile needs a resume kit (section 9) with your "
                           "roles, projects and bullet bank.",
                    "header": "Add your resume header (at least your name) first."}
        if needs["missing"]:
            raise HTTPException(422, messages[needs["missing"][0]])
        if not self.build_lock.acquire(blocking=False):
            raise HTTPException(409, "Another resume is being built right now. Try again in a "
                                     "minute.")
        try:
            with tempfile.TemporaryDirectory(prefix="vouch-build-") as tmp:
                root = Path(tmp)
                (root / "target_jobs").mkdir()
                jd = root / "target_jobs" / path.name
                jd.write_bytes(path.read_bytes())
                master = root / "master.md"
                master.write_text(needs["text"], encoding="utf-8")
                try:
                    kit = pipeline.load_kit(pipeline.parse_master(master))
                    header = user_template.clean_header(needs["header"])
                    template = user_template.build_template(header, kit)
                except (pipeline.PipelineError, user_template.TemplateInputError) as exc:
                    raise HTTPException(422, str(exc)) from None
                (root / "template.tex").write_text(template, encoding="utf-8")
                argv = [str(jd), "--workspace", str(root), "--master", str(master),
                        "--template", str(root / "template.tex"), "--output-dir",
                        str(root / "out")]
                if override_low_fit:
                    argv.append("--override-low-fit")
                log: list[str] = []
                code = pipeline.run(argv, which=self.which, runner=self.runner, out=log.append)
                found = [p for p in (root / "out").glob("*_analysis.json")
                         if not p.name.endswith("_gap_analysis.json")]
                analysis = _read_json(found[0]) if found else {}
                base = found[0].name[:-len("_analysis.json")] if found else None
                resume = (analysis or {}).get("resume") or {}
                fit = (analysis or {}).get("fit") or {}
                pdf_path = root / "out" / f"{base}_resume.pdf" if base else None
                verified = resume.get("status") == "verified" and pdf_path is not None \
                    and pdf_path.is_file()
                report = root / "out" / f"{base}_report.md" if base else None
                summary = {
                    "exit_code": code, "outcome": EXIT_MEANINGS.get(code, f"exit {code}"),
                    "verdict": (analysis or {}).get("verdict"),
                    "fit_score": (analysis or {}).get("fit_score"),
                    "profile_fit": fit.get("profile_fit"), "resume_fit": fit.get("resume_fit"),
                    "hard_requirement_flags": fit.get("hard_requirement_flags", []),
                    "work_authorization_note": fit.get("work_authorization_note"),
                    "role_family": (analysis or {}).get("role_family"),
                    "resume_status": resume.get("status"), "reason": resume.get("reason"),
                    "gates": {k: v.get("status") for k, v in
                              (resume.get("build_gates") or {}).items()},
                    "override_low_fit": override_low_fit,
                    "log_tail": [line for line in log if not line.startswith("Master profile:")][-12:]}
                job = next((j for j in self.index()["jobs"] if j["id"] == job_id), {})
                self.accounts.save_build(
                    user["id"], job_id, job.get("title") or job_id, job.get("company") or "",
                    "verified" if verified else (resume.get("status") or summary["outcome"]),
                    summary, pdf_path.read_bytes() if verified else None,
                    report.read_text(encoding="utf-8") if report and report.is_file() else None)
        finally:
            self.build_lock.release()
        return self.build_view(user, job_id)

    def process_builds(self, emit: Callable[[str], None] = print) -> int:
        """Run every build the hosted app queued (it has no LaTeX); returns how many ran."""
        done = 0
        for queued in self.accounts.queued_builds():
            user = self.accounts.user_by_id(queued["user_id"])
            job_id = queued["job_id"]
            if user is None:
                continue
            emit(f"Building {job_id} for {user['email']} ...")
            try:
                view = self.build_resume(user, job_id,
                                         bool(queued["summary"].get("override_low_fit")))
            except (HTTPException, accounts.AccountError, storage.StorageError) as exc:
                reason = str(exc.detail if isinstance(exc, HTTPException) else exc)
                self.accounts.fail_build(user["id"], job_id, reason)
                emit(f"  failed: {reason}")
                continue
            emit(f"  {view['status'] if view else 'no result'}")
            done += 1
        return done

    def build_view(self, user: dict[str, Any], job_id: str) -> dict[str, Any] | None:
        build = self.accounts.build(user["id"], job_id, with_pdf=False)
        if build is None:
            return None
        return {"job_id": job_id, "title": build["title"], "company": build["company"],
                "status": build["status"], "summary": build["summary"],
                "has_pdf": build["has_pdf"], "has_report": bool(build["report"]),
                "created": dt.datetime.fromtimestamp(build["created_at"]).isoformat(
                    timespec="seconds")}

    # -- admin console ---------------------------------------------------------------
    def admin_overview(self) -> dict[str, Any]:
        def when(ts: float | None) -> str | None:
            return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None
        users = [{**u, "created_at": when(u["created_at"])} for u in self.accounts.list_users()]
        requests = [{**r, "created_at": when(r["created_at"]), "decided_at": when(r["decided_at"])}
                    for r in self.accounts.list_requests(status=None)]
        return {"users": users, "requests": requests,
                "pending": sum(1 for r in requests if r["status"] == "pending"),
                "database": self.accounts.backend}

    def outputs_by_job(self) -> dict[str, dict[str, Any]]:
        """Latest analysis per job description, keyed by its workspace-relative path."""
        found: dict[str, dict[str, Any]] = {}
        if not self.outputs.is_dir():
            return found
        for path in sorted(self.outputs.glob("*_analysis.json")):
            if path.name.endswith("_gap_analysis.json"):  # same JD path, different report
                continue
            data = _read_json(path)
            if not data or not isinstance(data.get("source_job_description"), str):
                continue
            resume = data.get("resume") or {}
            found[data["source_job_description"]] = {
                "base": path.name[:-len("_analysis.json")],
                "fit_score": data.get("fit_score"), "verdict": data.get("verdict"),
                "resume_status": resume.get("status"),
                "updated": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                    timespec="seconds")}
        return found

    def base_for(self, job_id: str) -> str:
        info = self.outputs_by_job().get(self.display_path(self.job_path(job_id)))
        if not info:
            raise HTTPException(404, "This job has not been tailored yet")
        return info["base"]

    def output_file(self, base: str, suffix: str) -> Path:
        return pipeline.safe_output_path(self.outputs, f"{base}_{suffix}")

    # -- jobs ----------------------------------------------------------------
    def list_jobs(self, user: dict[str, Any] | None, q: str = "", region: str = "",
                  level: str = "", source: str = "", verdict: str = "", tailored: bool = False,
                  min_score: int | None = None, sort: str = "match", offset: int = 0,
                  limit: int = 50) -> dict[str, Any]:
        index = self.index()
        scores, profile = self.scores(user)
        admin = bool(user and user.get("is_admin"))
        outputs = self.outputs_by_job() if admin else {}  # the admin's own tailoring runs
        tailored = tailored and admin
        needle = q.casefold().strip()
        levels = {name for name in level.split(",") if name}  # one level or several
        rows = []
        facets: dict[str, collections.Counter] = {k: collections.Counter() for k in
                                                  ("region", "level", "source", "verdict")}
        for job in index["jobs"]:
            view = jobboard.public_job(job)
            score = scores.get(job["id"])
            view["score"] = score["score"] if score else None
            view["rank"] = score["rank"] if score else None
            view["verdict"] = score["verdict"] if score else None
            view["insufficient"] = bool(score and score["insufficient"])
            view["matched"] = (score.get("matched") or [])[:8] if score else []
            view["gaps"] = (score.get("gaps") or [])[:6] if score else []
            view["requirement_count"] = score["requirements"] if score else None
            view["file"] = job["file"]
            out = outputs.get(f"target_jobs/{job['file']}")
            if admin:
                view["tailored"] = out
            view["verdict_key"] = ("proceed" if view["verdict"] == pipeline.VERDICT_PROCEED else
                                   "low_fit" if view["verdict"] else
                                   "thin" if view["insufficient"] else
                                   "listing" if not view["complete"] else "unscored")
            for key, value in (("region", view["region"]), ("level", view["level"]),
                               ("source", view["source"]), ("verdict", view["verdict_key"])):
                facets[key][value] += 1
            if needle and needle not in " ".join(
                    str(view[k] or "") for k in ("title", "company", "location", "id")).casefold():
                continue
            if (region and view["region"] != region) or (levels and view["level"] not in levels) \
                    or (source and view["source"] != source) \
                    or (verdict and view["verdict_key"] != verdict) or (tailored and not out):
                continue
            if min_score is not None and (view["score"] is None or view["score"] < min_score):
                continue
            rows.append(view)
        rows.sort(key=lambda r: r["id"])
        if sort == "recent":  # newest first; undated last (stable sorts keep id order)
            rows.sort(key=lambda r: r["posted"] or "", reverse=True)
        elif sort == "tailored" and admin:
            rows.sort(key=lambda r: r["tailored"]["updated"] if r["tailored"] else "",
                      reverse=True)
        elif sort == "company":
            rows.sort(key=lambda r: (r["company"].casefold(), r["title"].casefold()))
        elif sort == "score":
            rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
        else:  # "match": the board's rank, which discounts scores resting on few requirements
            rows.sort(key=lambda r: (r["rank"] is None, -(r["rank"] or 0), -(r["score"] or 0)))
        limit = max(1, min(limit, PAGE_SIZE_MAX))
        return {"total": len(index["jobs"]), "matching": len(rows), "offset": offset,
                "scorable": sum(1 for j in index["jobs"] if jobboard.has_enough_detail(j)),
                "limit": limit, "jobs": rows[offset:offset + limit],
                "facets": {k: dict(sorted(v.items())) for k, v in facets.items()},
                "profile": profile, "index_generated": index.get("generated")}

    def job_detail(self, job_id: str, user: dict[str, Any]) -> dict[str, Any]:
        path = self.job_path(job_id)
        job = next((j for j in self.index()["jobs"] if j["id"] == job_id), None)
        view = jobboard.public_job(job) if job else {"id": job_id, "title": job_id}
        mine, profile = self.scores(user)
        view["my_score"] = mine.get(job_id)
        view["profile"] = profile
        view["my_build"] = self.build_view(user, job_id)
        view["file"] = path.name
        view["text"] = path.read_text(encoding="utf-8", errors="replace")
        if user.get("is_admin"):  # tailoring runs on the workspace master profile
            gate, _profile = self.scores(user, workspace=True)
            view["board_score"] = gate.get(job_id)
            view["results"] = self.results(job_id)
            active = self.active
            view["active_task"] = active.view(False) if active and active.job_id == job_id \
                else None
        return view

    def save_upload(self, body: UploadRequest) -> dict[str, Any]:
        text = body.text.replace("\r\n", "\n").strip()
        if not text:
            raise HTTPException(422, "The job description is empty")
        if "\x00" in text:
            raise HTTPException(422, "The job description looks like a binary file")
        if len(text.encode("utf-8")) > pipeline.MAX_JD_BYTES:
            raise HTTPException(413, "Job descriptions are limited to 1 MB")
        role = (body.role or "").strip()
        company = (body.company or "").strip()
        if role and company and not text.lstrip().startswith("# "):
            text = f"# {role} at {company}\n\n{text}"
        stem_source = " ".join(p for p in (company, role) if p) or \
            Path(body.filename or "").stem or text.splitlines()[0]
        slug = re.sub(r"[^a-z0-9]+", "_", pipeline.slugify(stem_source, 80)).strip("_") or "job"
        stem = f"manual_{slug}"
        self.target_dir.mkdir(parents=True, exist_ok=True)
        path = self.target_dir / f"{stem}.md"
        if path.exists():
            if path.read_text(encoding="utf-8", errors="replace").strip() == text:
                raise HTTPException(409, f"This description is already saved as {path.name}")
            path = self.target_dir / f"{stem}_{_sha(text.encode())[:8]}.md"
            if path.exists():
                raise HTTPException(409, f"This description is already saved as {path.name}")
        pipeline.atomic_write_text(path, text + "\n")
        self.reindex()
        return {"id": path.stem, "file": path.name}

    # -- results -------------------------------------------------------------
    def results(self, job_id: str) -> dict[str, Any]:
        path = self.job_path(job_id)
        info = self.outputs_by_job().get(self.display_path(path))
        if not info:
            return {"available": False}
        base = info["base"]
        analysis = _read_json(self.output_file(base, "analysis.json")) or {}
        resume = analysis.get("resume") or {}
        present = {name: self.output_file(base, name).is_file() for name in (
            "resume.pdf", "resume.tex", "resume.log", "resume.failed.log", "resume.failed.tex",
            "resume.unverified.tex", "provenance.json", "briefing.json", "briefing.md",
            "gap_analysis.json", "requirements.json", "report.md", "job.json")}
        log_name = "resume.failed.log" if present["resume.failed.log"] and \
            resume.get("status") != "verified" else "resume.log"
        log_lines = _tail(self.output_file(base, log_name)) if present[log_name] else []
        pdf = None
        if present["resume.pdf"]:
            pdf_path = self.output_file(base, "resume.pdf")
            status = resume.get("status")
            pdf = {"url": f"/api/files/{job_id}/resume.pdf?v={_sha(pdf_path.read_bytes())[:12]}",
                   "state": "verified" if status == "verified" else
                   "previous_verified" if resume.get("last_valid_pdf_kept") else "unverified"}
        return {
            "available": True, "base": base, "updated": info["updated"],
            "company": analysis.get("company"), "role": analysis.get("role"),
            "fit_score": analysis.get("fit_score"), "verdict": analysis.get("verdict"),
            "threshold": pipeline.FIT_THRESHOLD,
            "primary_domain": analysis.get("primary_domain"),
            "secondary_domains": analysis.get("secondary_domains", []),
            "score_confidence": analysis.get("score_confidence"),
            "description_completeness": analysis.get("description_completeness"),
            "evidence_coverage": analysis.get("evidence_coverage"),
            "score_breakdown": analysis.get("score_breakdown"),
            "requirements": analysis.get("requirements", []),
            "parsing_warnings": analysis.get("parsing_warnings", []),
            "fit": analysis.get("fit"),
            "role_family": analysis.get("role_family"),
            "resume": {k: resume.get(k) for k in (
                "status", "reason", "generated", "compiled", "single_page_verified",
                "page_count", "override_low_fit", "build_gates", "fitting", "verification",
                "provenance", "last_valid_pdf_kept", "template_items_removed",
                "rejected_template_bullets", "warnings", "selected_content", "kit_notes",
                "summary_source", "summary_facts", "project_ranking")},
            "files": present, "pdf": pdf,
            "log": {"name": log_name, "tail": log_lines,
                    "fatal": pipeline.fatal_log_lines("\n".join(log_lines))},
        }

    def provenance(self, job_id: str) -> dict[str, Any]:
        base = self.base_for(job_id)
        proof = _read_json(self.output_file(base, "provenance.json"))
        if proof is None:
            raise HTTPException(404, "No provenance map yet: run tailoring for this job")
        master = pipeline.parse_master(self.master_path())
        current = pipeline.sha256_text("\n".join(line.raw for line in master.lines))
        return {"provenance": proof,
                "stale": proof.get("master_profile_sha256") != current,
                "master_file": master.path.name,
                "master_lines": [{"n": line.number, "text": line.raw, "tag": line.tag,
                                  "kind": line.kind} for line in master.lines]}

    def fit_report(self, job_id: str) -> dict[str, Any]:
        base = self.base_for(job_id)
        document = _read_json(self.output_file(base, "requirements.json"))
        if document is None:
            raise HTTPException(404, "No fit report yet: run tailoring for this job")
        return document

    def briefing(self, job_id: str) -> dict[str, Any]:
        base = self.base_for(job_id)
        briefing = _read_json(self.output_file(base, "briefing.json"))
        md_path = self.output_file(base, "briefing.md")
        return {"briefing": briefing,
                "markdown": md_path.read_text(encoding="utf-8") if md_path.is_file() else None,
                "gap": _read_json(self.output_file(base, "gap_analysis.json"))}

    def file_for(self, job_id: str, name: str) -> tuple[Path, str]:
        if name not in SERVED_FILES:
            raise HTTPException(404, "Unknown file")
        path = self.output_file(self.base_for(job_id), name)
        if not path.is_file():
            raise HTTPException(404, f"No {name} for this job")
        return path, SERVED_FILES[name]

    # -- tasks ---------------------------------------------------------------
    def start(self, kind: str, label: str, job_id: str | None,
              work: Callable[[Task], tuple[int, str, dict[str, Any] | None]]) -> Task:
        with self.lock:
            if self.active is not None and self.active.status == "running":
                raise Busy(f"'{self.active.label}' is still running")
            task = Task(kind, label, job_id)
            self.active = task
            self.tasks[task.id] = task
            finished = [t for t in self.tasks.values() if t.status != "running"]
            for old in sorted(finished, key=lambda t: t.started)[:-FINISHED_TASKS_KEPT]:
                del self.tasks[old.id]

        def target() -> None:
            try:
                code, outcome, result = work(task)
                status = "done" if code == 0 or (kind == "tailor" and
                                                 code == pipeline.EXIT_LOW_FIT) else "failed"
                task.finish(status, code, outcome, result)
            except Exception as exc:  # report, never kill the server
                task.emit(f"internal error: {type(exc).__name__}: {exc}")
                task.emit(traceback.format_exc(limit=3))
                task.finish("failed", None, "internal error", None)

        threading.Thread(target=target, name=f"studio-{kind}-{task.id}", daemon=True).start()
        return task

    def tailor(self, job_id: str, body: TailorRequest) -> Task:
        path = self.job_path(job_id)
        argv = [str(path), "--workspace", str(self.workspace), "--output-dir",
                str(self.outputs), "--debug-proof"]
        for flag, enabled in (("--override-low-fit", body.override_low_fit),
                              ("--strict-no", body.strict_no),
                              ("--analyze-gap", body.analyze_gap),
                              ("--no-cache", body.no_cache)):
            if enabled:
                argv.append(flag)

        def work(task: Task) -> tuple[int, str, dict[str, Any] | None]:
            if refresh_engine(task.emit):
                self._scores = None  # board scores came from the old code
            task.emit(f"$ python3 pipeline.py {self.display_path(path)} --debug-proof"
                      + "".join(f" {a}" for a in argv[6:]))
            code = pipeline.run(argv, which=self.which,
                                runner=LoggingRunner(task.emit, self.runner), out=task.emit)
            outcome = EXIT_MEANINGS.get(code, f"exit {code}")
            task.emit(f"pipeline.py exited {code} ({outcome})")
            return code, outcome, self.results(job_id)

        return self.start("tailor", f"Tailoring {path.name}", job_id, work)

    def fetch(self, body: FetchRequest) -> Task:
        if body.source not in FETCH_SOURCES:
            raise HTTPException(422, f"source must be one of {', '.join(FETCH_SOURCES)}")
        if body.source == "jobright" and not body.allow_apify_spend:
            raise HTTPException(422, "Jobright runs a billable Apify actor; set "
                                     "allow_apify_spend to confirm the spend")
        argv = ["--workspace", str(self.workspace),
                "--source", "all" if body.source == "free" else body.source]
        for flag, value in (("--keyword", body.query), ("--location", body.location),
                            ("--country", body.country)):
            if value and value.strip():
                argv += [flag, value.strip()]
        if body.limit is not None:
            argv += ["--limit", str(body.limit)]
        if body.allow_apify_spend and body.source != "free":
            argv.append("--allow-apify-spend")
        for flag, enabled in (("--dry-run", body.dry_run), ("--refresh", body.refresh)):
            if enabled:
                argv.append(flag)

        def work(task: Task) -> tuple[int, str, dict[str, Any] | None]:
            before = len(self.index()["jobs"])  # load before fetching, or new files count twice
            task.emit("$ python3 fetch_jobs.py " + " ".join(argv[2:]))
            code = fetch_jobs.run(argv, env=self.env, opener=LoggingOpener(task.emit, self.opener),
                                  sleep=self.sleep, out=task.emit)
            outcome = FETCH_EXIT_MEANINGS.get(code, f"exit {code}")
            task.emit(f"fetch_jobs.py exited {code} ({outcome})")
            result: dict[str, Any] = {"dry_run": body.dry_run}
            if not body.dry_run:
                task.emit("Refreshing the job index...")
                index = self.reindex()
                result["jobs"] = len(index["jobs"])
                result["new_jobs"] = len(index["jobs"]) - before
                task.emit(f"Index now holds {result['jobs']} jobs ({result['new_jobs']:+d}).")
                summary = _read_json(self.outputs / "fetch_summary.json")
                if summary:
                    result["totals"] = summary.get("totals")
            return code, outcome, result

        return self.start("fetch", f"Fetching jobs ({body.source})", None, work)

    # -- profile and template ------------------------------------------------
    def _source_path(self, kind: str) -> Path:
        return self.master_path() if kind == "profile" else self.template_path()

    def describe_source(self, kind: str, text: str, compare: str | None = None
                        ) -> dict[str, Any]:
        errors: list[str] = []
        limit = MAX_PROFILE_BYTES if kind == "profile" else MAX_TEMPLATE_BYTES
        if len(text.encode("utf-8")) > limit:
            errors.append(f"The file is larger than {limit // 1000} KB")
        if "\x00" in text:
            errors.append("The file contains NUL bytes")
        if errors:
            return {"ok": False, "errors": errors, "warnings": [], "summary": None}
        if kind == "profile":
            summary, errors, warnings = _profile_summary(text)
            if summary and compare is not None:
                before, _e, _w = _profile_summary(compare)
                if before:
                    summary["changes"] = {
                        "skills_added": sorted(set(summary["skills"]) - set(before["skills"])),
                        "skills_removed": sorted(set(before["skills"]) - set(summary["skills"])),
                        "newly_no": sorted(set(summary["no_skills"]) - set(before["no_skills"])),
                        "no_lifted": sorted(set(before["no_skills"]) - set(summary["no_skills"])),
                    }
        else:
            summary, errors, warnings = _template_summary(text)
        return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}

    def read_source(self, kind: str) -> dict[str, Any]:
        try:
            path = self._source_path(kind)
        except pipeline.PipelineError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, f"{path.name} not found")
        data = path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        return {"file": path.name, "text": text, "sha256": _sha(data),
                **self.describe_source(kind, text)}

    def save_source(self, kind: str, body: SourceUpdate) -> dict[str, Any]:
        with self.lock:
            if self.active is not None and self.active.status == "running":
                raise Busy(f"'{self.active.label}' is running; save after it finishes")
            current = self.read_source(kind)
            if body.base_sha256 and body.base_sha256 != current["sha256"]:
                raise HTTPException(409, f"{current['file']} changed on disk since you opened "
                                         f"it; reload before saving")
            text = body.text.replace("\r\n", "\n")
            check = self.describe_source(kind, text, current["text"])
            if not check["ok"]:
                raise HTTPException(422, "; ".join(check["errors"]))
            path = self._source_path(kind)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copyfile(path, self.backup_dir / f"{path.name}.{stamp}.bak")
            for old in sorted(self.backup_dir.glob(f"{path.name}.*.bak"))[:-BACKUPS_KEPT]:
                old.unlink()
            pipeline.atomic_write_text(path, text)
            self._scores = None
        saved = self.read_source(kind)
        saved["changes"] = (check["summary"] or {}).get("changes")
        return saved

    def meta(self, user: dict[str, Any]) -> dict[str, Any]:
        index = self.index()
        out = {"jobs": len(index["jobs"]), "index_generated": index.get("generated"),
               "threshold": pipeline.FIT_THRESHOLD, "levels": list(jobboard.LEVELS),
               "min_skill_requirements": jobboard.MIN_SKILL_REQUIREMENTS, "user": user}
        if not user.get("is_admin"):
            return out
        tools = {name: bool(self.which(name)) for name in ("pdflatex", "pdftotext", "pdfinfo")}
        try:
            master = self.master_path().name
        except pipeline.PipelineError as exc:
            master = None
            tools["master_error"] = str(exc)
        active = self.active
        return {**out, "workspace": self.workspace.name, "tools": tools, "master": master,
                "pending_requests": len(self.accounts.list_requests()),
                "template": self.template_path().name, "fetch_sources": list(FETCH_SOURCES),
                "active_task": active.view(False) if active else None}

    def public_stats(self) -> dict[str, Any]:
        """What the home page may show anyone: counts about the board, nothing personal."""
        jobs = self.index()["jobs"]
        return {"jobs": len(jobs), "companies": len({j["company"] for j in jobs if j["company"]}),
                "scorable": sum(1 for j in jobs if jobboard.has_enough_detail(j)),
                "threshold": pipeline.FIT_THRESHOLD}


def _profile_summary(text: str) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "master.md"
        path.write_text(text, encoding="utf-8")
        try:
            master = pipeline.parse_master(path)
        except pipeline.PipelineError as exc:
            return None, [str(exc)], []
    tags = collections.Counter(line.own_tag for line in master.lines if line.own_tag)
    positive = sum(1 for line in master.lines
                   if not line.meta and line.tag in pipeline.POSITIVE_TAGS)
    errors = [] if positive else ["No [V]/[R] or skills-table evidence lines remain; every "
                                  "job would score 0"]
    warnings = list(master.warnings)
    kit = None
    if not pipeline.resume_kit.has_kit([line.raw for line in master.lines]):
        warnings.append("No '## 9. Resume kit' section: tailoring refuses to run until one is "
                        "added")
    else:
        try:
            kit = pipeline.load_kit(master)
        except pipeline.PipelineError as exc:
            errors.append(str(exc))
    if kit is not None:
        audit = pipeline.audit_kit(kit, master)
        warnings += [f"Kit bullet {bid} will not be used: {problems[0]}"
                     for bid, problems in audit.blocked.items()]
        warnings += kit.warnings
    skills = sorted({pipeline.SKILL_INDEX[o.key].display for occs in master.occurrences.values()
                     for o in occs if o.kind == "positive" and o.key not in master.global_no})
    return ({"lines": len(master.lines), "evidence_lines": positive,
             "tags": {f"[{tag}]": tags.get(tag, 0) for tag in pipeline.KNOWN_TAGS},
             "entries": [{"id": e.id, "title": e.title, "kind": e.kind, "line": e.start_line}
                         for e in master.entries],
             "skills": skills,
             "no_skills": sorted(pipeline.SKILL_INDEX[k].display for k in master.global_no),
             "resume_kit": None if kit is None else {"bullets": len(kit.bullets),
                                                     "new_bullets": kit.new_bullets}},
            errors, warnings)


def _template_summary(text: str) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / pipeline.TEMPLATE_NAME
        path.write_text(text, encoding="utf-8")
        try:
            template = pipeline.parse_template(path)
        except pipeline.PipelineError as exc:
            return None, [str(exc)], []
    return ({"engine": template.engine_requirement, "engine_reason": template.engine_reason,
             "sections": [{"title": s.title, "kind": s.kind, "entries": len(s.entries),
                           "bullets": sum(len(e.bullets) for e in s.entries)}
                          for s in template.sections]}, [], [])


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "frame-src 'self'; object-src 'self'; connect-src 'self'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'self'")


PAGES = frozenset({"/", "/app", "/login", "/signup"})


def create_app(studio: Studio, static_dir: Path = STATIC_DIR,
               allowed_hosts: frozenset[str] = LOCAL_HOSTS) -> FastAPI:
    app = FastAPI(title="VouchStudio", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.studio = studio

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Requests must be addressed to localhost"}, 403)
        if request.method not in ("GET", "HEAD"):
            if request.headers.get(STUDIO_HEADER) != "1":
                return JSONResponse({"detail": "Missing X-Vouch-Studio header"}, 403)
            origin = request.headers.get("origin")
            if origin and (urllib.parse.urlsplit(origin).hostname or "") not in allowed_hosts:
                return JSONResponse({"detail": "Cross-origin requests are refused"}, 403)
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "Request body too large"}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        pdf = request.url.path.startswith("/api/files/") or request.url.path.endswith("/resume.pdf")
        if not pdf:
            response.headers["Content-Security-Policy"] = CSP  # PDF viewers need none
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path in PAGES or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # revalidate: a UI edit shows at once
        return response

    @app.exception_handler(Busy)
    async def busy(_request: Request, exc: Busy):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(pipeline.PipelineError)
    async def pipeline_error(_request: Request, exc: pipeline.PipelineError):
        return JSONResponse({"detail": str(exc)}, 500 if exc.exit_code == 5 else 422)

    @app.exception_handler(accounts.AccountError)
    async def account_error(_request: Request, exc: accounts.AccountError):
        return JSONResponse({"detail": str(exc)}, exc.status)

    # -- who is asking ---------------------------------------------------------
    def current_user(request: Request) -> dict[str, Any] | None:
        return studio.accounts.user_for_session(request.cookies.get(SESSION_COOKIE))

    def require_user(request: Request) -> dict[str, Any]:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "Log in to continue")
        return user

    def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if not user["is_admin"]:
            raise HTTPException(403, "Only the workspace owner can do this")
        return user

    admin_only = [Depends(require_admin)]

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def start_session(request: Request, response: Response, user: dict[str, Any]) -> None:
        token = studio.accounts.create_session(user["id"])
        response.set_cookie(SESSION_COOKIE, token, max_age=accounts.SESSION_SECONDS,
                            httponly=True, samesite="lax", path="/",
                            secure=request.url.scheme == "https")

    # -- accounts --------------------------------------------------------------
    @app.post("/api/auth/signup", status_code=201)
    def signup(body: SignupRequest, request: Request, response: Response):
        key = f"ip:{client_ip(request)}"
        studio.accounts.signup_throttle.check(key)
        studio.accounts.signup_throttle.fail(key)  # every sign-up counts toward the limit
        user = studio.accounts.create_user(body.email, body.password, body.name)
        start_session(request, response, user)
        return {"user": user}

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request, response: Response):
        keys = (f"ip:{client_ip(request)}", f"email:{accounts.normalize_email(body.email)}")
        studio.accounts.login_throttle.check(*keys)
        user = studio.accounts.authenticate(body.email, body.password)
        if user is None:
            studio.accounts.login_throttle.fail(*keys)
            raise HTTPException(401, "That email and password don't match an account.")
        studio.accounts.login_throttle.reset(keys[1])
        start_session(request, response, user)
        return {"user": user}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        studio.accounts.end_session(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict[str, Any] = Depends(require_user)):
        return {"user": user, "pro_request": studio.accounts.latest_request(user["id"])}

    @app.get("/api/me/resume-header")
    def my_header(user: dict[str, Any] = Depends(require_user)):
        return {"header": studio.accounts.header(user["id"]),
                "build": {k: v for k, v in studio.build_requirements(user).items()
                          if k not in ("text", "header")}}

    @app.post("/api/me/resume-header")
    def save_my_header(body: ResumeHeader, user: dict[str, Any] = Depends(require_user)):
        try:
            header = user_template.clean_header(body.model_dump())
        except user_template.TemplateInputError as exc:
            raise HTTPException(422, str(exc)) from None
        studio.accounts.save_header(user["id"], header)
        return my_header(user)

    @app.post("/api/me/pro-request")
    def request_pro(body: ProRequest, user: dict[str, Any] = Depends(require_user)):
        return {"pro_request": studio.accounts.request_pro(user["id"], body.note)}

    # -- Pro builds (the account's own profile, never the workspace files) ------------
    @app.get("/api/builds")
    def builds(user: dict[str, Any] = Depends(require_user)):
        return {"builds": [{k: v for k, v in b.items()} for b in
                           studio.accounts.builds(user["id"])]}

    @app.post("/api/builds/{job_id}")
    def build(job_id: str, body: BuildRequest | None = None,
              user: dict[str, Any] = Depends(require_user)):
        return {"build": studio.build_resume(user, job_id, (body or BuildRequest()).override_low_fit)}

    @app.get("/api/builds/{job_id}/resume.pdf")
    def build_pdf(job_id: str, user: dict[str, Any] = Depends(require_user)):
        found = studio.accounts.build(user["id"], job_id)
        if not found or found["pdf"] is None:
            raise HTTPException(404, "No verified resume for this job yet")
        name = f"{pipeline.slugify(found['company'] or 'resume')}_{pipeline.slugify(found['title'])}.pdf"
        return Response(found["pdf"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})

    @app.get("/api/builds/{job_id}/report")
    def build_report(job_id: str, user: dict[str, Any] = Depends(require_user)):
        found = studio.accounts.build(user["id"], job_id)
        if not found or not found["report"]:
            raise HTTPException(404, "No report for this job yet")
        return Response(found["report"], media_type="text/markdown; charset=utf-8")

    # -- admin console ----------------------------------------------------------------
    @app.get("/api/admin/overview", dependencies=admin_only)
    def admin_overview():
        return studio.admin_overview()

    @app.post("/api/admin/requests/{request_id}/{decision}")
    def decide(request_id: int, decision: str, admin: dict[str, Any] = Depends(require_admin)):
        if decision not in ("approve", "decline"):
            raise HTTPException(404, "Unknown decision")
        studio.accounts.decide_request(request_id, decision == "approve", admin["id"])
        return studio.admin_overview()

    @app.post("/api/admin/users/{user_id}/plan", dependencies=admin_only)
    def change_plan(user_id: int, body: PlanChange):
        studio.accounts.set_plan(user_id, body.plan)
        return studio.admin_overview()

    @app.get("/api/me/profile")
    def my_profile(user: dict[str, Any] = Depends(require_user)):
        return studio.my_profile(user)

    @app.post("/api/me/profile")
    def save_my_profile(body: ProfileUpload, user: dict[str, Any] = Depends(require_user)):
        return studio.save_my_profile(user, body)

    @app.post("/api/me/profile/delete")
    def delete_my_profile(user: dict[str, Any] = Depends(require_user)):
        return studio.delete_my_profile(user)

    @app.post("/api/me/delete")
    def delete_me(body: PasswordConfirm, response: Response,
                  user: dict[str, Any] = Depends(require_user)):
        if studio.accounts.authenticate(user["email"], body.password) is None:
            raise HTTPException(403, "That password is not correct.")
        studio.accounts.delete_user(user["id"])  # sessions and the stored profile go with it
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"deleted": True}

    @app.get("/api/stats")
    def stats():
        return studio.public_stats()

    # -- the board (any signed-in account) ---------------------------------------
    @app.get("/api/meta")
    def meta(user: dict[str, Any] = Depends(require_user)):
        return studio.meta(user)

    @app.get("/api/jobs")
    def jobs(q: str = Query("", max_length=200), region: str = "", level: str = "",
             source: str = "", verdict: str = "", tailored: bool = False,
             min_score: int | None = Query(None, ge=0, le=100), sort: str = "match",
             offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=PAGE_SIZE_MAX),
             user: dict[str, Any] = Depends(require_user)):
        return studio.list_jobs(user, q, region, level, source, verdict, tailored, min_score,
                                sort, offset, limit)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, user: dict[str, Any] = Depends(require_user)):
        return studio.job_detail(job_id, user)

    # -- the workspace (admin only: it runs on the owner's files) -----------------
    @app.post("/api/jobs/fetch", status_code=202, dependencies=admin_only)
    def fetch(body: FetchRequest):
        return studio.fetch(body).view(False)

    @app.post("/api/jobs/upload", status_code=201, dependencies=admin_only)
    def upload(body: UploadRequest):
        return studio.save_upload(body)

    @app.post("/api/tailor/{job_id}", status_code=202, dependencies=admin_only)
    def tailor(job_id: str, body: TailorRequest | None = None):
        return studio.tailor(job_id, body or TailorRequest()).view(False)

    @app.get("/api/tailor/{job_id}", dependencies=admin_only)
    def results(job_id: str):
        return studio.results(job_id)

    @app.get("/api/provenance/{job_id}", dependencies=admin_only)
    def provenance(job_id: str):
        return studio.provenance(job_id)

    @app.get("/api/fit/{job_id}", dependencies=admin_only)
    def fit(job_id: str):
        return studio.fit_report(job_id)

    @app.get("/api/briefing/{job_id}", dependencies=admin_only)
    def briefing(job_id: str):
        return studio.briefing(job_id)

    @app.get("/api/files/{job_id}/{name}", dependencies=admin_only)
    def files(job_id: str, name: str):
        path, media = studio.file_for(job_id, name)
        return FileResponse(path, media_type=media, filename=path.name,
                            content_disposition_type="inline")

    @app.get("/api/tasks", dependencies=admin_only)
    def tasks():
        return {"active": studio.active.view(False) if studio.active else None,
                "recent": [t.view(False) for t in sorted(studio.tasks.values(),
                                                         key=lambda t: -t.started)]}

    def get_task(task_id: str) -> Task:
        task = studio.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "Unknown task")
        return task

    @app.get("/api/tasks/{task_id}", dependencies=admin_only)
    def task_view(task_id: str):
        return get_task(task_id).view()

    @app.get("/api/tasks/{task_id}/events", dependencies=admin_only)
    def task_events(task_id: str):
        task = get_task(task_id)

        def stream() -> Iterator[str]:
            seen = 0
            yield "retry: 3000\n\n"
            while True:
                lines, done = task.wait(seen, SSE_KEEPALIVE_SECONDS)
                for line in lines:
                    yield f"event: log\ndata: {json.dumps(line)}\n\n"
                seen += len(lines)
                if done:  # finish() runs after the last emit, so nothing follows
                    yield f"event: done\ndata: {json.dumps(task.view(False))}\n\n"
                    return
                if not lines:
                    yield ": keepalive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    for kind in ("profile", "template"):
        def register(kind: str = kind) -> None:
            @app.get(f"/api/{kind}", name=f"read_{kind}", dependencies=admin_only)
            def read():
                return studio.read_source(kind)

            @app.post(f"/api/{kind}/validate", name=f"validate_{kind}", dependencies=admin_only)
            def validate(body: SourceCheck):
                current = studio.read_source(kind)["text"]
                return studio.describe_source(kind, body.text.replace("\r\n", "\n"), current)

            @app.post(f"/api/{kind}", name=f"save_{kind}", dependencies=admin_only)
            def save(body: SourceUpdate):
                return studio.save_source(kind, body)

        register()

    # -- pages -------------------------------------------------------------------
    def page(name: str) -> FileResponse:
        return FileResponse(static_dir / name, media_type="text/html")

    @app.get("/", include_in_schema=False)
    def home():
        return page("home.html")

    @app.get("/signup", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    def auth_page(request: Request):
        if current_user(request) is not None:
            return RedirectResponse("/app", status_code=303)
        return page("auth.html")

    @app.get("/app", include_in_schema=False)
    def application(request: Request):
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return page("index.html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(static_dir / "brand" / "favicon.svg", media_type="image/svg+xml")

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(prog="server.py", description="Vouch Studio web UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--open", action="store_true", help="open the browser when ready")
    parser.add_argument("--make-admin", metavar="EMAIL",
                        help="give an existing account the workspace tools, then exit")
    parser.add_argument("--process-builds", action="store_true",
                        help="run the Pro builds queued on the hosted app, then exit")
    args = parser.parse_args(argv)
    if args.process_builds:
        studio = Studio(args.workspace)
        print(f"Ran {studio.process_builds()} queued build(s).")
        return 0
    if args.make_admin:
        db = os.environ.get("DATABASE_URL") or Path(args.workspace).resolve() / ".studio" / "vouch.db"
        try:
            user = accounts.Accounts(db).set_admin(args.make_admin)
        except accounts.AccountError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{user['email']} is now the workspace admin (tailoring, fetching, editors).")
        return 0
    studio = Studio(args.workspace)
    print(f"Indexing jobs in {studio.target_dir} ...", flush=True)
    studio.index()
    url = f"http://{DEFAULT_HOST}:{args.port}"
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"VouchStudio on {url}  (Ctrl+C to stop)", flush=True)
    if not any(True for _ in studio.accounts._run("SELECT 1 FROM users WHERE is_admin = 1")):
        print("No admin account yet: sign up, then run ./run_ui.sh --make-admin YOUR_EMAIL "
              "to unlock tailoring and fetching.", flush=True)
    uvicorn.run(create_app(studio), host=DEFAULT_HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
