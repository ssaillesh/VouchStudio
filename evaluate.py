#!/usr/bin/env python3
"""Verify and measure the job board's scoring.

    python3 evaluate.py checks [--resume FILE]
        Automatic sensitivity checks on the live board: the score must follow the
        uploaded resume, move only for jobs that ask for a changed skill, and match
        pipeline.py. Exit 1 if any check fails.

    python3 evaluate.py sheet --resume FILE [--jobs 30] [--seed 7]
        Write a labelling sheet (outputs/evaluation/<name>/) with a stratified sample
        of jobs, their extracted requirements and the engine's calls, for you to mark.

    python3 evaluate.py report outputs/evaluation/<name>
        Read your labels and report extraction precision/recall, matching accuracy and
        how well the scores agree with your own fit ratings.

Everything is local and deterministic; the resume text is never written to the sheet.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import jobboard
import pipeline

# Probe sentences each mention exactly one vocabulary skill (asserted at run time).
ADD_PROBES = (
    ("ansible", "Automated server configuration using Ansible playbooks."),
    ("terraform", "Managed cloud resources with Terraform modules."),
    ("react", "Built web interfaces with React."),
    ("kafka", "Processed event streams with Apache Kafka."),
    ("go", "Wrote services in Golang."),
    ("tableau", "Published Tableau workbooks for finance teams."),
)
IRRELEVANT_LINE = "Enjoys hiking, chess and cooking."
DEFAULT_RESUME = """Jordan Okafor
Seattle, WA

EXPERIENCE
Platform Engineer, Harbor Labs
- Ran Kubernetes clusters with Docker and Helm
- Wrote Python services backed by PostgreSQL
- Built CI/CD pipelines in GitHub Actions

EDUCATION
BSc Computer Science, University of Washington

