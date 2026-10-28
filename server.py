#!/usr/bin/env python3
"""Vouch Studio: a local web UI over pipeline.py, fetch_jobs.py and jobboard.py.

    ./run_ui.sh        # creates .venv, starts http://127.0.0.1:8000 and opens the browser

The engine stays on the standard library; FastAPI and uvicorn are used only here
(see requirements-ui.txt). Every route calls the engine's own functions in-process,
so scores, gates, provenance and briefings are exactly what the CLI produces:

    GET  /api/meta                      tools, profile, index and the running task
    GET  /api/jobs                      filtered, sorted, paginated jobs with fit scores
    GET  /api/resume                    which profile ranks the board (master or an upload)
    POST /api/resume[/clear]            rank against an uploaded resume (memory only), or stop
    GET  /api/jobs/{job_id}             one job: metadata, description, latest results
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
cross-site page cannot send without a CORS preflight this server never grants.
One task runs at a time, and the profile and template cannot be saved while it
runs: CLAUDE.md forbids editing them during a pipeline run.
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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import fetch_jobs
import jobboard
import pipeline

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
STUDIO_HEADER = "x-vouch-studio"
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


class ResumeUpload(BaseModel):
    filename: str = Field(max_length=200)
    content_base64: str = Field(max_length=MAX_BODY_BYTES)


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
                 backup_dir: Path | None = None) -> None:
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
        # Board scores per source ("master", "upload"): (key, rows, profile summary).
        self._scores: dict[str, tuple[tuple, dict[str, dict[str, Any]], dict[str, Any]]] | None \
            = None
        # A resume uploaded to rank the board: parsed in memory, never written or logged.
        self.uploaded: tuple[str, jobboard.Profile] | None = None

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

    def scores(self, use_upload: bool = True) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Board scores for the uploaded resume (when there is one and `use_upload`), else
        the master profile; cached per profile and index. Tailoring always uses the master."""
        index = self.index()
        uploaded = self.uploaded if use_upload else None
        if uploaded is not None:
            key: tuple = ("upload", uploaded[0], id(index), len(index["jobs"]))
            profile = uploaded[1]
        else:
            try:
                master = self.master_path()
                data = master.read_bytes()
            except (pipeline.PipelineError, OSError) as exc:
                return {}, {"error": str(exc), "source": "master"}
            key = ("master", _sha(data), id(index), len(index["jobs"]))
            profile = None
        source = key[0]
        with self.lock:
            cached = (self._scores or {}).get(source)
            if cached and cached[0] == key:
                return cached[1], cached[2]
        if profile is None:
            try:
                profile = jobboard.load_profile(master.name, data)
            except jobboard.ProfileError as exc:
                return {}, {"error": str(exc), "source": "master"}
        rows = {row["id"]: row for row in jobboard.score_jobs(profile, index["jobs"])}
        summary = {**profile.summary(), "source": "upload" if uploaded else "master"}
        with self.lock:
            self._scores = {**(self._scores or {}), source: (key, rows, summary)}
        return rows, summary

    def set_resume(self, body: ResumeUpload) -> dict[str, Any]:
        """Rank the board against an uploaded resume or master profile (memory only)."""
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, "The upload is not valid base64") from None
        try:
            profile = jobboard.load_profile(body.filename, data)
        except jobboard.ProfileError as exc:
            raise HTTPException(422, str(exc)) from None
        with self.lock:
            self.uploaded = (_sha(data), profile)
            self._scores = None
        return self.resume_state()

    def clear_resume(self) -> dict[str, Any]:
        with self.lock:
            self.uploaded = None
            self._scores = None
        return self.resume_state()

    def resume_state(self) -> dict[str, Any]:
        scores, profile = self.scores()
        ranked = [s for s in scores.values() if not s["insufficient"]]
        return {"profile": profile, "threshold": pipeline.FIT_THRESHOLD,
                "scored": len(ranked),
                "proceed": sum(1 for s in ranked if s["score"] >= pipeline.FIT_THRESHOLD),
                "thin": sum(1 for s in scores.values() if s["insufficient"])}

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
    def list_jobs(self, q: str = "", region: str = "", level: str = "", source: str = "",
                  verdict: str = "", tailored: bool = False, min_score: int | None = None,
                  sort: str = "match", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        index = self.index()
        scores, profile = self.scores()
        outputs = self.outputs_by_job()
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
        elif sort == "tailored":
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
                "limit": limit, "jobs": rows[offset:offset + limit],
                "facets": {k: dict(sorted(v.items())) for k, v in facets.items()},
                "profile": profile, "index_generated": index.get("generated")}

    def job_detail(self, job_id: str) -> dict[str, Any]:
        path = self.job_path(job_id)
        job = next((j for j in self.index()["jobs"] if j["id"] == job_id), None)
        view = jobboard.public_job(job) if job else {"id": job_id, "title": job_id}
        scores, _profile = self.scores(use_upload=False)  # the skill gate tailoring uses
        view["board_score"] = scores.get(job_id)
        view["file"] = path.name
        view["text"] = path.read_text(encoding="utf-8", errors="replace")
        view["results"] = self.results(job_id)
        active = self.active
        view["active_task"] = active.view(False) if active and active.job_id == job_id else None
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

    def meta(self) -> dict[str, Any]:
        index = self.index()
        tools = {name: bool(self.which(name)) for name in ("pdflatex", "pdftotext", "pdfinfo")}
        try:
            master = self.master_path().name
        except pipeline.PipelineError as exc:
            master = None
            tools["master_error"] = str(exc)
        active = self.active
        return {"workspace": self.workspace.name, "jobs": len(index["jobs"]),
                "index_generated": index.get("generated"), "tools": tools,
                "master": master, "template": self.template_path().name,
                "threshold": pipeline.FIT_THRESHOLD, "fetch_sources": list(FETCH_SOURCES),
                "levels": list(jobboard.LEVELS),
                "min_skill_requirements": jobboard.MIN_SKILL_REQUIREMENTS,
                "active_task": active.view(False) if active else None}


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


