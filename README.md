<p align="center">
  <img src="static/brand/mark.svg" alt="VouchStudio" width="72">
</p>

<h1 align="center">VouchStudio</h1>
<p align="center"><strong>Better jobs. Smarter search.</strong></p>

VouchStudio scores open roles against the evidence in your resume, then builds a one-page
resume tailored to a job, using only facts you have marked as verified.

- **Job board:** pulls public postings from Greenhouse, Ashby, Simplify and Jooble across the
  US and Canada, and scores every one against your resume or master profile.
- **Evidence-based scoring:** each requirement is matched to a line of your profile. A skill
  counts only when a verified line backs it, and related skills are never substituted
  (AWS never implies Azure).
- **Resume tailoring:** builds a one-page LaTeX resume for a job. Every claim is traced to
  your master profile, and the PDF must pass four checks: it compiles, fits on exactly one
  page, reads cleanly in applicant tracking systems, and has a clean layout.
- **Local and deterministic:** the scoring engine uses only the Python standard library, with
  no LLMs or external APIs. The same profile and posting always give the same score.

## Quick start

Requires Python 3.10+ on macOS or Linux.

```bash
./run_ui.sh
```

This opens VouchStudio at http://127.0.0.1:8000. The first run creates `.venv`, installs
FastAPI and uvicorn, and indexes the jobs, so it takes a minute; later starts are quick.
Create an account, then upload a resume (PDF, `.md` or `.txt`) to score the board.

To tailor resumes you also need:

- a LaTeX install with `pdflatex`, plus `pdftotext` and `pdfinfo` (poppler)
- `master_profile.md` and `template.tex` in the project root (both gitignored)
- an admin account: list your email in `.studio/admins.txt` before signing up

## Configuration

Copy `.env.example` to `.env` and fill in only what you use. `.env` is gitignored, and
`./run_ui.sh` loads it automatically.

| Variable | Used for |
|---|---|
| `JOOBLE_API_KEY` | Jooble job search (optional) |
| `APIFY_TOKEN` | Jobright via Apify (optional, billable) |
| `DATABASE_URL` | Postgres for accounts (default: local SQLite in `.studio/`) |
| `VOUCH_ADMIN_EMAILS` | Comma-separated admin emails |

Job sources and filters live in `config/companies.json`.

## Command line

```bash
python3 fetch_jobs.py                               # fetch postings into target_jobs/
python3 pipeline.py target_jobs/<job>.md            # score one job; build a resume at 65+
python3 pipeline.py target_jobs/<job>.md --dry-run  # analyze only, write nothing
python3 dashboard.py --open                         # static HTML dashboard of all scores
```

## Tests

```bash
python3 -m unittest discover -s tests               # engine (offline, standard library)
.venv/bin/python -m unittest discover -s tests      # also runs the web app tests
```

## Privacy

Uploaded resumes are scored on your own machine. Your master profile, template, accounts
database, generated resumes and `.env` are all gitignored and never committed