SKILLS
Python, Docker, Kubernetes, PostgreSQL, Linux, Bash
"""
CONTRAST_RESUMES = {
    "tech": """Maya Chen\nToronto, ON\n\nEXPERIENCE\nData Analyst, Lakeshore Retail\n"""
            """- Built Power BI dashboards on SQL Server and Excel\n"""
            """- Wrote SQL and pandas reports for finance\n\nSKILLS\n"""
            """SQL, Excel, Power BI, Tableau, pandas, Statistics\n""",
    "other": DEFAULT_RESUME,
}
SCORE_BANDS = ((0, 25), (25, 50), (50, 65), (65, 101))
CONTRAST_MIN_RANKED = 40   # a top-20 comparison needs a board larger than the top 20
YES = {"yes", "y", "true", "1", "correct"}
NO = {"no", "n", "false", "0", "incorrect", "wrong"}
STATUS_WORD = {"matched": "matched", "not_evidenced": "missing", "explicitly_no": "no",
               "ambiguous": "unclear"}


@dataclass
class Check:
    name: str
    status: str          # PASS | FAIL | SKIP
    detail: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def score_map(text: str, filename: str, jobs: list[dict[str, Any]]) -> tuple[jobboard.Profile,
                                                                               dict[str, Any]]:
    profile = jobboard.load_profile(filename, text.encode("utf-8"))
    return profile, {s["id"]: s for s in jobboard.score_jobs(profile, jobs)}


def ranked_scores(scores: dict[str, Any]) -> dict[str, int]:
    return {k: v["score"] for k, v in scores.items() if not v["insufficient"]}


def requires(job: dict[str, Any], key: str, direct_only: bool = False) -> bool:
    for req in job["requirements"]:
        if key in req.get("members", ()):
            return True  # either-or group that names the skill
        if not req["key"]:
            continue
        if req["key"] == key:
            return True
        if not direct_only and key in pipeline.SKILL_INDEX[req["key"]].members:
            return True
    return False


def add_line(text: str, sentence: str, mode: str) -> str:
    if mode == "master":
        return text.rstrip("\n") + f"\n\n## Evaluation probe\n\n- `[V]` {sentence}\n"
    return text.rstrip("\n") + f"\n- {sentence}\n"


def remove_skill(text: str, key: str) -> str:
    """Delete every mention of one skill, leaving the rest of each line intact."""
    out = []
    for line in text.split("\n"):
        mentions = [m for m in pipeline.SKILL_FINDER.find(line) if m.key == key]
        for mention in sorted(mentions, key=lambda m: -m.start):
            line = line[:mention.start] + line[mention.end:]
        out.append(line)
    return "\n".join(out)


def swap_two_bullets(text: str, mode: str) -> str | None:
    lines = text.split("\n")
    pattern = re.compile(r"^- `\[(V|R)\]` " if mode == "master" else r"^\s*([-*•]|\d+[.)])\s+")
    for i in range(len(lines) - 1):
        a, b = lines[i], lines[i + 1]
        if pattern.match(a) and pattern.match(b) and \
                len(a) - len(a.lstrip()) == len(b) - len(b.lstrip()):
            lines[i], lines[i + 1] = b, a
            return "\n".join(lines)
    return None


def diff(before: dict[str, int], after: dict[str, int]) -> tuple[set[str], set[str]]:
    up = {k for k in before if after.get(k, before[k]) > before[k]}
    down = {k for k in before if after.get(k, before[k]) < before[k]}
    return up, down


# ---------------------------------------------------------------------------
# Automatic checks
# ---------------------------------------------------------------------------
def run_checks(workspace: Path, text: str, filename: str,
               index: dict[str, Any] | None = None) -> list[Check]:
    index = index or jobboard.load_or_build_index(workspace)
    jobs = index["jobs"]
    byid = {j["id"]: j for j in jobs}
    sufficient = {j["id"] for j in jobs if jobboard.has_enough_detail(j)}
    profile, base_all = score_map(text, filename, jobs)
    base = ranked_scores(base_all)
    mode = profile.mode
    checks: list[Check] = []

    _, again = score_map(text, filename, jobs)
    checks.append(Check("Same resume scored twice", "PASS" if again == base_all else "FAIL",
                        f"{len(base)} ranked jobs"))

    _, extra = score_map(add_line(text, IRRELEVANT_LINE, mode), filename, jobs)
    up, down = diff(base, ranked_scores(extra))
    checks.append(Check("Unrelated line changes nothing", "PASS" if not up and not down else "FAIL",
                        f"raised {len(up)}, lowered {len(down)}"))

    swapped = swap_two_bullets(text, mode)
    if swapped is None:
        checks.append(Check("Reordering bullets changes nothing", "SKIP", "no adjacent bullets"))
    else:
        _, moved = score_map(swapped, filename, jobs)
        up, down = diff(base, ranked_scores(moved))
        checks.append(Check("Reordering bullets changes nothing",
                            "PASS" if not up and not down else "FAIL",
                            f"raised {len(up)}, lowered {len(down)}"))

    have = {k for k, occs in profile.master.occurrences.items()
            if any(o.kind in ("positive", "no") for o in occs)}
    probes = [(k, s) for k, s in ADD_PROBES if k in pipeline.SKILL_INDEX and k not in have][:3]
    for key, sentence in probes:
        found = {m.key for m in pipeline.SKILL_FINDER.find(sentence)}
        if found != {key}:
            checks.append(Check(f"Adding {pipeline.SKILL_INDEX[key].display}", "FAIL",
                                f"probe sentence mentions {sorted(found)}"))
            continue
        _, added = score_map(add_line(text, sentence, mode), filename, jobs)
        up, down = diff(base, ranked_scores(added))
        allowed = {i for i in base if requires(byid[i], key)}
        display = pipeline.SKILL_INDEX[key].display
        # Jobs that ask for the skill by name and counted it as a gap must all go up.
        must = {i for i in base if requires(byid[i], key, direct_only=True)
                and any(name == display for name, _status in base_all[i]["gaps"])}
        ok = not down and up <= allowed and must <= up
        checks.append(Check(
            f"Adding {pipeline.SKILL_INDEX[key].display} raises only jobs asking for it",
            "PASS" if ok else "FAIL",
            f"raised {len(up)} (jobs asking for it: {len(allowed)}, raised elsewhere: "
            f"{len(up - allowed)}, expected but not raised: {len(must - up)}), lowered {len(down)}"))

    demand = sorted(((sum(1 for i in base if requires(byid[i], k, direct_only=True)), k)
                     for k in profile.master.occurrences
                     if any(o.kind == "positive" for o in profile.master.occurrences[k])
                     and pipeline.SKILL_INDEX[k].kind == "skill"), reverse=True)
    if not demand or demand[0][0] == 0:
        checks.append(Check("Removing a skill lowers only jobs asking for it", "SKIP",
                            "no skill in the resume is requested by a ranked job"))
    else:
        key = demand[0][1]
        _, removed = score_map(remove_skill(text, key), filename, jobs)
        after = ranked_scores(removed)
        up, down = diff(base, after)
        allowed = {i for i in base if requires(byid[i], key)}
        ok = not up and down <= allowed and bool(down)
        checks.append(Check(
            f"Removing {pipeline.SKILL_INDEX[key].display} lowers only jobs asking for it",
            "PASS" if ok else "FAIL",
            f"lowered {len(down)} (jobs asking for it: {len(allowed)}, lowered elsewhere: "
            f"{len(down - allowed)}), raised {len(up)}"))

    tech = sum(1 for k in profile.master.occurrences
               if pipeline.SKILL_INDEX[k].group in ("cloud_devops", "backend", "os"))
    contrast_text = CONTRAST_RESUMES["tech" if tech >= 3 else "other"]
    _, contrast = score_map(contrast_text, "contrast.txt", jobs)
    top = lambda s: {i for i, _ in sorted(((k, v) for k, v in s.items() if not v["insufficient"]),
                                          key=lambda kv: (-kv[1]["rank"], kv[0]))[:20]}
    a, b = top(base_all), top(contrast)
    overlap = len(a & b) / max(1, len(a | b))
    if len(base) < CONTRAST_MIN_RANKED:
        checks.append(Check("A very different resume ranks jobs differently", "SKIP",
                            f"only {len(base)} ranked jobs; needs {CONTRAST_MIN_RANKED}"))
    else:
        checks.append(Check("A very different resume ranks jobs differently",
                            "PASS" if overlap < 0.5 else "FAIL",
                            f"top-20 overlap {overlap:.0%}"))

    bad_thin = [i for i, s in base_all.items() if s["insufficient"] and s["score"] is not None]
    bad_ranked = [i for i, s in base_all.items() if not s["insufficient"] and i not in sufficient]
    checks.append(Check(f"Jobs under {jobboard.MIN_SKILL_REQUIREMENTS} recognized skills get no "
                        f"score", "PASS" if not bad_thin and not bad_ranked else "FAIL",
                        f"{sum(1 for s in base_all.values() if s['insufficient'])} held back"))

    sample = sorted(base)[:: max(1, len(base) // 12)][:12]
    mismatches = []
    for job_id in sample:
        job = byid[job_id]
        jd = pipeline.read_job_description(workspace / "target_jobs" / job["file"], workspace)
        if pipeline.analyze(jd, profile.master).score != base[job_id]:
            mismatches.append(job_id)
    checks.append(Check("Board score equals pipeline.py's score", "PASS" if not mismatches
                        else "FAIL", f"{len(sample) - len(mismatches)} of {len(sample)} agree"))
    return checks


# ---------------------------------------------------------------------------
# Labelling sheet
# ---------------------------------------------------------------------------
def sample_jobs(scores: dict[str, Any], per_band: int, seed: int) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    picked: list[tuple[str, str]] = []
    for low, high in SCORE_BANDS:
        ids = sorted(i for i, s in scores.items()
                     if not s["insufficient"] and low <= s["score"] < high)
        picked += [(i, f"score {low}-{min(high, 100)}") for i in rng.sample(ids, min(per_band,
                                                                                      len(ids)))]
    thin = sorted(i for i, s in scores.items() if s["insufficient"])
    picked += [(i, "not enough detail") for i in rng.sample(thin, min(per_band, len(thin)))]
    return picked


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def band_of(score: dict[str, Any]) -> str:
    if score["insufficient"]:
        return "not enough detail"
    for low, high in SCORE_BANDS:
        if low <= score["score"] < high:
            return f"score {low}-{min(high, 100)}"
    return "score 65-100"


def make_sheet(workspace: Path, resume_path: Path, jobs_wanted: int = 30, seed: int = 7,
               out_root: Path | None = None, job_ids: list[str] | None = None,
               name: str | None = None, carry_from: Path | None = None) -> Path:
    """Write a sheet for a stratified sample, or for exactly ``job_ids`` when given.

    With ``carry_from`` (an earlier sheet), its jobs are reused and your labels are
    copied onto the fresh engine scores, so a scoring change can be re-measured
    without labelling again.
    """
    carried_jobs: dict[str, dict[str, str]] = {}
    carried_reqs: dict[tuple[str, str], dict[str, str]] = {}
    if carry_from is not None:
        carried_jobs = {r["job_id"]: r for r in read_csv(carry_from / "jobs.csv")}
        carried_reqs = {(r["job_id"], r["requirement"]): r
                        for r in read_csv(carry_from / "requirements.csv")}
        job_ids = job_ids or list(carried_jobs)
    data = resume_path.read_bytes()
    index = jobboard.load_or_build_index(workspace)
    jobs = index["jobs"]
    byid = {j["id"]: j for j in jobs}
    profile = jobboard.load_profile(resume_path.name, data)
    scores = {s["id"]: s for s in jobboard.score_jobs(profile, jobs)}
    if job_ids:
        unknown = [i for i in job_ids if i not in scores]
        if unknown:
            raise ValueError(f"not scorable or not on the board: {', '.join(unknown)}")
        picked = [(i, band_of(scores[i])) for i in dict.fromkeys(job_ids)]
    else:
        per_band = max(1, jobs_wanted // (len(SCORE_BANDS) + 1))
        picked = sample_jobs(scores, per_band, seed)
    stem = re.sub(r"[^a-z0-9]+", "-", resume_path.stem.lower()).strip("-") or "resume"
    label = re.sub(r"[^a-z0-9]+", "-", (name or f"seed{seed}").lower()).strip("-")
    root = out_root or workspace / "outputs" / "evaluation"
    base = f"{dt.date.today().isoformat()}-{stem}-{label}"
    folder, suffix = root / base, 2
    while folder.exists():
        folder = root / f"{base}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True)
    job_rows, req_rows = [], []
    for job_id, band in picked:
        job, score = byid[job_id], scores[job_id]
        statuses = {}
        if not score["insufficient"]:
            for name in score["matched"]:
                statuses[name] = "matched"
            for name, status in score["gaps"]:
                statuses[name] = STATUS_WORD.get(status, status)
        old = carried_jobs.get(job_id, {})
        job_rows.append([job_id, band, job["company"], job["title"], job["level"],
                         job["location"], job["url"] or "", f"target_jobs/{job['file']}",
                         "" if score["insufficient"] else score["score"],
                         "not enough detail" if score["insufficient"] else "scored",
                         "; ".join(score.get("matched", [])),
                         "; ".join(n for n, _s in score.get("gaps", [])),
                         old.get("your_fit_1_to_5", ""), old.get("missed_requirements", ""),
                         old.get("notes", "")])
        for req in job["requirements"]:
            weight = pipeline.CATEGORY_WEIGHTS[req["category"]]
            prior = carried_reqs.get((job_id, req["name"]), {})
            req_rows.append([job_id, job["title"], req["name"], req["category"], weight,
                             statuses.get(req["name"], "not scored"),
                             prior.get("in_posting_yes_no", ""),
                             prior.get("engine_call_correct_yes_no", ""), prior.get("notes", "")])
    write_csv(folder / "jobs.csv",
              ["job_id", "sample_band", "company", "title", "level", "location", "posting_url",
               "job_file", "engine_score", "engine_status", "engine_matched", "engine_gaps",
               "your_fit_1_to_5", "missed_requirements", "notes"], job_rows)
    write_csv(folder / "requirements.csv",
              ["job_id", "title", "requirement", "category", "weight", "engine_call",
               "in_posting_yes_no", "engine_call_correct_yes_no", "notes"], req_rows)
    meta = {"resume_file": resume_path.name, "resume_sha256": hashlib.sha256(data).hexdigest(),
            "profile_mode": profile.mode, "index_generated": index.get("generated"),
            "seed": seed, "selection": "chosen jobs" if job_ids else "stratified sample",
            "labels_carried_from": carry_from.name if carry_from else None,
            "jobs": len(job_rows), "requirements": len(req_rows),
            "min_skill_requirements": jobboard.MIN_SKILL_REQUIREMENTS,
            "threshold": pipeline.FIT_THRESHOLD, "pipeline_version": pipeline.PIPELINE_VERSION}
    (folder / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (folder / "README.md").write_text(SHEET_README.format(folder=folder.relative_to(workspace)
                                                          if folder.is_relative_to(workspace)
                                                          else folder, **meta),
                                      encoding="utf-8")
    return folder


SHEET_README = """# Scoring evaluation sheet

