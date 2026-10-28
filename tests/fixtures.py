"""Synthetic, generic fixtures for the pipeline tests.

Nothing here describes a real person. Fixtures are written into temporary
directories; the real master profile and template are never touched.
"""

from __future__ import annotations

import importlib.util
import json
from html import escape
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent


def load_pipeline():
    if "pipeline" in sys.modules:
        return sys.modules["pipeline"]
    spec = importlib.util.spec_from_file_location("pipeline", ROOT / "pipeline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pipeline"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MASTER = """# Master Career Bank: Test Person

## Legend

| Tag | Meaning |
|---|---|
| `[V]` | Verified |
| `[NO]` | Never use |

## 1. Skills inventory (with evidence)

| Skill | Depth | Evidence |
|---|---|---|
| Python | Production | Acme tooling, all projects |
| Docker | Project | Acme builds |

Exposure-level skills, never main bullets:

| Skill | Where | Depth |
|---|---|---|
| Helm | Acme | Exposure |
| ELK | Nowhere | `[NO]` |

## 2. Experience

### 2.1 Acme Corp: Platform Engineer, Toronto, Jan 2024 to Dec 2024

- `[V]` Built a Python tool that cut deploy time 30% across 50+ services.
- `[V]` Ran GitLab CI/CD pipelines with Docker executors on Ubuntu runners.
- `[V]` Led troubleshooting of runner outages with the platform team.
- `[V]` No experience with Kafka in this role.
- `[NO]` Grafana at Acme.
- `[OLD]` Jenkins migration.
- `[GAP]` Spinnaker use unknown.

**Unconfirmed `[OLD]`**
- Ansible playbooks for patching.

### 2.2 Beta Labs: Data Analyst, Waterloo, Jan 2023 to Apr 2023

- `[V]` Built Power BI dashboards on SQL Server for 3 teams.
- `[V]` Wrote Terraform modules for a sandbox.

## 3. Projects

### 3.1 Gamma App
- `[R]` FastAPI service with PostgreSQL and Grafana dashboards.

**RBAC access tool**
- `[V]` Granted access to groups in seconds.

## 4. Education and awards

- `[V]` Example University, BSc Physics, 2024.
- `[V]` Languages: English; German fluent.

## 5. Global do-not-claim list

| Claim | Status |
|---|---|
| Kubernetes and Terraform | `[NO]` |
| Networking as a main skill (exposure only, via troubleshooting) | `[NO]` |
"""

KIT = """
## 9. Resume kit (for the tailoring pipeline)

Everything in this section is built only from `[V]` and `[R]` facts above.

### 9.1 Role families

| Code | Family | Typical titles |
|---|---|---|
| `dev` | DevOps, platform, SRE | DevOps Engineer, Platform Engineer, SRE |
| `swe` | Software engineering | Software Engineer, Backend Engineer |
| `ml` | Machine learning engineering | ML Engineer |
| `fde` | AI engineering, forward deployed | Forward Deployed Engineer, Solutions Engineer |
| `ana` | Data and analytics | Data Analyst, BI Analyst |
| `test` | Test infrastructure | Test Infra, SDET |
| `qa` | Quality and compliance | Quality Automation |
| `ops` | Operations | Operations |

### 9.2 Top proof points, ranked by strength

1. **Acme:** cut deploy time 30% with a Python tool across 50+ services.

### 9.3 Summaries by family

- **`dev`:** Platform engineer who cut deploy time 30% across 50+ services with a **Python** tool and runs **GitLab CI/CD** pipelines with **Docker** executors.
- **`ana`:** Analyst who built **Power BI** dashboards on **SQL Server** for 3 teams.

### 9.4 Skills rows

**Row library**

| Row | Items |
|---|---|
| Cloud and DevOps | Docker, GitLab CI/CD |
| Backend | Python, FastAPI |
| Databases | PostgreSQL, SQL Server |
| Analytics | Power BI, SQL Server |

**Row order by family.** Use at most 5 or 6 rows.

| Family | Row order |
|---|---|
| `dev` | Cloud and DevOps, Backend, Databases |
| `ana` | Analytics, Backend (Python), Databases |

**Guard:** any language or tool named in the job's core requirements that appears in the library must stay in skills.

### 9.5 Bullet bank

**Acme Corp, Platform Engineer, Toronto, Jan 2024 to Dec 2024**

| ID | Bullet | Themes | Families | Impact | Keep with |
|---|---|---|---|---|---|
| A01 | Cut deploy time 30% with a **Python** tool across 50+ services | automation, deployment | all | 5 | |
| A02 | Ran **GitLab CI/CD** pipelines with **Docker** executors on **Ubuntu** runners | ci_cd | dev, swe | 4 | |
| A03 | Led troubleshooting of runner outages with the platform team | debugging, collaboration | dev | 3 | |

**Beta Labs, Data Analyst, Waterloo, Jan 2023 to Apr 2023**

| ID | Bullet | Themes | Families | Impact | Keep with |
|---|---|---|---|---|---|
| B01 | Built **Power BI** dashboards on **SQL Server** for 3 teams | dashboards, analytics | ana, dev | 4 | |

**Gamma App**

| ID | Bullet | Themes | Families | Impact | Keep with |
|---|---|---|---|---|---|
| G01 | Built a **FastAPI** service with **PostgreSQL** and **Grafana** dashboards | backend, monitoring | all | 3 | |

### 9.6 Synonym map

| Fact | Requirement words it can honestly cover |
|---|---|
| Deploy tool (A01) | release automation, developer productivity |

### 9.7 Inclusion and exclusion rules
- **Projects:** pick the top 3 projects by requirement coverage. A project only gets in if at least one of its bullets maps to a job requirement.
- **Experience:** keep chronological order. Each included experience needs at least 3 bullets.

### 9.8 Gap lines (Interests) by family

| Family | Gaps |
|---|---|
| `dev` | Kubernetes, Terraform, Helm |
| `ana` | statistical modelling |

### 9.9 Formatting rules
- **Bullets:** each fits on one line. Bold only tech.
"""

MASTER_KIT = MASTER + KIT

TEMPLATE = r"""%-------------------------
% Synthetic test template
%-------------------------
\documentclass[letterpaper,11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[margin=0.75in]{geometry}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\input{glyphtounicode}
\pdfgentounicode=1
\titleformat{\section}{\scshape\large}{}{0em}{}[\titlerule]
\titlespacing*{\section}{0pt}{5pt}{4pt}
\newcommand{\resumeItem}[1]{\item\small{#1}}
\newcommand{\resumeSubheading}[4]{\item \textbf{#1} \hfill #2 \\ \textit{#3} \hfill \textit{#4}}
\newcommand{\resumeProjectHeading}[2]{\item #1 \hfill #2}
\newcommand{\resumeSubHeadingListStart}{\begin{itemize}[leftmargin=0.15in, label={}]}
\newcommand{\resumeSubHeadingListEnd}{\end{itemize}}
\newcommand{\resumeItemListStart}{\begin{itemize}}
\newcommand{\resumeItemListEnd}{\end{itemize}}
\pagestyle{empty}
\begin{document}
\begin{center}\textbf{\Huge \scshape Test Person}\end{center}

\section{Summary}
\begin{itemize}[leftmargin=0.15in, label={}]
    \item \small{
    Platform engineer who built a \textbf{Python} tool that cut deploy time 30\%. Expert in Kubernetes clusters.
    }
\end{itemize}

\section{Technical Skills}
\begin{itemize}[leftmargin=0.15in, label={}]
    \item \small{
        \textbf{Cloud and DevOps}{: Docker, GitLab CI/CD, Kubernetes} \\
        \textbf{Backend}{: Python, FastAPI}
    }
\end{itemize}

\section{Experience}
\resumeSubHeadingListStart
    \resumeSubheading
        {Acme Corp}{Jan 2024 -- Dec 2024}
        {Platform Engineer}{Toronto, ON}
        \resumeItemListStart
            \resumeItem{Cut deploy time 30\% with a \textbf{Python} tool across 50+ services}
            \resumeItem{Ran \textbf{GitLab CI/CD} pipelines with \textbf{Docker} executors on \textbf{Ubuntu} runners}
            \resumeItem{Cut deploy time 45\% with a \textbf{Python} tool}
            \resumeItem{Operated \textbf{Kubernetes} clusters for 50+ services}
            \resumeItem{Built \textbf{Grafana} dashboards for the platform}
            \resumeItem{Led troubleshooting of runner outages with the platform team}
        \resumeItemListEnd
    \resumeSubheading
        {Beta Labs}{Jan 2023 -- Apr 2023}
        {Data Analyst}{Waterloo, ON}
        \resumeItemListStart
            \resumeItem{Built \textbf{Power BI} dashboards on \textbf{SQL Server} for 3 teams}
        \resumeItemListEnd
\resumeSubHeadingListEnd

\section{Projects}
\resumeSubHeadingListStart
    \resumeProjectHeading
        {\textbf{Gamma App} $|$ \emph{FastAPI, PostgreSQL, Grafana, Terraform}}{}
        \resumeItemListStart
            \resumeItem{Built a \textbf{FastAPI} service with \textbf{PostgreSQL} and \textbf{Grafana} dashboards}
        \resumeItemListEnd
\resumeSubHeadingListEnd

\section{Education}
\resumeSubHeadingListStart
    \resumeSubheading
        {Example University}{2024}
        {BSc Physics}{Waterloo, ON}
\resumeSubHeadingListEnd

\end{document}
"""

JD_PROCEED = """# Platform Engineer at Example Co

## Responsibilities
- Build Python tooling and GitLab CI/CD pipelines
- Run Docker workloads on Ubuntu

## Requirements
- Must have Python and Docker
- Experience with FastAPI and PostgreSQL

## Nice to have
- Kubernetes is a plus
"""

JD_REJECT = """# Data Scientist at Other Co

## Requirements
- Must have Rust, Scala and Apache Spark
- Experience with TensorFlow and Kubernetes
- Snowflake and Airflow required

## Nice to have
- Python
"""


def make_workspace(root: Path, master: str = MASTER, template: str = TEMPLATE) -> Path:
    (root / "target_jobs").mkdir(parents=True, exist_ok=True)
    if master is not None:
        (root / "master.md").write_text(master, encoding="utf-8")
    if template is not None:
        (root / "template.tex").write_text(template, encoding="utf-8")
    (root / "target_jobs" / "proceed.md").write_text(JD_PROCEED, encoding="utf-8")
    (root / "target_jobs" / "reject.md").write_text(JD_REJECT, encoding="utf-8")
    return root


def fake_which(available: tuple[str, ...] = ("pdflatex", "pdftotext", "pdfinfo")
               ) -> Callable[[str], str | None]:
    return lambda name: f"/fake/bin/{name}" if name in available else None


class FakeTex:
    """Stand-in for pdflatex/pdfinfo/pdftotext used only by unit tests.

    The fake "PDF" is JSON holding a page count computed from the LaTeX source,
    so the fitting loop can be exercised deterministically. Real PDF behaviour
    is covered separately by the end-to-end test when pdflatex is installed.
    """

    def __init__(self, pages_for: Callable[[str], int] = lambda tex: 1, fail: bool = False,
                 timeout: bool = False, drop_text: bool = False, info_pages_offset: int = 0):
        self.pages_for = pages_for
        self.fail = fail
        self.timeout = timeout
        self.drop_text = drop_text
        self.info_pages_offset = info_pages_offset
        self.compiles = 0
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        pipeline = load_pipeline()
        self.commands.append(list(command))
        exe = Path(command[0]).name
        if exe in ("pdflatex", "xelatex", "lualatex"):
            assert kwargs.get("cwd"), "compiler must run with an explicit working directory"
            assert "timeout" in kwargs, "compiler must run with a timeout"
            if self.timeout:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            outdir = Path(next(a.split("=", 1)[1] for a in command
                               if a.startswith("-output-directory=")))
            job = next(a.split("=", 1)[1] for a in command if a.startswith("-jobname="))
            tex = Path(command[-1]).read_text(encoding="utf-8")
            self.compiles += 1
            log = "This is a fake pdfTeX run\n"
            if self.fail:
                log += "./x.tex:12: Undefined control sequence.\n! Undefined control sequence.\n"
                (outdir / f"{job}.log").write_text(log, encoding="utf-8")
                return subprocess.CompletedProcess(command, 1, "! Undefined control sequence.", "")
            (outdir / f"{job}.log").write_text(log, encoding="utf-8")
            body = tex.split("\\begin{document}", 1)[1]
            payload = {"pages": self.pages_for(tex),
                       "text": "" if self.drop_text else pipeline.latex_to_plain(body)}
            (outdir / f"{job}.pdf").write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "Output written", "")
        if exe == "pdfinfo":
            data = json.loads(Path(command[1]).read_text(encoding="utf-8"))
            pages = data["pages"] + self.info_pages_offset
            return subprocess.CompletedProcess(command, 0, f"Producer: fake\nPages:          "
                                                           f"{pages}\n", "")
        if exe == "pdftotext":
            pdf = next(a for a in command if a.endswith(".pdf"))
            data = json.loads(Path(pdf).read_text(encoding="utf-8"))
            text = data["text"] + "\f" + "".join("overflow text\f"
                                                 for _ in range(data["pages"] - 1))
            if "-bbox-layout" in command:  # one line per page, nothing overlaps
                pages = text.split("\f")[:max(1, data["pages"])]
                body = "".join(
                    f'<page width="612" height="792"><flow><block><line xMin="50" yMin="40" '
                    f'xMax="560" yMax="50"><word xMin="50" yMin="40" xMax="560" yMax="50">'
                    f'{escape(page[:60])}</word></line></block></flow></page>' for page in pages)
                text = (f'<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>{body}</doc>'
                        f'</body></html>')
            return subprocess.CompletedProcess(command, 0, text, "")
        raise AssertionError(f"unexpected command {command}")
