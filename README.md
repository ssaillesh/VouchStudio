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
| `DATABASE_URL` | Neon Postgres (default for the local app: SQLite in `.studio/`) |
| `VOUCH_ADMIN_EMAILS` | Comma-separated admin emails |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | Cloudflare R2 for files (all four or none); `R2_ENDPOINT` for an EU bucket |
| `ENGINE_SECRET` | Shared secret between the Next.js API and the Python engine function |
| `ENGINE_URL` | Local development only: where `npm run engine` listens |
| `CRON_SECRET` | Authorizes the daily cleanup cron on Vercel |

Job sources and filters live in `config/companies.json`.

## Hosting: Vercel, Neon and Cloudflare R2

The hosted app is a Next.js project at the repository root (`src/`), deployed to Vercel.

| Piece | Where | What |
|---|---|---|
| Pages and API | Vercel, Next.js (`src/app`) | Home, sign-up, the board, your profile, reviews and admin; accounts, uploads and reviews API |
| Scoring engine | Vercel Python Function (`api/engine.py`) | The same `jobboard.py` scorer, standard library only, called server to server |
| Database | Neon (`db/schema.sql`) | Accounts (shared with `accounts.py`), jobs, cached scores, reviews and links |
| Files | Cloudflare R2 | Profiles, resume PDFs, review images, PDFs and videos (presigned URLs), and the job index |
| Tailoring and fetching | Your machine | LaTeX does not run on Vercel: fetch and publish jobs, and run queued Pro builds, locally |

One-time setup:

1. **Neon:** create a project and copy the pooled connection string. The Neon integration
   on Vercel can set `DATABASE_URL` and give each preview deployment its own branch.
2. **R2:** create a private bucket and an API token with Object Read & Write on that bucket
   only. Put your site's origins in `infra/r2-cors.json`, then apply it with
   `npx wrangler r2 bucket cors set <bucket> --file infra/r2-cors.json` (or paste the rules
   into the bucket's CORS settings). Browsers upload straight to R2, so CORS is required.
3. **Vercel:** import the repository; `vercel.json` sets Next.js, the Python function and the
   cron. Add `DATABASE_URL`, the four `R2_*` values, `ENGINE_SECRET`, `CRON_SECRET` and
   `VOUCH_ADMIN_EMAILS`. Each build runs `db/schema.sql` first (it is idempotent). For
   protected preview deployments, enable Protection Bypass for Automation so the API can
   reach the engine.
4. **Publish jobs** from your machine, with the same values in `.env`:
   `python3 fetch_jobs.py`, then `npm run publish:jobs` (`-- --dry-run` to preview).
   Each account's scores are recomputed on its next visit.
5. **Pro builds** requested on the site wait as "queued". Run `./run_ui.sh --process-builds`
   on your machine (LaTeX installed, same `.env`) to build them; the PDFs go to R2.

Local development of the hosted app: `npm install`, then `npm run engine` (with
`ENGINE_SECRET` set) in one terminal and `npm run dev` in another (http://localhost:3000).
PDF uploads are read with pdf.js on Vercel instead of `pdftotext`, so a PDF can score
slightly differently there; `.md` and `.txt` profiles score identically.

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

The local app scores uploaded resumes on your own machine. The hosted app keeps each
account's files in your private R2 bucket under that account's folder, serves them only
through five-minute links, and deletes the folder with the account; scoring runs in your
own Vercel deployment and nothing is sent to an AI service. Your master profile, template,
accounts database, generated resumes and `.env` are all gitignored and never committed