Resume: {resume_file} ({profile_mode} mode). Sample: {jobs} jobs and {requirements}
requirements (seed {seed}). The resume text is not stored here; meta.json keeps only its
SHA-256 so results can be traced to the exact file.

## How to label

Open both CSV files in Numbers, Excel or Google Sheets. Read each job through its
`posting_url` or its `job_file`.

`requirements.csv`, one row per requirement the engine extracted:

- `in_posting_yes_no`: does the posting really ask for this? (yes/no)
- `engine_call_correct_yes_no`: is the engine's `engine_call` right for your resume?
  `matched` means the engine thinks you have it; `missing`, `no` and `unclear` mean it
  does not count it. Answer yes if that call is correct. Leave it blank when
  `in_posting` is no, or when the job was "not scored".

`jobs.csv`, one row per job:

- `your_fit_1_to_5`: your own judgment (1 = poor fit, 5 = excellent), made without
  looking at `engine_score` if you can.
- `missed_requirements`: important requirements the engine did not list, separated by
  semicolons (for example `Kubernetes; on-call rotation`).

Blank cells are skipped, so a partly labelled sheet still produces a report.

## Then run

    python3 evaluate.py report {folder}
"""


# ---------------------------------------------------------------------------
# Accuracy report
# ---------------------------------------------------------------------------
def answer(value: str | None) -> bool | None:
    word = (value or "").strip().lower()
    if word in YES:
        return True
    if word in NO:
        return False
    return None


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation with average ranks for ties; None when undefined."""
    if len(xs) < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                result[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return result

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return None if var == 0 else cov / var


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def pct(num: int, den: int) -> str:
    return "n/a" if den == 0 else f"{100 * num / den:.0f}% ({num}/{den})"


def build_report(folder: Path) -> dict[str, Any]:
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    jobs = read_csv(folder / "jobs.csv")
    reqs = read_csv(folder / "requirements.csv")
    threshold = meta["threshold"]
    in_posting = [answer(r["in_posting_yes_no"]) for r in reqs]
    yes = sum(1 for a in in_posting if a is True)
    no = sum(1 for a in in_posting if a is False)
    missed = sum(len([m for m in re.split(r"[;\n]", j["missed_requirements"]) if m.strip()])
                 for j in jobs)
    confusion = {"matched_correct": 0, "matched_wrong": 0, "not_matched_correct": 0,
                 "not_matched_wrong": 0}
    for row in reqs:
        if answer(row["in_posting_yes_no"]) is not True:
            continue
        correct = answer(row["engine_call_correct_yes_no"])
        if correct is None or row["engine_call"] == "not scored":
            continue
        matched = row["engine_call"] == "matched"
        confusion[("matched_" if matched else "not_matched_") + ("correct" if correct
                                                                   else "wrong")] += 1
    calls = sum(confusion.values())
    pairs = [(float(j["engine_score"]), float(j["your_fit_1_to_5"])) for j in jobs
             if j["engine_score"].strip() and j["your_fit_1_to_5"].strip()]
    rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    by_band: dict[str, list[float]] = {}
    for j in jobs:
        if j["your_fit_1_to_5"].strip():
            by_band.setdefault(j["sample_band"], []).append(float(j["your_fit_1_to_5"]))
    high = [p for p in pairs if p[0] >= threshold]
    liked = [p for p in pairs if p[1] >= 4]
    thin_rated = [float(j["your_fit_1_to_5"]) for j in jobs
                  if j["engine_status"] == "not enough detail" and j["your_fit_1_to_5"].strip()]
    return {
        "meta": meta,
        "extraction_precision": (yes, yes + no),
        "extraction_recall": (yes, yes + missed),
        "matching_accuracy": (confusion["matched_correct"] + confusion["not_matched_correct"],
                              calls),
        "confusion": confusion,
        "spearman": rho, "rated_scored_jobs": len(pairs),
        "fit_by_band": {k: sum(v) / len(v) for k, v in sorted(by_band.items())},
        "band_counts": {k: len(v) for k, v in sorted(by_band.items())},
        "threshold_precision": (sum(1 for p in high if p[1] >= 4), len(high)),
        "threshold_recall": (sum(1 for p in liked if p[0] >= threshold), len(liked)),
        "thin_good_fits": (sum(1 for v in thin_rated if v >= 4), len(thin_rated)),
    }


def render_report(result: dict[str, Any]) -> str:
    meta, c = result["meta"], result["confusion"]
    rho = result["spearman"]
    labelled = (result["extraction_precision"][1] + result["matching_accuracy"][1]
                + sum(result["band_counts"].values()))
    if labelled == 0:
        return ("# Scoring accuracy report\n\nNo labels found yet. Fill in "
                "`your_fit_1_to_5` in jobs.csv and the two yes/no columns in "
                "requirements.csv (see README.md in this folder), save the files as CSV, "
                "then run this report again.\n")
    lines = [
        "# Scoring accuracy report", "",
        f"Resume: {meta['resume_file']} ({meta['profile_mode']} mode), threshold "
        f"{meta['threshold']}, seed {meta['seed']}.", "",
        "## Requirement extraction",
        f"- Precision (extracted requirements that are really in the posting): "
        f"{pct(*result['extraction_precision'])}",
        f"- Recall (estimated; real requirements the engine found): "
        f"{pct(*result['extraction_recall'])}", "",
        "## Matching your resume",
        f"- Correct calls: {pct(*result['matching_accuracy'])}",
        f"- Said you have it, and you do: {c['matched_correct']}",
        f"- Said you have it, but you don't (false match): {c['matched_wrong']}",
        f"- Said you don't have it, and you don't: {c['not_matched_correct']}",
        f"- Said you don't have it, but you do (missed evidence): {c['not_matched_wrong']}", "",
        "## Scores versus your fit ratings",
        f"- Rank correlation (Spearman) over {result['rated_scored_jobs']} rated jobs: "
        + ("n/a (need at least 3)" if rho is None else f"{rho:.2f}"),
        f"- Jobs at or above {meta['threshold']} that you rated 4 or 5: "
        f"{pct(*result['threshold_precision'])}",
        f"- Jobs you rated 4 or 5 that scored {meta['threshold']} or more: "
        f"{pct(*result['threshold_recall'])}",
        f"- \"Not enough detail\" jobs you rated 4 or 5: {pct(*result['thin_good_fits'])}", "",
        "Average fit rating by score band:",
    ]
    for band, mean in result["fit_by_band"].items():
        lines.append(f"- {band}: {mean:.1f} (n={result['band_counts'][band]})")
    small = [name for name, (n, d) in (("extraction", result["extraction_precision"]),
                                       ("matching", result["matching_accuracy"]))
             if d < 20] + (["fit"] if result["rated_scored_jobs"] < 15 else [])
    if small:
        lines += ["", f"Small sample for: {', '.join(small)}. Label more rows before "
                      f"trusting these numbers."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="evaluate.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    checks = sub.add_parser("checks", help="automatic sensitivity checks")
    checks.add_argument("--resume", type=Path, help="resume or master file (default: synthetic)")
    sheet = sub.add_parser("sheet", help="write a labelling sheet")
    sheet.add_argument("--resume", type=Path, required=True)
    sheet.add_argument("--jobs", type=int, default=30)
    sheet.add_argument("--seed", type=int, default=7)
    sheet.add_argument("--job", action="append", dest="job_ids",
                       help="include this job id instead of sampling (repeatable)")
    sheet.add_argument("--name", help="folder label (default: seed<N>)")
    sheet.add_argument("--carry-labels", type=Path, dest="carry_from",
                       help="earlier sheet folder: reuse its jobs and copy your labels over")
    report = sub.add_parser("report", help="accuracy from a labelled sheet")
    report.add_argument("folder", type=Path)
    for p in (checks, sheet, report):
        p.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    try:
        if args.command == "checks":
            text = args.resume.read_text(encoding="utf-8", errors="replace") if args.resume \
                else DEFAULT_RESUME
            name = args.resume.name if args.resume else "synthetic-resume.txt"
            if args.resume and args.resume.suffix.lower() == ".pdf":
                text = jobboard.decode_upload(name, args.resume.read_bytes())
                name = args.resume.stem + ".txt"
            results = run_checks(workspace, text, name)
            width = max(len(r.name) for r in results)
            for r in results:
                out(f"{r.status:<4}  {r.name:<{width}}  {r.detail}")
            failed = [r for r in results if r.status == "FAIL"]
            out(f"\n{len(results) - len(failed)} of {len(results)} checks passed or skipped")
            return 1 if failed else 0
        if args.command == "sheet":
            folder = make_sheet(workspace, args.resume, args.jobs, args.seed,
                                job_ids=args.job_ids, name=args.name, carry_from=args.carry_from)
            out(f"Labelling sheet: {folder}\nOpen jobs.csv and requirements.csv, fill the "
                f"blank columns, then run: python3 evaluate.py report {folder}")
            return 0
        result = build_report(args.folder)
        text = render_report(result)
        (args.folder / "report.md").write_text(text, encoding="utf-8")
        out(text)
        return 0
    except (jobboard.ProfileError, OSError, KeyError, ValueError) as exc:
        out(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