def create_app(studio: Studio, static_dir: Path = STATIC_DIR,
               allowed_hosts: frozenset[str] = LOCAL_HOSTS) -> FastAPI:
    app = FastAPI(title="Vouch Studio", docs_url=None, redoc_url=None, openapi_url=None)
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
        if not request.url.path.startswith("/api/files/"):
            response.headers["Content-Security-Policy"] = CSP  # PDF viewers need none
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # revalidate: a UI edit shows at once
        return response

    @app.exception_handler(Busy)
    async def busy(_request: Request, exc: Busy):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(pipeline.PipelineError)
    async def pipeline_error(_request: Request, exc: pipeline.PipelineError):
        return JSONResponse({"detail": str(exc)}, 500 if exc.exit_code == 5 else 422)

    @app.get("/api/meta")
    def meta():
        return studio.meta()

    @app.get("/api/jobs")
    def jobs(q: str = Query("", max_length=200), region: str = "", level: str = "",
             source: str = "", verdict: str = "", tailored: bool = False,
             min_score: int | None = Query(None, ge=0, le=100), sort: str = "match",
             offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=PAGE_SIZE_MAX)):
        return studio.list_jobs(q, region, level, source, verdict, tailored, min_score, sort,
                                offset, limit)

    @app.get("/api/resume")
    def resume():
        return studio.resume_state()

    @app.post("/api/resume")
    def set_resume(body: ResumeUpload):
        return studio.set_resume(body)

    @app.post("/api/resume/clear")
    def clear_resume():
        return studio.clear_resume()

    @app.post("/api/jobs/fetch", status_code=202)
    def fetch(body: FetchRequest):
        return studio.fetch(body).view(False)

    @app.post("/api/jobs/upload", status_code=201)
    def upload(body: UploadRequest):
        return studio.save_upload(body)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return studio.job_detail(job_id)

    @app.post("/api/tailor/{job_id}", status_code=202)
    def tailor(job_id: str, body: TailorRequest | None = None):
        return studio.tailor(job_id, body or TailorRequest()).view(False)

    @app.get("/api/tailor/{job_id}")
    def results(job_id: str):
        return studio.results(job_id)

    @app.get("/api/provenance/{job_id}")
    def provenance(job_id: str):
        return studio.provenance(job_id)

    @app.get("/api/fit/{job_id}")
    def fit(job_id: str):
        return studio.fit_report(job_id)

    @app.get("/api/briefing/{job_id}")
    def briefing(job_id: str):
        return studio.briefing(job_id)

    @app.get("/api/files/{job_id}/{name}")
    def files(job_id: str, name: str):
        path, media = studio.file_for(job_id, name)
        return FileResponse(path, media_type=media, filename=path.name,
                            content_disposition_type="inline")

    @app.get("/api/tasks")
    def tasks():
        return {"active": studio.active.view(False) if studio.active else None,
                "recent": [t.view(False) for t in sorted(studio.tasks.values(),
                                                         key=lambda t: -t.started)]}

    def get_task(task_id: str) -> Task:
        task = studio.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "Unknown task")
        return task

    @app.get("/api/tasks/{task_id}")
    def task_view(task_id: str):
        return get_task(task_id).view()

    @app.get("/api/tasks/{task_id}/events")
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
            @app.get(f"/api/{kind}", name=f"read_{kind}")
            def read():
                return studio.read_source(kind)

            @app.post(f"/api/{kind}/validate", name=f"validate_{kind}")
            def validate(body: SourceCheck):
                current = studio.read_source(kind)["text"]
                return studio.describe_source(kind, body.text.replace("\r\n", "\n"), current)

            @app.post(f"/api/{kind}", name=f"save_{kind}")
            def save(body: SourceUpdate):
                return studio.save_source(kind, body)

        register()

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static_dir / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(prog="server.py", description="Vouch Studio web UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--open", action="store_true", help="open the browser when ready")
    args = parser.parse_args(argv)
    studio = Studio(args.workspace)
    print(f"Indexing jobs in {studio.target_dir} ...", flush=True)
    studio.index()
    url = f"http://{DEFAULT_HOST}:{args.port}"
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Vouch Studio on {url}  (Ctrl+C to stop)", flush=True)
    uvicorn.run(create_app(studio), host=DEFAULT_HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
