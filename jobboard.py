#!/usr/bin/env python3
"""Shared job board core: job index, uploaded-profile parsing and scoring.

No HTTP code lives here, so a future hosted back end can import it unchanged.

    python3 jobboard.py index      # (re)build outputs/job_index.json from target_jobs/

Index: each job's requirements are extracted once with pipeline.extract_requirements.
They depend only on the job description, never on whose resume is scored.
Scoring: an uploaded profile is parsed into the same evidence model the resume
pipeline uses, then every indexed requirement is matched against it.

Profiles come in two modes:
  * master - a tagged master profile ([V]/[R]/[NO]); the strict evidence rules apply.
  * resume - an ordinary resume (.md, .txt or PDF text). Every line is treated as
             the person's own claim, so scores are less strict and labelled that way.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import dashboard
import fetch_jobs
import pipeline

INDEX_VERSION = 5  # 2: either-or groups, pay text, company name; 3: group overlap;
                   # 4: link targets, "excel" verb, "good fit" headings; 5: "what we look for"
INDEX_NAME = "job_index.json"
MAX_UPLOAD_BYTES = 2_000_000
PROFILE_EXTENSIONS = (".md", ".txt", ".pdf")
MASTER_TAG_MINIMUM = 3          # tagged lines needed to treat an upload as a master file
PDFTOTEXT_TIMEOUT = 30
# Ranking only (the fit score itself is pipeline.compute_score, unchanged): adding this
# much unmatched weight pulls scores built on one or two requirements down, so
# "1 of 1 matched" never outranks "8 of 10 matched".
RANK_PRIOR_WEIGHT = 6
# A job needs at least this many recognized skill requirements before the board shows a
# score. Postings the vocabulary barely understands (often sales, finance or operations)
# otherwise score 100 on a single match such as "degree".
MIN_SKILL_REQUIREMENTS = 3
SKILL_REQUIREMENT_KINDS = frozenset({"skill", "skill_category", "skill_group", "language"})

# Checked in order; the first match wins. Early-career words come first so that
# "Product Manager Intern" is an internship, not management. A trailing level number
# ("Software Engineer I") only means junior when no seniority word is present, so
# "Principal Software Engineer I" stays principal.
LEVEL_RULES = (
    ("Intern", re.compile(r"\b(intern|internship|co-?op|apprentice(ship)?|stagiaire)\b", re.I)),
    ("New grad", re.compile(r"\b(new[- ]?grad(uate)?s?|recent grad(uate)?s?|graduate|"
                            r"early[- ]career|university grad(uate)?|campus|class of 20\d\d)\b",
                            re.I)),
    ("Junior", re.compile(r"\b(junior|jr\.?|entry[- ]level)\b", re.I)),
    ("Director and above", re.compile(r"\b(director|vp|svp|evp|vice president|head of|chief|"
                                      r"cto|cio|ciso)\b", re.I)),
    ("Manager", re.compile(r"\bmanager\b", re.I)),
    ("Staff and principal", re.compile(r"\b(staff|principal|distinguished|architect|lead)\b",
                                       re.I)),
    ("Senior", re.compile(r"\b(senior|sr\.?)(?![a-z])", re.I)),
    ("Junior", re.compile(r"\b(engineer|developer|analyst|sde|swe|scientist)\s+(i|1)\b(?![\w-])",
                          re.I)),
)
MID_LEVEL = "Mid or unspecified"
LEVELS = ("Intern", "New grad", "Junior", MID_LEVEL, "Senior", "Staff and principal", "Manager",
          "Director and above")
RESUME_SECTIONS = re.compile(
    r"^(summary|profile|about( me)?|objective|experience|work experience|professional "
    r"experience|employment( history)?|work history|projects?|personal projects|education|"
    r"skills|technical skills|core skills|certifications?|awards?|honou?rs|publications|"
    r"leadership|volunteer(ing)?|activities|languages|interests|research)\s*:?$", re.I)


class ProfileError(Exception):
    """The uploaded profile cannot be read; the message is safe to show users."""


def level_of(title: str) -> str:
    """Seniority from the job title alone; titles that say nothing are mid/unspecified."""
    for level, pattern in LEVEL_RULES:
        if pattern.search(title or ""):
            return level
    return MID_LEVEL


# ---------------------------------------------------------------------------
# Job index
# ---------------------------------------------------------------------------
def index_path(workspace: Path) -> Path:
    return workspace / "outputs" / INDEX_NAME


def _requirement_dict(req: pipeline.Requirement) -> dict[str, Any]:
    item = {"name": req.name, "key": req.key, "kind": req.kind, "category": req.category}
    if req.members:
        item["members"] = list(req.members)
    if req.kind == "education":
        item["jd_excerpt"] = req.jd_excerpt  # degree rules read the JD wording
    return item


def index_job(path: Path, workspace: Path, text: str) -> dict[str, Any]:
    meta = dashboard.read_metadata(text)
    first = text.splitlines()[0].lstrip("# ").strip() if text.strip() else path.stem
    completeness = meta.get("description completeness", "Full")
    url = meta.get("url")
    job: dict[str, Any] = {
        "id": path.stem,
        "file": path.name,
        "title": meta.get("role") or first,
        "company": meta.get("company") or "",
        "source": meta.get("source") or "Manual",
        "location": meta.get("location") or "Not specified",
        "region": dashboard.region_of(meta.get("location") or ""),
        "url": url if fetch_jobs.is_http_url(url) else None,
        "posted": meta.get("posted") or meta.get("updated"),
        "complete": not completeness.lower().startswith("partial"),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "requirements": [],
        "domain": None,
        "error": None,
    }
    job["level"] = level_of(job["title"])
    if job["complete"]:
        try:
            jd = pipeline.read_job_description(path, workspace)
        except pipeline.PipelineError as exc:
            job["error"] = str(exc)
            return job
        requirements, _unscored, _not_required, _warnings = pipeline.extract_requirements(jd)
        job["company"] = job["company"] or jd.company
        job["requirements"] = [_requirement_dict(r) for r in requirements]
        job["domain"] = pipeline.classify_domain(jd, requirements)["primary"]
    return job


def build_index(workspace: Path, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Index every job file; unchanged files reuse their previous entry."""
    reuse: dict[str, dict[str, Any]] = {}
    if previous and previous.get("version") == INDEX_VERSION:
        reuse = {job["file"]: job for job in previous.get("jobs", [])}
    jobs = []
    for path in sorted((workspace / "target_jobs").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        cached = reuse.get(path.name)
        if cached and cached.get("sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest():
            cached["level"] = level_of(cached["title"])  # cheap; follows LEVEL_RULES changes
            jobs.append(cached)
        else:
            jobs.append(index_job(path, workspace, text))
    return {"version": INDEX_VERSION,
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "jobs": jobs}


def load_or_build_index(workspace: Path, rebuild: bool = False) -> dict[str, Any]:
    path = index_path(workspace)
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    index = build_index(workspace, None if rebuild else previous)
    if previous and index["jobs"] == previous.get("jobs"):
        return previous  # unchanged: keep the original build time
    if index["jobs"] != (previous or {}).get("jobs"):
        path.parent.mkdir(parents=True, exist_ok=True)
        fetch_jobs.atomic_write(path, json.dumps(index, ensure_ascii=False) + "\n")
    return index


def skill_requirement_count(job: dict[str, Any]) -> int:
    return sum(1 for r in job.get("requirements") or [] if r["kind"] in SKILL_REQUIREMENT_KINDS)


def has_enough_detail(job: dict[str, Any]) -> bool:
    return skill_requirement_count(job) >= MIN_SKILL_REQUIREMENTS


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    """What the board shows; requirements stay on the server."""
    view = {k: job[k] for k in ("id", "title", "company", "source", "location", "region",
                                "url", "posted", "complete", "level", "domain")}
    view["skill_requirements"] = skill_requirement_count(job)
    view["enough_detail"] = has_enough_detail(job)
    return view


# ---------------------------------------------------------------------------
# Uploaded profiles
# ---------------------------------------------------------------------------
@dataclass
class Profile:
    mode: str                         # master | resume
    filename: str
    master: pipeline.MasterProfile
    evidence_lines: int
    skills: list[str] = field(default_factory=list)
    no_skills: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"mode": self.mode, "filename": self.filename,
                "evidence_lines": self.evidence_lines, "skills": self.skills,
                "no_skills": self.no_skills, "warnings": self.warnings}


def pdf_to_text(data: bytes) -> str:
    tool = shutil.which("pdftotext")
    if not tool:
        raise ProfileError("PDF uploads need pdftotext (poppler) on the server; upload a .md "
                           "or .txt version instead")
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "upload.pdf"
        pdf.write_bytes(data)
        try:
            result = subprocess.run([tool, "-enc", "UTF-8", "-layout", str(pdf), "-"],
                                    capture_output=True, text=True, timeout=PDFTOTEXT_TIMEOUT,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProfileError("The PDF could not be read") from exc
    if result.returncode != 0:
        raise ProfileError("The PDF could not be read (it may be encrypted or damaged)")
    return result.stdout.replace("\f", "\n")


def decode_upload(filename: str, data: bytes) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ProfileError(f"The file is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in PROFILE_EXTENSIONS:
        raise ProfileError("Upload a .md, .txt or .pdf file")
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ProfileError("That file is not a PDF")
        text = pdf_to_text(data)
    else:
        if b"\x00" in data:
            raise ProfileError("That file is not plain text")
        text = data.decode("utf-8-sig", errors="replace")
    text = pipeline.normalize_text(text)
    if len(re.findall(r"[A-Za-z]{3,}", text)) < 20:
        raise ProfileError("The file has too little readable text to score (for PDFs, this "
                           "usually means a scanned image without text)")
    return text


def is_master(text: str) -> bool:
    return len(pipeline.TAG_PATTERN.findall(text)) >= MASTER_TAG_MINIMUM


def resume_to_master(text: str) -> str:
    """Rewrite a plain resume into the master layout, every content line a claim."""
    out = ["# Uploaded resume", "", "## Profile", ""]
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            out.append("")
            continue
        markdown = re.match(r"^#{1,6}\s+(.*)$", line)
        letters = re.sub(r"[^A-Za-z]", "", line)
        heading = None
        if markdown:
            heading = markdown.group(1).strip()
        elif RESUME_SECTIONS.match(line.strip("*_ ")):
            heading = line.strip("*_ :")
        elif letters and letters.isupper() and len(line.split()) <= 4 and len(letters) >= 4:
            heading = line.title()
        if heading:
            out += ["", f"## {pipeline.collapse_ws(heading)}", ""]
            continue
        body = re.sub(r"^([-*+•●▪‣⁃]|\d+[.)])\s*", "", line)
        body = pipeline.TAG_PATTERN.sub("", body).strip()
        if body:
            out.append(f"- `[V]` {body}")
    return "\n".join(out) + "\n"


def load_profile(filename: str, data: bytes) -> Profile:
    text = decode_upload(filename, data)
    mode = "master" if is_master(text) else "resume"
    source = text if mode == "master" else resume_to_master(text)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "profile.md"
        path.write_text(source, encoding="utf-8")
        try:
            master = pipeline.parse_master(path)
        except pipeline.PipelineError as exc:
            raise ProfileError(f"The profile could not be parsed: {exc}") from exc
    positive = sorted({o.key for occs in master.occurrences.values() for o in occs
                       if o.kind == "positive"})
    evidence = sum(1 for line in master.lines if not line.meta
                   and line.tag in pipeline.POSITIVE_TAGS)
    if evidence == 0:
        raise ProfileError("No evidence lines were found in the profile")
    warnings = []
    if mode == "resume":
        warnings.append("Scored as a plain resume: every line counts as your own claim. A "
                        "tagged master profile ([V]/[NO]) gives stricter, more reliable scores.")
    return Profile(mode=mode, filename=Path(filename).name, master=master,
                   evidence_lines=evidence,
                   skills=[pipeline.SKILL_INDEX[k].display for k in positive
                           if k not in master.global_no],
                   no_skills=[pipeline.SKILL_INDEX[k].display for k in master.global_no],
                   warnings=warnings)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _signature(req: dict[str, Any]) -> tuple:
    return (req["kind"], req["key"], req["name"], req.get("jd_excerpt", ""),
            tuple(req.get("members", ())))


def score_jobs(profile: Profile, jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fit score for every job with enough detail; matches are memoized per profile.

    Jobs with requirements but fewer than MIN_SKILL_REQUIREMENTS recognized skills are
    returned with ``insufficient: True`` and no score, rank or verdict.
    """
    memo: dict[tuple, str] = {}
    results = []
    thin = []
    for job in jobs:
        if not job.get("requirements"):
            continue
        if not has_enough_detail(job):
            thin.append({"id": job["id"], "score": None, "verdict": None, "rank": None,
                         "insufficient": True, "requirements": len(job["requirements"]),
                         "skill_requirements": skill_requirement_count(job),
                         "recognized": [r["name"] for r in job["requirements"]
                                        if r["kind"] in SKILL_REQUIREMENT_KINDS]})
            continue
        reqs = []
        for item in job["requirements"]:
            sig = _signature(item)
            if sig not in memo:
                probe = pipeline.Requirement(item["name"], item["key"], item["kind"], "required",
                                             [0], item.get("jd_excerpt", ""), "",
                                             members=list(item.get("members", [])))
                pipeline.match_requirement(probe, profile.master)
                memo[sig] = probe.status
            req = pipeline.Requirement(item["name"], item["key"], item["kind"], item["category"],
                                       [0], "", "")
            req.status = memo[sig]
            reqs.append(req)
        score, breakdown = pipeline.compute_score(reqs)
        matched_w, total_w = breakdown["matched_weight"], breakdown["total_weight"]
        rank = (200 * matched_w + total_w + RANK_PRIOR_WEIGHT) // (
            2 * (total_w + RANK_PRIOR_WEIGHT))
        matched = [r.name for r in reqs if r.status == pipeline.STATUS_MATCHED]
        gaps = [[r.name, r.status] for r in reqs if r.status != pipeline.STATUS_MATCHED]
        results.append({"id": job["id"], "score": score, "verdict": pipeline.verdict_for(score),
                        "rank": rank, "insufficient": False, "matched": matched, "gaps": gaps,
                        "requirements": len(reqs),
                        "skill_requirements": skill_requirement_count(job)})
    results.sort(key=lambda r: (-r["rank"], -r["score"], r["id"]))
    thin.sort(key=lambda r: r["id"])
    return results + thin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobboard.py", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["index"])
    parser.add_argument("--rebuild", action="store_true", help="ignore the cached index")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    index = load_or_build_index(args.workspace.resolve(), rebuild=args.rebuild)
    scored = sum(1 for job in index["jobs"] if job["requirements"])
    print(f"Indexed {len(index['jobs'])} jobs ({scored} scorable) -> "
          f"{index_path(args.workspace.resolve())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
