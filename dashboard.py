#!/usr/bin/env python3
"""Static job dashboard for the local pipeline.

Usage:
    python3 dashboard.py            # writes outputs/dashboard.html
    python3 dashboard.py --open     # ... and opens it in the default browser

Reads every job file in target_jobs/, scores the ones with full descriptions using
pipeline.py's analysis (no resume building, nothing written besides the page),
links any verified resume PDF already in outputs/, and renders one self-contained
HTML file. Local only: no network requests, no external fonts or scripts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fetch_jobs
import pipeline

META_LINE = re.compile(r"^\*\*([A-Za-z ]+)\*\*:\s*(.*)$")


@dataclass
class JobView:
    file: str
    title: str
    company: str
    source: str
    location: str
    url: str | None
    posted: str | None
    complete: bool
    region: str
    score: int | None = None
    verdict: str | None = None
    domain: str | None = None
    confidence: str | None = None
    matched: list[str] = field(default_factory=list)
    gaps: list[tuple[str, str]] = field(default_factory=list)
    resume_pdf: str | None = None
    error: str | None = None


def read_metadata(text: str) -> dict[str, str]:
    """Labelled lines between the title and '## Description'."""
    meta: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if line.startswith("## "):
            break
        match = META_LINE.match(line.strip())
        if match:
            meta[match.group(1).strip().lower()] = match.group(2).strip()
    return meta


def region_of(location: str) -> str:
    folded = fetch_jobs._fold(location or "")
    us = any(p.search(folded) for p in fetch_jobs.US_LOCATION)
    ca = any(p.search(folded) for p in fetch_jobs.CANADA_LOCATION)
    if us and ca:
        return "US + Canada"
    if us:
        return "US"
    if ca:
        return "Canada"
    return "Remote" if re.search(r"remote|anywhere", location or "", re.I) else "Other"


def verified_resumes(outputs: Path) -> dict[str, str]:
    """Map source JD path -> verified resume PDF name, from analysis reports."""
    found: dict[str, str] = {}
    for report_path in sorted(outputs.glob("*_analysis.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        resume = report.get("resume") or {}
        pdf = (resume.get("files") or {}).get("pdf")
        if resume.get("single_page_verified") and pdf and (outputs / pdf).is_file():
            found[report.get("source_job_description", "")] = pdf
    return found


def collect(workspace: Path) -> list[JobView]:
    target = workspace / "target_jobs"
    outputs = workspace / "outputs"
    master = pipeline.parse_master(pipeline.find_master(workspace, None))
    resumes = verified_resumes(outputs) if outputs.is_dir() else {}
    views: list[JobView] = []
    for path in sorted(target.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = read_metadata(text)
        title_line = text.splitlines()[0].lstrip("# ").strip() if text.strip() else path.stem
        completeness = meta.get("description completeness", "Full")
        url = meta.get("url")
        view = JobView(
            file=path.name,
            title=meta.get("role") or title_line,
            company=meta.get("company") or "",
            source=meta.get("source") or "Manual",
            location=meta.get("location") or "Not specified",
            url=url if fetch_jobs.is_http_url(url) else None,
            posted=meta.get("posted") or meta.get("updated"),
            complete=not completeness.lower().startswith("partial"),
            region=region_of(meta.get("location") or ""),
        )
        display = f"target_jobs/{path.name}"
        view.resume_pdf = resumes.get(display)
        if view.complete:
            try:
                jd = pipeline.read_job_description(path, workspace)
                analysis = pipeline.analyze(jd, master)
                report = pipeline.build_report(analysis, workspace)
            except pipeline.PipelineError as exc:
                view.error = str(exc)
            else:
                view.company = view.company or jd.company
                view.score = analysis.score
                view.verdict = analysis.verdict
                view.domain = report["primary_domain"]
                view.confidence = report["score_confidence"]
                view.matched = [m["name"] for m in report["strong_matches"]]
                view.gaps = [(g["name"], g["status"]) for g in report["missing_gaps"]]
        views.append(view)
    views.sort(key=lambda v: (v.score is None, -(v.score or 0), v.company.lower(),
                              v.title.lower(), v.file))
    return views


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


GAP_LABEL = {"explicitly_no": "no", "not_evidenced": "missing", "ambiguous": "unclear"}


def render_row(view: JobView, index: int) -> str:
    proceed = view.verdict == pipeline.VERDICT_PROCEED
    score = "&mdash;" if view.score is None else str(view.score)
    status = ("listing" if not view.complete else "proceed" if proceed else "below")
    status_tag = {
        "proceed": '<span class="tag tag-green">Proceed</span>',
        "below": '<span class="tag tag-red">Below 65</span>',
        "listing": '<span class="tag tag-neutral">Listing only</span>',
    }[status]
    region_class = {"US": "tag-blue", "Canada": "tag-yellow",
                    "US + Canada": "tag-blue"}.get(view.region, "tag-neutral")
    matched = "".join(f'<span class="chip chip-match">{esc(m)}</span>' for m in view.matched[:8])
    more = len(view.matched) - 8
    if more > 0:
        matched += f'<span class="chip chip-more">+{more}</span>'
    gaps = "".join(f'<span class="chip chip-gap" title="{esc(GAP_LABEL.get(s, s))}">'
                   f'{esc(n)}<em>{esc(GAP_LABEL.get(s, s))}</em></span>'
                   for n, s in view.gaps[:6])
    links = []
    if view.url:
        links.append(f'<a class="link" href="{esc(view.url)}" target="_blank" '
                     f'rel="noopener noreferrer">Posting {ARROW}</a>')
    links.append(f'<a class="link" href="../target_jobs/{esc(view.file)}">Job file</a>')
    if view.resume_pdf:
        links.append(f'<a class="link link-strong" href="{esc(view.resume_pdf)}">'
                     f'Resume PDF {ARROW}</a>')
    command = f"python3 pipeline.py target_jobs/{view.file}"
    detail = ""
    if view.complete and view.score is not None:
        detail = (f'<div class="detail"><div><h4>Evidence matched</h4>'
                  f'<div class="chips">{matched or "<span class=muted>None</span>"}</div></div>'
                  f'<div><h4>Gaps</h4><div class="chips">'
                  f'{gaps or "<span class=muted>None</span>"}</div></div></div>')
    elif view.error:
        detail = f'<p class="muted small">Could not analyze: {esc(view.error)}</p>'
    else:
        detail = ('<p class="muted small">The source only lists this job. Open the posting '
                  'and save the full description before scoring it.</p>')
    meta_bits = [esc(view.company), esc(view.location)]
    if view.posted:
        meta_bits.append(f"posted {esc(view.posted)}")
    return f"""
    <article class="job reveal" style="--index:{min(index, 12)}"
      data-status="{status}" data-region="{esc(view.region)}" data-source="{esc(view.source)}"
      data-score="{view.score if view.score is not None else -1}"
      data-search="{esc((view.title + ' ' + view.company + ' ' + view.location).lower())}">
      <div class="score {'score-pass' if proceed else ''}" aria-label="Fit score">{score}</div>
      <div class="job-main">
        <div class="job-head">
          <h3>{esc(view.title)}</h3>
          <div class="tags">{status_tag}<span class="tag {region_class}">{esc(view.region)}</span>
            <span class="tag tag-neutral">{esc(view.source)}</span></div>
        </div>
        <p class="job-meta">{' <span class="dot">&middot;</span> '.join(meta_bits)}</p>
        {detail}
        <div class="job-foot">
          <div class="links">{''.join(links)}</div>
          <button class="copy" type="button" data-copy="{esc(command)}">
            <kbd>{esc(command)}</kbd><span class="copy-label">Copy</span></button>
        </div>
      </div>
    </article>"""


ARROW = ('<svg class="arrow" viewBox="0 0 12 12" aria-hidden="true"><path d="M3.5 8.5l5-5M4.5 '
         '3.5h4v4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square"/>'
         '</svg>')


def render(views: list[JobView], generated: str) -> str:
    total = len(views)
    full = [v for v in views if v.complete]
    proceed = [v for v in views if v.verdict == pipeline.VERDICT_PROCEED]
    resumes = [v for v in views if v.resume_pdf]
    us = sum(1 for v in views if v.region in ("US", "US + Canada"))
    canada = sum(1 for v in views if v.region in ("Canada", "US + Canada"))
    best = max(full, key=lambda v: v.score or -1, default=None)
    sources: dict[str, int] = {}
    for view in views:
        sources[view.source] = sources.get(view.source, 0) + 1
    source_rows = "".join(f'<li><span>{esc(name)}</span><span class="mono">{count}</span></li>'
                          for name, count in sorted(sources.items(), key=lambda kv: -kv[1]))
    rows = "".join(render_row(view, index) for index, view in enumerate(views))
    best_text = (f'<p class="stat-big">{best.score}</p><p class="stat-label">Top fit: '
                 f'{esc(best.title)} at {esc(best.company)}</p>') if best and best.score else \
        '<p class="stat-big">&mdash;</p><p class="stat-label">No scored jobs yet</p>'
    return TEMPLATE.format(
        generated=esc(generated), total=total, full=len(full), proceed=len(proceed),
        resumes=len(resumes), us=us, canada=canada, listing=total - len(full),
        best=best_text, source_rows=source_rows, rows=rows,
        threshold=pipeline.FIT_THRESHOLD)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Board</title>
<style>
:root {{
  --canvas: #FBFBFA; --surface: #FFFFFF; --sunk: #F7F6F3; --border: #EAEAEA;
  --ink: #111111; --body: #2F3437; --muted: #787774;
  --green-bg: #EDF3EC; --green: #346538; --red-bg: #FDEBEC; --red: #9F2F2D;
  --blue-bg: #E1F3FE; --blue: #1F6C9F; --yellow-bg: #FBF3DB; --yellow: #956400;
  --sans: "SF Pro Display", "SF Pro Text", "Helvetica Neue", system-ui, sans-serif;
  --serif: "New York", "Iowan Old Style", "Charter", ui-serif, Georgia, serif;
  --mono: "SF Mono", ui-monospace, "JetBrains Mono", Menlo, monospace;
  --ease: cubic-bezier(0.16, 1, 0.3, 1);
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-font-smoothing: antialiased; }}
body {{ margin: 0; background: var(--canvas); color: var(--body); font: 15px/1.6 var(--sans); }}
.ambient {{ position: fixed; inset: -20vmax; pointer-events: none; z-index: 0;
  background: radial-gradient(circle at 30% 20%, rgba(214, 196, 160, 0.06) 0%,
    rgba(214, 196, 160, 0.025) 25%, rgba(214, 196, 160, 0) 55%);
  animation: drift 28s ease-in-out infinite alternate; }}
@keyframes drift {{ to {{ transform: translate3d(8vmax, 6vmax, 0); }} }}
.wrap {{ position: relative; z-index: 1; max-width: 1040px; margin: 0 auto; padding: 0 24px; }}
header.top {{ padding: 96px 0 48px; }}
.eyebrow {{ font: 500 12px/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 20px; }}
h1 {{ font: 400 clamp(40px, 6vw, 64px)/1.05 var(--serif); letter-spacing: -0.035em;
  color: var(--ink); margin: 0; }}
.lede {{ max-width: 620px; color: var(--muted); font-size: 17px; margin: 20px 0 0; }}
.bento {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin: 0 0 96px; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 28px; transition: box-shadow 200ms ease; }}
.card:hover {{ box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04); }}
.span-3 {{ grid-column: span 3; }} .span-2 {{ grid-column: span 2; }}
.span-4 {{ grid-column: span 4; }} .span-6 {{ grid-column: span 6; }}
.stat-big {{ font: 400 44px/1 var(--serif); letter-spacing: -0.03em; color: var(--ink);
  margin: 0 0 10px; }}
.stat-label {{ margin: 0; color: var(--muted); font-size: 13px; }}
.split {{ display: flex; gap: 32px; }}
.sources {{ list-style: none; margin: 0; padding: 0; font-size: 14px; display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0 32px; }}
.sources li {{ display: flex; justify-content: space-between; padding: 10px 0;
  border-bottom: 1px solid var(--border); }}
.mono {{ font-family: var(--mono); }}
.card h2 {{ font: 500 12px/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 18px; }}
section.list-head {{ display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between;
  gap: 16px; padding-bottom: 20px; border-bottom: 1px solid var(--border); }}
section.list-head h2 {{ font: 400 32px/1.1 var(--serif); letter-spacing: -0.025em; color: var(--ink);
  margin: 0; }}
.controls {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.search {{ font: 14px var(--sans); color: var(--ink); background: var(--surface);
  border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; width: 220px; }}
.search:focus {{ outline: none; border-color: #CFCFCB; }}
.seg {{ display: inline-flex; border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); overflow: hidden; }}
.seg button {{ font: 13px var(--sans); color: var(--muted); background: none; border: 0;
  padding: 7px 12px; cursor: pointer; border-right: 1px solid var(--border); }}
.seg button:last-child {{ border-right: 0; }}
.seg button[aria-pressed="true"] {{ background: var(--ink); color: #FFFFFF; }}
.count {{ font: 12px var(--mono); color: var(--muted); }}
.jobs {{ margin: 0 0 96px; }}
.job {{ display: grid; grid-template-columns: 72px 1fr; gap: 20px; padding: 28px 0;
  border-bottom: 1px solid var(--border); }}
.job[hidden] {{ display: none; }}
.score {{ font: 400 34px/1 var(--serif); color: var(--muted); letter-spacing: -0.03em;
  padding-top: 2px; }}
.score-pass {{ color: var(--ink); }}
.job-head {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 16px; }}
.job h3 {{ font: 500 17px/1.35 var(--sans); color: var(--ink); margin: 0; letter-spacing: -0.01em; }}
.job-meta {{ margin: 4px 0 0; color: var(--muted); font-size: 14px; }}
.dot {{ padding: 0 4px; }}
.tags {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.tag {{ font: 600 10.5px/1 var(--sans); letter-spacing: 0.06em; text-transform: uppercase;
  border-radius: 9999px; padding: 5px 9px; white-space: nowrap; }}
.tag-green {{ background: var(--green-bg); color: var(--green); }}
.tag-red {{ background: var(--red-bg); color: var(--red); }}
.tag-blue {{ background: var(--blue-bg); color: var(--blue); }}
.tag-yellow {{ background: var(--yellow-bg); color: var(--yellow); }}
.tag-neutral {{ background: var(--sunk); color: var(--muted); }}
.detail {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 18px 0 0; }}
.detail h4 {{ font: 500 11px/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 10px; }}
.chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.chip {{ font: 12.5px/1.4 var(--sans); border-radius: 4px; padding: 3px 8px;
  border: 1px solid var(--border); background: var(--surface); color: var(--body); }}
.chip-match {{ background: var(--green-bg); border-color: transparent; color: var(--green); }}
.chip-gap em {{ font: 10.5px var(--mono); font-style: normal; color: var(--muted); margin-left: 6px; }}
.chip-more {{ color: var(--muted); }}
.muted {{ color: var(--muted); }} .small {{ font-size: 13px; margin: 14px 0 0; }}
.job-foot {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
  gap: 12px; margin-top: 18px; }}
.links {{ display: flex; gap: 18px; flex-wrap: wrap; }}
.link {{ font-size: 14px; color: var(--body); text-decoration: none;
  border-bottom: 1px solid var(--border); padding-bottom: 1px; display: inline-flex;
  align-items: center; gap: 4px; }}
.link:hover {{ border-color: var(--ink); color: var(--ink); }}
.link-strong {{ color: var(--ink); font-weight: 500; border-color: var(--ink); }}
.arrow {{ width: 11px; height: 11px; }}
.copy {{ display: inline-flex; align-items: center; gap: 8px; background: none; border: 0; padding: 0;
  cursor: pointer; max-width: 100%; }}
kbd {{ font: 12px var(--mono); color: var(--body); background: var(--sunk);
  border: 1px solid var(--border); border-radius: 4px; padding: 4px 8px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; max-width: 460px; display: inline-block; }}
.copy-label {{ font: 12px var(--sans); color: var(--muted); }}
.copy:hover kbd {{ border-color: #CFCFCB; }}
.copy:active {{ transform: scale(0.98); }}
.faq {{ max-width: 720px; margin: 0 0 120px; }}
.faq h2 {{ font: 400 32px/1.1 var(--serif); letter-spacing: -0.025em; color: var(--ink);
  margin: 0 0 12px; }}
details {{ border-bottom: 1px solid var(--border); }}
summary {{ list-style: none; cursor: pointer; padding: 20px 0; font-weight: 500; color: var(--ink);
  display: flex; justify-content: space-between; }}
summary::-webkit-details-marker {{ display: none; }}
summary::after {{ content: "+"; font: 400 20px/1 var(--mono); color: var(--muted); }}
details[open] summary::after {{ content: "\\2212"; }}
details p {{ margin: 0 0 20px; color: var(--body); }}
footer {{ padding: 32px 0 64px; border-top: 1px solid var(--border); color: var(--muted);
  font-size: 13px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}
.reveal {{ opacity: 0; transform: translateY(12px);
  transition: opacity 600ms var(--ease), transform 600ms var(--ease);
  transition-delay: calc(var(--index, 0) * 80ms); }}
.reveal.in {{ opacity: 1; transform: none; }}
@media (prefers-reduced-motion: reduce) {{
  .reveal {{ opacity: 1; transform: none; transition: none; }} .ambient {{ animation: none; }} }}
@media (max-width: 760px) {{
  header.top {{ padding: 64px 0 32px; }}
  .bento {{ grid-template-columns: 1fr 1fr; }} .span-2, .span-3, .span-4, .span-6 {{ grid-column: span 2; }}
  .job {{ grid-template-columns: 52px 1fr; gap: 14px; }} .score {{ font-size: 26px; }}
  .detail {{ grid-template-columns: 1fr; }} .search {{ width: 100%; }} kbd {{ max-width: 260px; }}
}}
</style>
</head>
<body>
<div class="ambient" aria-hidden="true"></div>
<div class="wrap">
  <header class="top reveal">
    <p class="eyebrow">jobauto &middot; local job board</p>
    <h1>Where you fit, ranked by evidence.</h1>
    <p class="lede">Every job in target_jobs, scored against your master profile with the same
      rules the resume pipeline uses. Scores of {threshold} and above get a tailored resume.</p>
  </header>

  <section class="bento">
    <div class="card span-2 reveal" style="--index:0"><p class="stat-big">{total}</p>
      <p class="stat-label">Jobs in target_jobs, {listing} of them listing-only</p></div>
    <div class="card span-2 reveal" style="--index:1"><p class="stat-big">{proceed}</p>
      <p class="stat-label">At or above {threshold}, out of {full} with full descriptions</p></div>
    <div class="card span-2 reveal" style="--index:2"><p class="stat-big">{resumes}</p>
      <p class="stat-label">Verified one-page resumes built</p></div>
    <div class="card span-4 reveal" style="--index:3">{best}</div>
    <div class="card span-2 reveal" style="--index:4"><h2>Regions</h2>
      <div class="split"><div><p class="stat-big">{us}</p><p class="stat-label">US</p></div>
      <div><p class="stat-big">{canada}</p><p class="stat-label">Canada</p></div></div></div>
    <div class="card span-6 reveal" style="--index:5"><h2>Sources</h2>
      <ul class="sources">{source_rows}</ul></div>
  </section>

  <section class="list-head reveal">
    <h2>All jobs</h2>
    <div class="controls">
      <input class="search" type="search" placeholder="Filter by title, company, city"
        aria-label="Filter jobs">
      <div class="seg" role="group" aria-label="Status">
        <button type="button" data-filter="status" data-value="all" aria-pressed="true">All</button>
        <button type="button" data-filter="status" data-value="proceed" aria-pressed="false">Proceed</button>
        <button type="button" data-filter="status" data-value="scored" aria-pressed="false">Scored</button>
        <button type="button" data-filter="status" data-value="listing" aria-pressed="false">Listings</button>
      </div>
      <div class="seg" role="group" aria-label="Region">
        <button type="button" data-filter="region" data-value="all" aria-pressed="true">Anywhere</button>
        <button type="button" data-filter="region" data-value="US" aria-pressed="false">US</button>
        <button type="button" data-filter="region" data-value="Canada" aria-pressed="false">Canada</button>
      </div>
      <span class="count" aria-live="polite"></span>
    </div>
  </section>
  <div class="jobs">{rows}
  </div>

  <section class="faq reveal">
    <h2>How to read this</h2>
    <details><summary>What the score means</summary>
      <p>The share of the posting's parsed requirements that your master profile backs with
      verified evidence, weighted 3 for must-haves, 2 for required and 1 for nice-to-haves.
      It measures evidence coverage, not your chance of getting hired.</p></details>
    <details><summary>Why some jobs have no score</summary>
      <p>Listing-only jobs (mostly Simplify) come without a description, so there is nothing to
      score. Open the posting, paste the full description into a file in target_jobs, and it
      will be scored on the next refresh.</p></details>
    <details><summary>Gaps marked "no", "missing" and "unclear"</summary>
      <p>"No" means the skill is marked [NO] in your master profile. "Missing" means there is
      no verified evidence for it. "Unclear" means it only appears in unconfirmed or heading
      text, or the posting's wording could not be settled.</p></details>
    <details><summary>Building a resume</summary>
      <p>Copy the command on a job and run it from the jobauto folder. It writes the analysis
      and, at {threshold} or above, a verified one-page PDF that appears here after you run
      python3 dashboard.py again.</p></details>
  </section>

  <footer><span>Generated {generated} by dashboard.py</span>
    <span>Refresh: python3 fetch_jobs.py, then python3 dashboard.py</span></footer>
</div>
<script>
(() => {{
  const jobs = [...document.querySelectorAll('.job')];
  const state = {{ status: 'all', region: 'all', q: '' }};
  const count = document.querySelector('.count');
  function apply() {{
    let shown = 0;
    for (const job of jobs) {{
      const s = job.dataset.status, r = job.dataset.region;
      const okStatus = state.status === 'all' || (state.status === 'scored' ? s !== 'listing'
        : s === state.status);
      const okRegion = state.region === 'all' || r.includes(state.region);
      const okText = !state.q || job.dataset.search.includes(state.q);
      job.hidden = !(okStatus && okRegion && okText);
      if (!job.hidden) {{ shown++; job.classList.add('in'); }}
    }}
    count.textContent = shown + ' of ' + jobs.length;
  }}
  document.querySelectorAll('.seg button').forEach(btn => btn.addEventListener('click', () => {{
    btn.parentElement.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', 'false'));
    btn.setAttribute('aria-pressed', 'true');
    state[btn.dataset.filter] = btn.dataset.value;
    apply();
  }}));
  document.querySelector('.search').addEventListener('input', e => {{
    state.q = e.target.value.trim().toLowerCase(); apply();
  }});
  document.querySelectorAll('.copy').forEach(btn => btn.addEventListener('click', async () => {{
    const label = btn.querySelector('.copy-label');
    try {{ await navigator.clipboard.writeText(btn.dataset.copy); label.textContent = 'Copied'; }}
    catch (e) {{ label.textContent = 'Select and copy'; }}
    setTimeout(() => label.textContent = 'Copy', 1600);
  }}));
  const io = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {{
    for (const entry of entries) if (entry.isIntersecting) {{
      entry.target.classList.add('in'); io.unobserve(entry.target); }}
  }}, {{ rootMargin: '0px 0px -40px 0px' }}) : null;
  document.querySelectorAll('.reveal').forEach(el => io ? io.observe(el) : el.classList.add('in'));
  apply();
}})();
</script>
</body>
</html>
"""


def build(workspace: Path, out_path: Path | None = None,
          now: dt.datetime | None = None) -> Path:
    views = collect(workspace)
    generated = (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    out_path = out_path or workspace / "outputs" / "dashboard.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fetch_jobs.atomic_write(out_path, render(views, generated))
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.py",
                                     description="Write outputs/dashboard.html from target_jobs/.")
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    try:
        path = build(args.workspace.resolve())
    except pipeline.PipelineError as exc:
        print(f"ERROR: {exc}")
        return exc.exit_code
    print(f"Dashboard: {path}")
    if args.open:
        webbrowser.open(path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
