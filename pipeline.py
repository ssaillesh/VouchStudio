#!/usr/bin/env python3
"""Local, deterministic job matching and resume tailoring engine.

Usage:
    python3 pipeline.py target_jobs/devops_role.md [--verbose] [--dry-run]

Pipeline stages (all local, standard library only, no network, no LLM):
    1. Validate the workspace (master profile, template, output directory).
    2. Read and normalize the job description (JD).
    3. Parse the master profile into tagged lines, entries and a skill
       evidence index. ``[NO]`` markers become explicit negative evidence.
    4. Extract JD requirements with section and wording rules, classify them
       (mandatory / required / preferred) and match each against positive
       evidence only.
    5. Score: weighted matched / weighted total, rounded half up. Verdict is
       PROCEED at >= 65, otherwise REJECT - LOW FIT.
    6. Write ``outputs/<company>_<role>_analysis.json``.
    7. On PROCEED: verify every template bullet against the master profile,
       rank the verified bullets against the JD, render a tailored copy of the
       template, compile it with a compatible engine and require a verified
       single-page PDF (pdfinfo + pdftotext), trimming content and then
       tightening spacing within fixed bounds when it overflows.

See CLAUDE.md for the full rule set. Exit codes are listed in EXIT_* below.
"""

from __future__ import annotations

import argparse
import dataclasses
from fractions import Fraction
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

try:
    import resume_kit
except ImportError:  # loaded by file path (tests, Studio): resume_kit.py sits next to this file
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import resume_kit

PIPELINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK = 0                # analysis done; PROCEED resume verified as one page
EXIT_INVALID_INPUT = 2     # bad JD path, extension, empty or malformed JD
EXIT_LOW_FIT = 3           # valid analysis, verdict REJECT - LOW FIT
EXIT_MISSING_PREREQ = 4    # master/template missing or unusable, no compiler
EXIT_FAILED = 5            # analysis, rendering, compilation or output failure
EXIT_PAGE_LIMIT = 6        # compiled, but could not verify exactly one page

# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------
WEIGHT_MANDATORY = 3       # explicit must-have / mandatory / required wording
WEIGHT_REQUIRED = 2        # requirement section, responsibilities, core stack
WEIGHT_PREFERRED = 1       # nice to have / preferred / bonus / a plus
CATEGORY_WEIGHTS = {
    "mandatory": WEIGHT_MANDATORY,
    "required": WEIGHT_REQUIRED,
    "preferred": WEIGHT_PREFERRED,
}
FIT_THRESHOLD = 65
VERDICT_PROCEED = "PROCEED"
VERDICT_REJECT = "REJECT - LOW FIT"

STATUS_MATCHED = "matched"
STATUS_NO = "explicitly_no"
STATUS_NOT_EVIDENCED = "not_evidenced"
STATUS_AMBIGUOUS = "ambiguous"
STATUSES = (STATUS_MATCHED, STATUS_NO, STATUS_NOT_EVIDENCED, STATUS_AMBIGUOUS)

# ---------------------------------------------------------------------------
# Master profile tags
# ---------------------------------------------------------------------------
TAG_VERIFIED = "V"
TAG_REPO = "R"
TAG_OLD = "OLD"
TAG_NO = "NO"
TAG_GAP = "GAP"
TAG_TABLE = "TABLE"        # row of the skills inventory table (positive)
TAG_EXPOSURE = "EXPOSURE"  # row of the exposure-level skills table
POSITIVE_TAGS = frozenset({TAG_VERIFIED, TAG_REPO, TAG_TABLE})
KNOWN_TAGS = (TAG_VERIFIED, TAG_REPO, TAG_OLD, TAG_NO, TAG_GAP)
TAG_PATTERN = re.compile(r"`?\[(V|R|OLD|NO|GAP)\]`?")

MASTER_CANDIDATES = ("master.md", "master_profile.md")
# The resume kit (section 9 of the master profile) is the tailoring engine's main input.
RESUME_KIT_HEADING = re.compile(r"^##\s+9\.\s+Resume kit\b", re.IGNORECASE | re.MULTILINE)
TEMPLATE_NAME = "template.tex"
OUTPUT_DIR_NAME = "outputs"
JD_EXTENSIONS = (".md", ".txt")
MAX_JD_BYTES = 1_000_000
MAX_SLUG_LENGTH = 48

# ---------------------------------------------------------------------------
# Resume selection and fitting constants
# ---------------------------------------------------------------------------
CANDIDATE_BULLETS_PER_ENTRY = 5          # initial candidates per experience/project
CANDIDATE_BULLETS_MINOR = 3              # research / leadership entries
FLOOR_RELEVANT_EXPERIENCE = 3            # bullets kept in an included job (or all it has)
FLOOR_OTHER_EXPERIENCE = 3               # same rule: an included job always shows 3 bullets
COVERAGE_BULLET_BUDGET = 22              # bullets picked before fitting trims to one page
SUMMARY_PROOFS = 3                       # proof clauses in a generated summary
SUMMARY_CLAUSE_CHARS = (25, 90, 120)     # min cut, preferred max, hard max per clause
FLOOR_KEPT_PROJECT = 1                   # bullets kept in a project that stays
BULLETS_REMOVED_PER_ITERATION = 2
REFILL_ATTEMPTS = 8                      # compiles spent restoring over-trimmed units
MAX_FIT_ITERATIONS = 80
LEXICAL_SUPPORT_THRESHOLD = 0.5          # share of bullet stems found in source
NO_CLAIM_OVERLAP_THRESHOLD = 0.6         # share of a [NO] statement echoed
RELEVANCE_REQ_FACTOR = 10
RELEVANCE_METRIC_BONUS = 3
RELEVANCE_OVERLAP_CAP = 6

COMPILE_TIMEOUT_SECONDS = 90
TOOL_TIMEOUT_SECONDS = 30

# Bounded vertical-spacing levels. Level 0 is the untouched template. Only
# section spacing and nested-list spacing change; fonts and margins never do.
SPACING_LEVELS: tuple[dict[str, str], ...] = (
    {},
    {"section": "{0pt}{4pt}{3pt}", "list": "itemsep=0pt"},
    {"section": "{0pt}{3pt}{2pt}", "list": "itemsep=0pt, parsep=0pt, topsep=1pt"},
    {"section": "{0pt}{2pt}{2pt}", "list": "itemsep=-1pt, parsep=0pt, topsep=0pt"},
)

SECTION_PRIORITY = {  # lower is trimmed first
    "leadership": 0,
    "research": 1,
    "projects": 2,
    "experience": 3,
    "education": 4,
    "awards": 5,
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class PipelineError(Exception):
    """Base error carrying an exit code and an actionable message."""

    exit_code = EXIT_FAILED


class InvalidInputError(PipelineError):
    exit_code = EXIT_INVALID_INPUT


class MissingPrerequisiteError(PipelineError):
    exit_code = EXIT_MISSING_PREREQ


class TemplateStructureError(PipelineError):
    exit_code = EXIT_MISSING_PREREQ


class CompilationError(PipelineError):
    exit_code = EXIT_FAILED


class PageLimitError(PipelineError):
    exit_code = EXIT_PAGE_LIMIT


class OutputError(PipelineError):
    exit_code = EXIT_FAILED


class ProvenanceValidationError(PipelineError):
    """A rendered claim cannot be traced to positive master evidence (hard gate)."""

    exit_code = EXIT_FAILED


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SkillSpec:
    key: str
    display: str
    aliases: tuple[str, ...]
    group: str
    domains: tuple[str, ...] = ()
    case_sensitive: tuple[str, ...] = ()
    members: tuple[str, ...] = ()   # category satisfied by these skill keys
    kind: str = "skill"             # skill | category | language


@dataclass
class SkillMention:
    key: str
    start: int
    end: int
    text: str


@dataclass
class MasterLine:
    number: int
    raw: str
    content: str           # text without bullet marker and leading tag
    kind: str              # heading | bold | bullet | paragraph | table | blank
    indent: int = 0
    heading_level: int = 0
    own_tag: str | None = None
    tag: str | None = None  # effective tag after inheritance
    section: str = ""
    entry_id: str | None = None
    meta: bool = False
    table_kind: str | None = None  # skills | exposure | do_not_claim | other
    statement: str = ""    # for [NO] lines: the negated claim (first sentence)
    refs: tuple[str, ...] = ()  # skills-table rows: entry ids named as evidence


@dataclass
class MasterEntry:
    id: str
    title: str
    section: str
    kind: str
    start_line: int
    end_line: int
    key_tokens: tuple[str, ...]


@dataclass
class Occurrence:
    key: str
    line: int
    kind: str              # positive | exposure | no | old | gap | untagged | heading | negated
    entry_id: str | None
    hard_no: bool = False


@dataclass
class MasterProfile:
    path: Path
    lines: list[MasterLine]
    entries: list[MasterEntry]
    occurrences: dict[str, list[Occurrence]]
    global_no: dict[str, list[int]]
    scoped_no: dict[str, list[Occurrence]]
    no_statements: list[tuple[int, str, str | None]]  # (line, text, entry_id)
    warnings: list[str]
    cache: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def line(self, number: int) -> MasterLine:
        return self.lines[number - 1]

    def entry(self, entry_id: str | None) -> MasterEntry | None:
        for item in self.entries:
            if item.id == entry_id:
                return item
        return None


@dataclass
class JDLine:
    number: int
    raw: str
    text: str
    section_type: str      # mandatory | required | preferred | responsibilities | info | intro | unclassified
    section_title: str
    is_heading: bool
    is_bullet: bool


@dataclass
class JobDescription:
    path: Path
    display_path: str
    raw_text: str
    lines: list[JDLine]
    sha256: str
    company: str
    company_source: str
    role: str
    role_source: str
    warnings: list[str]
    partial_description: bool = False


@dataclass
class Requirement:
    name: str
    key: str | None
    kind: str              # skill | skill_category | language | unknown_named | years_experience | education | certification
    category: str          # mandatory | required | preferred
    jd_lines: list[int]
    jd_excerpt: str
    reason: str
    confidence: str = "high"
    status: str = STATUS_NOT_EVIDENCED
    evidence: list[dict[str, Any]] = field(default_factory=list)
    evidence_count: int = 0
    matched_via: str | None = None
    explanation: str = ""
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    members: list[str] = field(default_factory=list)  # skill_group: any one satisfies it

    @property
    def weight(self) -> int:
        return CATEGORY_WEIGHTS[self.category]


@dataclass
class TemplateBullet:
    raw: str
    plain: str
    index: int


@dataclass
class TemplateEntry:
    macro: str
    header_raw: str
    args: list[str]
    bullets: list[TemplateBullet]
    index: int
    has_item_list: bool


@dataclass
class KVLine:
    label_raw: str
    label: str
    items: list[str]


@dataclass
class TemplateSection:
    title: str
    kind: str              # list | keyvalue | paragraph
    raw_body: str
    prefix: str = ""
    suffix: str = ""
    entries: list[TemplateEntry] = field(default_factory=list)
    kv_lines: list[KVLine] = field(default_factory=list)
    kv_wrapper: tuple[str, str] = ("", "")
    paragraph_wrapper: tuple[str, str] = ("", "")
    paragraph_text: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.title) or "section"


@dataclass
class ParsedTemplate:
    path: Path
    preamble: str
    header: str
    sections: list[TemplateSection]
    trailer: str
    engine_requirement: str  # pdflatex | xelatex
    engine_reason: str


@dataclass
class Candidate:
    section_index: int
    entry_index: int
    bullet_index: int
    raw: str
    plain: str
    skills: tuple[str, ...]
    support: float
    source_lines: list[int]
    req_weight: int = 0
    req_hits: tuple[str, ...] = ()
    metric: bool = False
    overlap: int = 0
    parent_index: int | None = None   # a dependent bullet ("Drove it ...") follows this one
    proves: dict[str, Fraction] = field(default_factory=dict)  # coverage term -> credit
    requirement_ids: tuple[str, ...] = ()  # requirements whose terms this bullet proves
    value: float | None = None        # coverage gain when the set-cover pass picked it
    kit_id: str | None = None         # resume kit bullet ID (kit mode)
    impact: int = 0                   # kit impact, 1 to 5
    kit_line: int = 0                 # master line of the kit row
    hooks: frozenset[str] = frozenset()  # terms its kit themes and 9.6 synonyms name

    def worth(self) -> float:
        """Trimming value: coverage gain when selected by coverage, else relevance."""
        return self.relevance if self.value is None else self.value

    @property
    def relevance(self) -> int:
        return (RELEVANCE_REQ_FACTOR * self.req_weight
                + (RELEVANCE_METRIC_BONUS if self.metric else 0)
                + self.overlap)


@dataclass
class EntryPlan:
    section_index: int
    entry_index: int
    master_entry_id: str | None
    candidates: list[Candidate]
    selected: list[Candidate]
    floor: int
    optional: bool
    keep: bool = True
    rejected: list[dict[str, Any]] = field(default_factory=list)
    header_raw: str = ""
    coverage_value: float | None = None

    @property
    def relevance(self) -> int:
        return sum(c.req_weight for c in self.selected) if self.selected else 0

    def worth(self) -> float:
        return self.relevance if self.coverage_value is None else self.coverage_value


@dataclass
class ResumePlan:
    template: ParsedTemplate
    entries: dict[tuple[int, int], EntryPlan]
    section_order: list[int]
    entry_order: dict[int, list[int]]
    summary_text: str | None
    kv_items: dict[tuple[int, int], list[str]]
    summary_sentences: list[str] = field(default_factory=list)
    kv_order: dict[int, list[int]] = field(default_factory=dict)  # skills rows by relevance
    gap_lines: dict[tuple[int, int], list[dict[str, Any]]] = field(default_factory=dict)
    rewrites: list[dict[str, Any]] = field(default_factory=list)
    selection_check: dict[str, Any] = field(default_factory=dict)
    spacing_level: int = 0
    removed: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    template_items_removed: list[dict[str, Any]] = field(default_factory=list)
    kit_mode: bool = False
    considered: list[dict[str, Any]] = field(default_factory=list)  # kit bullets left out, why
    project_ranks: dict[tuple[int, int], int] = field(default_factory=dict)
    project_scores: dict[str, float] = field(default_factory=dict)
    kit_rows: dict[tuple[int, int], int] = field(default_factory=dict)  # skills row -> kit line
    summary_facts: list[dict[str, Any]] = field(default_factory=list)
    summary_source: dict[str, Any] = field(default_factory=dict)
    wrap_probe: dict[str, Any] = field(default_factory=dict)
    floors: dict[tuple[int, int], int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Controlled skill vocabulary
# ---------------------------------------------------------------------------
# Rules:
#   * Aliases are genuine equivalents only (K8s == Kubernetes). Related but
#     different technologies are separate keys (Docker != Kubernetes,
#     AWS != Azure, Ansible != Ansible Vault).
#   * ``members`` turns a key into a category that is satisfied ONLY by
#     positive evidence for one of the listed member keys (Docker satisfies
#     "Containerization"). The reverse never holds: evidence for a category
#     never implies any specific member.
#   * Case-sensitive aliases protect short or common words (Go, React, DAX).
#   * Matching is longest-alias-first, so "Docker Compose" is never also
#     counted as "Docker" and "Ansible Vault" never as "Ansible".
def _skill(key: str, display: str, aliases: str, group: str, domains: str = "",
           cs: str = "", members: str = "", kind: str = "") -> SkillSpec:
    def split(value: str) -> tuple[str, ...]:
        return tuple(part.strip() for part in value.split("|") if part.strip())

    member_keys = split(members)
    return SkillSpec(
        key=key,
        display=display,
        aliases=split(aliases),
        group=group,
        domains=split(domains),
        case_sensitive=split(cs),
        members=member_keys,
        kind=kind or ("category" if member_keys else "skill"),
    )


SKILLS: tuple[SkillSpec, ...] = (
    # Programming and markup languages
    _skill("python", "Python", "python|python3", "backend", "backend|general|data|mlops"),
    _skill("bash", "Bash", "bash|bash scripting", "cloud_devops", "devops"),
    _skill("powershell", "PowerShell", "powershell", "cloud_devops", "devops"),
    _skill("shell_scripting", "Shell scripting", "shell scripting|shell scripts|shell script",
           "cloud_devops", "devops", members="bash|powershell"),
    _skill("scripting", "Scripting", "scripting|scripting languages", "cloud_devops", "devops",
           members="python|bash|powershell|perl|ruby"),
    _skill("java", "Java", "java", "backend", "backend|general"),
    _skill("javascript", "JavaScript", "javascript", "frontend", "fullstack", cs="JS"),
    _skill("typescript", "TypeScript", "typescript", "frontend", "fullstack"),
    _skill("cpp", "C++", "c++|cpp", "backend", "general"),
    _skill("csharp", "C#", "c#|csharp", "backend", "backend"),
    _skill("dotnet", ".NET", ".net|dotnet|asp.net", "backend", "backend"),
    _skill("go", "Go", "golang", "backend", "backend|devops", cs="Go"),
    _skill("rust", "Rust", "", "backend", "backend", cs="Rust"),
    _skill("ruby", "Ruby", "ruby", "backend", "backend"),
    _skill("rails", "Ruby on Rails", "ruby on rails|rails", "backend", "backend|fullstack"),
    _skill("php", "PHP", "php", "backend", "backend"),
    _skill("scala", "Scala", "scala", "data", "data"),
    _skill("kotlin", "Kotlin", "kotlin", "backend", "backend"),
    _skill("swift", "Swift", "", "backend", "general", cs="Swift"),
    _skill("perl", "Perl", "perl", "cloud_devops", "devops"),
    _skill("sql", "SQL", "sql", "databases", "data|backend"),
    _skill("matlab", "MATLAB", "matlab", "ai_ml", "general"),
    _skill("html", "HTML", "html|html5", "frontend", "fullstack"),
    _skill("css", "CSS", "css|css3", "frontend", "fullstack"),
    _skill("yaml", "YAML", "yaml", "cloud_devops", "devops"),
    # Frontend
    _skill("react", "React", "react.js|reactjs", "frontend", "fullstack", cs="React"),
    _skill("nextjs", "Next.js", "next.js|nextjs", "frontend", "fullstack"),
    _skill("vue", "Vue.js", "vue|vue.js|vuejs", "frontend", "fullstack"),
    _skill("angular", "Angular", "angular|angularjs", "frontend", "fullstack"),
    _skill("svelte", "Svelte", "svelte", "frontend", "fullstack"),
    _skill("tailwind", "Tailwind CSS", "tailwind|tailwind css|tailwindcss", "frontend", "fullstack"),
    _skill("vite", "Vite", "vite", "frontend", "fullstack"),
    _skill("frontend_dev", "Frontend development",
           "frontend development|front-end development|front end development", "frontend",
           "fullstack", members="react|vue|angular|svelte|nextjs|html|css|javascript|typescript"),
    # Backend
    _skill("nodejs", "Node.js", "node.js|nodejs", "backend", "backend|fullstack"),
    _skill("express", "Express.js", "express.js|expressjs", "backend", "backend|fullstack"),
    _skill("graphql", "GraphQL", "graphql", "backend", "backend|fullstack"),
    _skill("fastapi", "FastAPI", "fastapi", "backend", "backend"),
    _skill("flask", "Flask", "flask", "backend", "backend"),
    _skill("django", "Django", "django", "backend", "backend|fullstack"),
    _skill("spring", "Spring Boot", "spring boot|spring framework", "backend", "backend"),
    _skill("pydantic", "Pydantic", "pydantic", "backend", "backend"),
    _skill("sqlalchemy", "SQLAlchemy", "sqlalchemy", "backend", "backend"),
    _skill("alembic", "Alembic", "alembic", "backend", "backend"),
    _skill("celery", "Celery", "celery", "backend", "backend"),
    _skill("typer", "Typer", "typer", "backend", "backend"),
    _skill("rest", "REST APIs",
           "rest api|rest apis|restful|restful api|restful apis|restful services|rest services",
           "backend", "backend|fullstack", cs="REST"),
    _skill("grpc", "gRPC", "grpc", "backend", "backend"),
    _skill("microservices", "Microservices", "microservices|microservice architecture", "backend",
           "backend"),
    _skill("task_queues", "Task queues", "task queue|task queues|background jobs|background workers",
           "backend", "backend", members="celery"),
    _skill("kafka", "Apache Kafka", "kafka|apache kafka", "data", "data|backend"),
    _skill("rabbitmq", "RabbitMQ", "rabbitmq", "backend", "backend"),
    _skill("sqs", "AWS SQS", "sqs|amazon sqs|aws sqs", "cloud_devops", "backend"),
    _skill("message_queues", "Message queues",
           "message queue|message queues|message broker|message brokers|event streaming|pub/sub",
           "backend", "backend", members="kafka|rabbitmq|sqs"),
    _skill("caching", "Caching", "caching", "backend", "backend"),
    _skill("rate_limiting", "Rate limiting", "rate limiting|rate-limiting|rate limiter", "backend",
           "backend"),
    _skill("idempotency", "Idempotency", "idempotent|idempotency", "backend", "backend"),
    # Databases and warehouses
    _skill("postgresql", "PostgreSQL", "postgresql|postgres", "databases", "backend|data"),
    _skill("postgis", "PostGIS", "postgis", "databases", "backend"),
    _skill("mysql", "MySQL", "mysql", "databases", "backend|data"),
    _skill("sqlite", "SQLite", "sqlite", "databases", "backend"),
    _skill("sql_server", "SQL Server", "sql server|mssql|microsoft sql server|t-sql", "databases",
           "data"),
    _skill("oracle_db", "Oracle Database", "oracle database|oracle db|pl/sql", "databases", "data"),
    _skill("mongodb", "MongoDB", "mongodb|mongo", "databases", "backend"),
    _skill("redis", "Redis", "redis", "databases", "backend"),
    _skill("dynamodb", "DynamoDB", "dynamodb", "databases", "backend"),
    _skill("cassandra", "Cassandra", "cassandra", "databases", "data"),
    _skill("elasticsearch", "Elasticsearch", "elasticsearch", "databases", "backend|data"),
    _skill("relational_db", "Relational databases",
           "relational database|relational databases|rdbms|sql databases|sql database", "databases",
           "backend|data", members="postgresql|mysql|sqlite|sql_server|oracle_db"),
    _skill("nosql", "NoSQL databases", "nosql", "databases", "backend",
           members="mongodb|redis|dynamodb|cassandra"),
    _skill("snowflake", "Snowflake", "", "data", "data", cs="Snowflake"),
    _skill("bigquery", "BigQuery", "bigquery", "data", "data"),
    _skill("redshift", "Amazon Redshift", "redshift", "data", "data"),
    _skill("databricks", "Databricks", "databricks", "data", "data"),
    # Data engineering and analytics
    _skill("pandas", "pandas", "pandas", "data", "data"),
    _skill("numpy", "NumPy", "numpy", "data", "data"),
    _skill("scipy", "SciPy", "scipy", "data", "data"),
    _skill("quantlib", "QuantLib", "quantlib", "data", "data"),
    _skill("spark", "Apache Spark", "apache spark|pyspark", "data", "data", cs="Spark"),
    _skill("airflow", "Apache Airflow", "airflow|apache airflow", "data", "data"),
    _skill("dbt", "dbt", "", "data", "data", cs="dbt"),
    _skill("hadoop", "Hadoop", "hadoop", "data", "data"),
    _skill("flink", "Apache Flink", "flink|apache flink", "data", "data"),
    _skill("etl", "ETL / data pipelines",
           "etl|elt|etl pipeline|etl pipelines|data pipeline|data pipelines|extract, transform, load",
           "data", "data"),
    _skill("power_bi", "Power BI", "power bi|powerbi", "data", "data"),
    _skill("power_query", "Power Query", "power query", "data", "data"),
    _skill("dax", "DAX", "", "data", "data", cs="DAX"),
    _skill("power_automate", "Power Automate", "power automate", "data", "data"),
    _skill("tableau", "Tableau", "tableau", "data", "data"),
    _skill("looker", "Looker", "looker", "data", "data"),
    _skill("data_viz", "Data visualization", "data visualization|data visualisation", "data", "data",
           members="power_bi|tableau|looker"),
    _skill("bi", "Business intelligence", "business intelligence|bi tools|bi reporting", "data",
           "data", cs="BI", members="power_bi|tableau|looker"),
    _skill("excel", "Excel", "microsoft excel|ms excel", "data", "data", cs="Excel"),
    _skill("sharepoint", "SharePoint", "sharepoint", "tools", ""),
    _skill("sap", "SAP", "", "data", "data", cs="SAP"),
    _skill("statistics", "Statistics", "statistics|statistical analysis", "data", "data|mlops"),
    _skill("linear_algebra", "Linear algebra", "linear algebra", "data", "mlops"),
    _skill("data_analysis", "Data analysis", "data analysis|data analytics", "data", "data",
           members="pandas|sql|power_bi|excel"),
    _skill("dsa", "Data structures and algorithms",
           "data structures and algorithms|data structures|algorithms|dsa", "backend", "general"),
    # Machine learning and AI
    _skill("pytorch", "PyTorch", "pytorch", "ai_ml", "mlops"),
    _skill("tensorflow", "TensorFlow", "tensorflow", "ai_ml", "mlops"),
    _skill("keras", "Keras", "keras", "ai_ml", "mlops"),
    _skill("scikit_learn", "scikit-learn", "scikit-learn|sklearn|scikit learn", "ai_ml",
           "mlops|data"),
    _skill("xgboost", "XGBoost", "xgboost", "ai_ml", "mlops|data"),
    _skill("huggingface", "Hugging Face", "hugging face|huggingface", "ai_ml", "mlops"),
    _skill("finbert", "FinBERT", "finbert", "ai_ml", "mlops"),
    _skill("opencv", "OpenCV", "opencv", "ai_ml", "mlops"),
    _skill("yolo", "YOLO", "yolo|yolov5|yolov8|yolov8n", "ai_ml", "mlops"),
    _skill("roboflow", "Roboflow", "roboflow", "ai_ml", "mlops"),
    _skill("labelimg", "LabelImg", "labelimg", "ai_ml", "mlops"),
    _skill("ppo", "PPO", "ppo|proximal policy optimization", "ai_ml", "mlops"),
    _skill("sb3", "Stable-Baselines3", "stable-baselines3|stable baselines3|stable-baselines|sb3",
           "ai_ml", "mlops"),
    _skill("behavior_cloning", "Behaviour cloning", "behaviour cloning|behavior cloning", "ai_ml",
           "mlops"),
    _skill("machine_learning", "Machine learning", "machine learning", "ai_ml", "mlops", cs="ML",
           members="pytorch|tensorflow|keras|scikit_learn|xgboost|sb3|yolo"),
    _skill("deep_learning", "Deep learning", "deep learning|neural network|neural networks", "ai_ml",
           "mlops", members="pytorch|tensorflow|keras|yolo"),
    _skill("computer_vision", "Computer vision", "computer vision", "ai_ml", "mlops",
           members="opencv|yolo"),
    _skill("reinforcement_learning", "Reinforcement learning",
           "reinforcement learning|deep reinforcement learning", "ai_ml", "mlops", cs="RL",
           members="ppo|sb3"),
    _skill("imitation_learning", "Imitation learning", "imitation learning", "ai_ml", "mlops",
           members="behavior_cloning"),
    _skill("nlp", "NLP", "natural language processing", "ai_ml", "mlops", cs="NLP",
           members="finbert"),
    _skill("sentiment", "Sentiment analysis", "sentiment analysis", "ai_ml", "mlops"),
    _skill("llm", "LLMs",
           "llm|llms|large language model|large language models|llm api|llm apis", "ai_ml",
           "mlops"),
    _skill("genai", "Generative AI", "generative ai|genai|gen ai", "ai_ml", "mlops",
           members="llm"),
    _skill("rag", "RAG", "rag|retrieval-augmented generation|retrieval augmented generation",
           "ai_ml", "mlops"),
    _skill("langchain", "LangChain", "langchain", "ai_ml", "mlops"),
    _skill("llamaindex", "LlamaIndex", "llamaindex|llama index", "ai_ml", "mlops"),
    _skill("ollama", "Ollama", "ollama", "ai_ml", "mlops"),
    _skill("openai_api", "OpenAI API", "openai api|openai", "ai_ml", "mlops"),
    _skill("groq", "Groq", "groq", "ai_ml", "mlops"),
    _skill("gemini", "Gemini", "", "ai_ml", "mlops", cs="Gemini"),
    _skill("claude", "Claude", "claude code", "ai_ml", "mlops", cs="Claude"),
    _skill("prompt_engineering", "Prompt engineering", "prompt engineering", "ai_ml", "mlops"),
    _skill("faiss", "FAISS", "faiss", "ai_ml", "mlops"),
    _skill("pinecone", "Pinecone", "pinecone", "ai_ml", "mlops"),
    _skill("pgvector", "pgvector", "pgvector", "ai_ml", "mlops"),
    _skill("chroma", "ChromaDB", "chromadb|chroma db", "ai_ml", "mlops"),
    _skill("weaviate", "Weaviate", "weaviate", "ai_ml", "mlops"),
    _skill("milvus", "Milvus", "milvus", "ai_ml", "mlops"),
    _skill("qdrant", "Qdrant", "qdrant", "ai_ml", "mlops"),
    _skill("vector_search", "Vector search",
           "vector search|vector database|vector databases|vector store|vector stores|vector db"
           "|similarity search", "ai_ml", "mlops",
           members="faiss|pinecone|pgvector|chroma|weaviate|milvus|qdrant"),
    _skill("mlflow", "MLflow", "mlflow", "ai_ml", "mlops"),
    _skill("kubeflow", "Kubeflow", "kubeflow", "ai_ml", "mlops"),
    _skill("sagemaker", "Amazon SageMaker", "sagemaker", "ai_ml", "mlops"),
    _skill("vertex_ai", "Vertex AI", "vertex ai", "ai_ml", "mlops"),
    _skill("wandb", "Weights & Biases", "weights & biases|weights and biases|wandb", "ai_ml",
           "mlops"),
    _skill("mlops", "MLOps", "mlops|ml ops|ml infrastructure|machine learning operations", "ai_ml",
           "mlops", members="mlflow|kubeflow|sagemaker|vertex_ai|wandb"),
    _skill("experiment_tracking", "Experiment tracking", "experiment tracking", "ai_ml", "mlops",
           members="mlflow|wandb"),
    _skill("model_registry", "Model registry", "model registry", "ai_ml", "mlops"),
    _skill("gpu", "GPU computing", "gpu|gpus", "ai_ml", "mlops"),
    _skill("cuda", "CUDA", "cuda", "ai_ml", "mlops"),
    _skill("kalman", "Kalman filtering",
           "kalman filter|kalman filters|extended kalman filter|kalman filtering|ekf", "ai_ml", ""),
    _skill("sensor_fusion", "Sensor fusion", "sensor fusion", "ai_ml", ""),
    _skill("lidar", "LiDAR", "lidar", "hardware", ""),
    _skill("carla", "CARLA", "", "ai_ml", "", cs="CARLA"),
    _skill("ros", "ROS", "ros 2|ros2|rosbag", "hardware", "", cs="ROS"),
    _skill("can_bus", "CAN bus", "can bus|canbus", "hardware", ""),
    _skill("bazel", "Bazel", "bazel", "cloud_devops", "general"),
    _skill("googletest", "GoogleTest", "googletest|gtest", "testing", "qa"),
    _skill("catch2", "Catch2", "catch2", "testing", "qa"),
    # Cloud, containers, infrastructure
    _skill("aws", "AWS", "aws|amazon web services", "cloud_devops", "devops",
           members="ec2|s3|ecs|eks|rds|cdk|lambda|cloudformation|cloudwatch|sqs|dynamodb"
                   "|redshift|sagemaker"),
    _skill("ec2", "AWS EC2", "ec2|amazon ec2|aws ec2", "cloud_devops", "devops"),
    _skill("s3", "AWS S3", "s3|amazon s3|aws s3", "cloud_devops", "devops"),
    _skill("ecs", "AWS ECS", "ecs|amazon ecs|aws ecs", "cloud_devops", "devops"),
    _skill("eks", "AWS EKS", "eks|amazon eks|aws eks", "cloud_devops", "devops"),
    _skill("rds", "AWS RDS", "rds|amazon rds|aws rds", "cloud_devops", "devops"),
    _skill("cdk", "AWS CDK", "aws cdk|cdk", "cloud_devops", "devops"),
    _skill("lambda", "AWS Lambda", "aws lambda", "cloud_devops", "devops", cs="Lambda"),
    _skill("cloudformation", "CloudFormation", "cloudformation", "cloud_devops", "devops"),
    _skill("iam", "IAM", "aws iam|identity and access management", "cloud_devops",
           "security|devops", cs="IAM"),
    _skill("cloudwatch", "CloudWatch", "cloudwatch", "observability_security", "devops"),
    _skill("vpc", "VPC", "vpc|vpcs", "cloud_devops", "devops"),
    _skill("azure", "Azure", "azure|microsoft azure", "cloud_devops", "devops",
           members="azure_devops|bicep"),
    _skill("azure_devops", "Azure DevOps", "azure devops|azure pipelines", "cloud_devops", "devops"),
    _skill("bicep", "Bicep", "", "cloud_devops", "devops", cs="Bicep"),
    _skill("gcp", "GCP", "gcp|google cloud|google cloud platform", "cloud_devops", "devops",
           members="bigquery|vertex_ai"),
    _skill("cloud", "Cloud platforms",
           "cloud platform|cloud platforms|cloud computing|cloud infrastructure|public cloud"
           "|cloud services|cloud provider|cloud providers", "cloud_devops", "devops",
           members="aws|azure|gcp"),
    _skill("docker", "Docker", "docker", "cloud_devops", "devops"),
    _skill("docker_compose", "Docker Compose", "docker compose|docker-compose", "cloud_devops",
           "devops"),
    _skill("podman", "Podman", "podman", "cloud_devops", "devops"),
    _skill("containers", "Containerization",
           "containerization|containerisation|containers|containerized|container", "cloud_devops",
           "devops", members="docker|podman"),
    _skill("kubernetes", "Kubernetes", "kubernetes|k8s", "cloud_devops", "devops"),
    _skill("helm", "Helm", "", "cloud_devops", "devops", cs="Helm"),
    _skill("argocd", "Argo CD", "argo cd|argocd", "cloud_devops", "devops"),
    _skill("openshift", "OpenShift", "openshift", "cloud_devops", "devops"),
    _skill("kustomize", "Kustomize", "kustomize", "cloud_devops", "devops"),
    _skill("network_policies", "Kubernetes network policies", "network policies|network policy",
           "cloud_devops", "devops|security"),
    _skill("nomad", "Nomad", "", "cloud_devops", "devops", cs="Nomad"),
    _skill("orchestration", "Container orchestration", "container orchestration", "cloud_devops",
           "devops", members="kubernetes|openshift|ecs|eks|nomad"),
    _skill("terraform", "Terraform", "terraform", "cloud_devops", "devops"),
    _skill("pulumi", "Pulumi", "pulumi", "cloud_devops", "devops"),
    _skill("iac", "Infrastructure as code", "infrastructure as code|infrastructure-as-code",
           "cloud_devops", "devops", cs="IaC",
           members="terraform|pulumi|cloudformation|bicep|cdk"),
    _skill("ansible", "Ansible", "ansible", "cloud_devops", "devops"),
    _skill("ansible_vault", "Ansible Vault", "ansible vault", "observability_security",
           "devops|security"),
    _skill("chef", "Chef", "", "cloud_devops", "devops", cs="Chef"),
    _skill("puppet", "Puppet", "", "cloud_devops", "devops", cs="Puppet"),
    _skill("saltstack", "SaltStack", "saltstack|salt stack", "cloud_devops", "devops"),
    _skill("config_mgmt", "Configuration management",
           "configuration management|config management", "cloud_devops", "devops",
           members="ansible|chef|puppet|saltstack"),
    _skill("packer", "Packer", "", "cloud_devops", "devops", cs="Packer"),
    _skill("vagrant", "Vagrant", "vagrant", "cloud_devops", "devops"),
    _skill("gitlab_ci", "GitLab CI/CD",
           "gitlab ci/cd|gitlab ci|gitlab-ci|.gitlab-ci.yml|gitlab pipelines|gitlab runner"
           "|gitlab runners", "cloud_devops", "devops"),
    _skill("gitlab", "GitLab", "gitlab", "cloud_devops", "devops"),
    _skill("github_actions", "GitHub Actions", "github actions", "cloud_devops", "devops"),
    _skill("github", "GitHub", "github", "tools", ""),
    _skill("jenkins", "Jenkins", "jenkins", "cloud_devops", "devops"),
    _skill("circleci", "CircleCI", "circleci|circle ci", "cloud_devops", "devops"),
    _skill("travis", "Travis CI", "travis ci|travis-ci", "cloud_devops", "devops"),
    _skill("teamcity", "TeamCity", "teamcity", "cloud_devops", "devops"),
    _skill("cicd", "CI/CD",
           "ci/cd|ci / cd|cicd|ci-cd|continuous integration|continuous delivery"
           "|continuous deployment|ci pipelines|ci/cd pipelines|build pipelines"
           "|deployment pipelines", "cloud_devops", "devops",
           members="gitlab_ci|github_actions|jenkins|circleci|azure_devops|argocd|travis|teamcity"),
    _skill("git", "Git", "git", "tools", "general"),
    _skill("svn", "SVN", "svn|subversion", "tools", ""),
    _skill("version_control", "Version control", "version control|source control|version-control",
           "tools", "general", members="git|svn"),
    _skill("artifactory", "JFrog Artifactory", "jfrog artifactory|artifactory|jfrog",
           "cloud_devops", "devops"),
    _skill("nexus", "Sonatype Nexus", "sonatype nexus|nexus repository", "cloud_devops", "devops"),
    _skill("artifact_mgmt", "Artifact management",
           "artifact management|artifact repository|artifact repositories|artifact registry"
           "|binary repository", "cloud_devops", "devops", members="artifactory|nexus"),
    _skill("maven", "Maven", "maven|apache maven", "cloud_devops", "backend|devops"),
    _skill("gradle", "Gradle", "gradle", "cloud_devops", "backend"),
    _skill("linux", "Linux", "linux|gnu/linux", "os", "devops"),
    _skill("rhel", "RHEL", "rhel|red hat enterprise linux|red hat linux", "os", "devops"),
    _skill("ubuntu", "Ubuntu", "ubuntu", "os", "devops"),
    _skill("centos", "CentOS", "centos", "os", "devops"),
    _skill("debian", "Debian", "debian", "os", "devops"),
    _skill("unix", "Unix", "unix", "os", "devops"),
    _skill("windows_server", "Windows Server", "windows server", "os", "devops"),
    _skill("active_directory", "Active Directory", "active directory", "os", "devops"),
    _skill("dns", "DNS", "", "os", "devops", cs="DNS"),
    _skill("tcp_ip", "TCP/IP", "tcp/ip", "os", "devops"),
    _skill("firewalls", "Firewalls", "firewall|firewalls", "os", "devops|security"),
    _skill("networking", "Networking",
           "networking|computer networking|network engineering|network administration", "os",
           "devops"),
    _skill("ssh", "SSH", "ssh", "os", "devops"),
    _skill("cron", "cron", "cron|cron job|cron jobs|crontab", "os", "devops"),
    _skill("nginx", "Nginx", "nginx", "cloud_devops", "devops|backend"),
    _skill("load_balancing", "Load balancing", "load balancing|load balancer|load balancers",
           "cloud_devops", "devops"),
    _skill("vercel", "Vercel", "vercel", "cloud_devops", "fullstack"),
    _skill("heroku", "Heroku", "heroku", "cloud_devops", "backend"),
    # Observability, reliability and security
    _skill("prometheus", "Prometheus", "prometheus", "observability_security", "devops"),
    _skill("grafana", "Grafana", "grafana", "observability_security", "devops"),
    _skill("datadog", "Datadog", "datadog", "observability_security", "devops"),
    _skill("splunk", "Splunk", "splunk", "observability_security", "devops|security"),
    _skill("new_relic", "New Relic", "new relic", "observability_security", "devops"),
    _skill("elk", "ELK Stack", "elk|elk stack|elastic stack", "observability_security", "devops"),
    _skill("logstash", "Logstash", "logstash", "observability_security", "devops"),
    _skill("kibana", "Kibana", "kibana", "observability_security", "devops"),
    _skill("opentelemetry", "OpenTelemetry", "opentelemetry|otel", "observability_security",
           "devops"),
    _skill("pagerduty", "PagerDuty", "pagerduty", "observability_security", "devops"),
    _skill("monitoring", "Monitoring and observability",
           "monitoring|observability|monitoring and alerting|telemetry",
           "observability_security", "devops",
           members="prometheus|grafana|datadog|splunk|new_relic|elk|opentelemetry|cloudwatch"),
    _skill("logging", "Centralized logging",
           "log aggregation|centralized logging|centralised logging|logging pipeline"
           "|logging pipelines", "observability_security", "devops", members="elk|splunk|datadog"),
    _skill("slo", "SLOs / SLIs",
           "slo|slos|sli|slis|service level objectives|service level indicators|error budget"
           "|error budgets", "observability_security", "devops"),
    _skill("on_call", "On-call", "on-call|on call", "observability_security", "devops"),
    _skill("incident", "Incident response",
           "incident response|incident management|incident calls|production incident"
           "|production incidents|incident handling", "observability_security", "devops"),
    _skill("troubleshooting", "Troubleshooting",
           "troubleshooting|troubleshoot|debugging|root cause analysis|root cause|root-cause",
           "tools", "devops|general"),
    _skill("release_mgmt", "Release management", "release management|release engineering",
           "cloud_devops", "devops"),
    _skill("sonarqube", "SonarQube", "sonarqube|sonar", "observability_security",
           "security|devops"),
    _skill("sast", "SAST", "sast|static application security testing", "observability_security",
           "security"),
    _skill("dast", "DAST", "dast|dynamic application security testing", "observability_security",
           "security"),
    _skill("snyk", "Snyk", "snyk", "observability_security", "security"),
    _skill("trivy", "Trivy", "trivy", "observability_security", "security"),
    _skill("devsecops", "DevSecOps", "devsecops", "observability_security", "security|devops",
           members="sast|dast|sonarqube|snyk|trivy"),
    _skill("vuln_scanning", "Vulnerability scanning",
           "vulnerability scanning|vulnerability scans|vulnerability management|security scanning",
           "observability_security", "security", members="sonarqube|sast|dast|snyk|trivy"),
    _skill("delinea", "Delinea Secret Server",
           "delinea secret server|delinea|thycotic secret server", "observability_security",
           "security"),
    _skill("vault", "HashiCorp Vault", "hashicorp vault", "observability_security",
           "security|devops", cs="Vault"),
    _skill("secrets_mgmt", "Secrets management",
           "secrets management|secret management|secrets rotation|secret rotation"
           "|credential rotation|token rotation", "observability_security", "security|devops",
           members="delinea|vault"),
    _skill("rbac", "RBAC", "rbac|role-based access control|role based access control",
           "observability_security", "security"),
    _skill("jwt", "JWT", "jwt|json web token|json web tokens", "backend", "security|backend"),
    _skill("oauth", "OAuth", "oauth|oauth2|oauth 2.0", "backend", "security|backend"),
    _skill("bcrypt", "bcrypt", "bcrypt", "backend", "security"),
    _skill("encryption", "Encryption", "encryption", "observability_security", "security"),
    _skill("hl7", "HL7", "hl7", "backend", ""),
    _skill("fhir", "FHIR", "", "backend", "", cs="FHIR"),
    # Testing and quality
    _skill("pytest", "pytest", "pytest", "testing", "qa"),
    _skill("unit_testing", "Unit testing", "unit testing|unit tests|unit test", "testing",
           "qa|general"),
    _skill("integration_testing", "Integration testing", "integration testing|integration tests",
           "testing", "qa"),
    _skill("regression_testing", "Regression testing", "regression testing|regression tests",
           "testing", "qa"),
    _skill("selenium", "Selenium", "selenium", "testing", "qa"),
    _skill("playwright", "Playwright", "playwright", "testing", "qa"),
    _skill("cypress", "Cypress", "cypress", "testing", "qa"),
    _skill("junit", "JUnit", "junit", "testing", "qa"),
    _skill("jest", "Jest", "", "testing", "qa", cs="Jest"),
    _skill("test_automation", "Test automation",
           "test automation|automated testing|automated tests|test frameworks", "testing", "qa",
           members="pytest|selenium|playwright|cypress|junit|jest"),
    _skill("regression_model", "Regression modeling",
           "regression|regression model|regression models|regression analysis|linear regression"
           "|logistic regression", "ai_ml", "data|mlops"),
    _skill("mypy", "mypy", "mypy", "testing", "general"),
    _skill("ruff", "Ruff", "ruff", "testing", "general"),
    _skill("qa_practice", "Quality assurance", "quality assurance", "testing", "qa"),
    _skill("gmp", "GMP", "gmp|good manufacturing practice", "process", ""),
    # Process and collaboration tools
    _skill("code_review", "Code review",
           "code review|code reviews|reviewing code|peer review|peer reviews|pull request reviews",
           "process", "general"),
    _skill("agile", "Agile", "agile|scrum|sprints|sprint planning|kanban", "process", "general"),
    _skill("jira", "Jira", "jira|jira service management", "tools", ""),
    _skill("confluence", "Confluence", "confluence", "tools", ""),
    _skill("servicenow", "ServiceNow", "servicenow", "tools", "devops"),
    _skill("documentation", "Technical documentation",
           "technical documentation|runbook|runbooks|technical writing", "tools", ""),
    # Hardware and mechanical
    _skill("solidworks", "SolidWorks", "solidworks", "hardware", ""),
    _skill("soldering", "Soldering", "soldering|soldered", "hardware", ""),
    _skill("pcb", "PCB design and repair", "pcb|pcbs|printed circuit board|printed circuit boards",
           "hardware", ""),
    # Spoken languages
    _skill("english", "English", "", "spoken", "", cs="English", kind="language"),
    _skill("german", "German", "", "spoken", "", cs="German", kind="language"),
    _skill("french", "French", "", "spoken", "", cs="French", kind="language"),
    _skill("spanish", "Spanish", "", "spoken", "", cs="Spanish", kind="language"),
    _skill("mandarin", "Mandarin", "", "spoken", "", cs="Mandarin", kind="language"),
)

SKILL_INDEX: dict[str, SkillSpec] = {spec.key: spec for spec in SKILLS}

# ---------------------------------------------------------------------------
# Domain taxonomy (reported separately; never used as skill evidence)
# ---------------------------------------------------------------------------
DOMAIN_UNKNOWN = "Unknown / mixed"
DOMAIN_TITLE_WEIGHT = 6
DOMAIN_BODY_CAP = 15
DOMAIN_MIN_SCORE = 6
DOMAIN_SECONDARY_RATIO = 0.4
DOMAIN_SECONDARY_MIN = 4

DOMAINS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("devops", "DevOps / SRE",
     ("devops", "dev ops", "sre", "site reliability", "platform engineer", "platform engineering",
      "infrastructure engineer", "infrastructure", "cloud engineer", "cloud operations",
      "release engineer", "build engineer", "reliability"),
     ("deploy", "deployment", "deployments", "infrastructure", "uptime", "reliability", "on-call",
      "incident", "incidents", "ci/cd", "pipelines", "monitoring", "observability",
      "provisioning", "automation", "release", "runbooks", "availability", "scalability",
      "capacity", "containers", "orchestration", "cloud")),
    ("mlops", "ML infrastructure / MLOps",
     ("mlops", "ml ops", "ml infrastructure", "machine learning engineer", "ml engineer",
      "ml platform", "ai infrastructure", "ai engineer", "ai/ml", "deep learning",
      "computer vision engineer", "ai platform"),
     ("model training", "model deployment", "model serving", "inference", "training pipelines",
      "ml pipelines", "feature store", "machine learning", "models", "llm", "llms", "gpu",
      "experiment tracking", "model registry", "evaluation")),
    ("fullstack", "Full-stack engineering",
     ("full stack", "full-stack", "fullstack"),
     ("frontend", "front-end", "ui", "user interface", "full stack", "web application",
      "web applications", "ux", "components", "responsive")),
    ("backend", "Backend engineering",
     ("backend", "back-end", "back end", "api engineer", "server-side", "server engineer"),
     ("api", "apis", "backend", "back-end", "services", "microservices", "database",
      "databases", "server", "scalable", "distributed systems", "endpoints", "business logic")),
    ("data", "Data engineering / analytics",
     ("data engineer", "data analyst", "analytics engineer", "analytics", "business intelligence",
      "bi developer", "bi analyst", "data platform", "data scientist"),
     ("etl", "data pipeline", "data pipelines", "data warehouse", "analytics", "dashboards",
      "reporting", "data modeling", "data quality", "kpi", "kpis", "insights", "data models")),
    ("qa", "QA / test automation",
     ("qa", "quality assurance", "test automation", "sdet", "test engineer", "quality engineer",
      "automation tester", "software tester", "tester"),
     ("test cases", "test plans", "testing", "quality", "regression", "bugs", "defects",
      "test coverage", "manual testing")),
    ("security", "Security engineering",
     ("security", "appsec", "application security", "devsecops", "cybersecurity", "infosec"),
     ("security", "vulnerability", "vulnerabilities", "threat", "compliance", "penetration",
      "encryption", "secrets", "soc 2", "audit")),
    ("general", "General software engineering",
     ("software engineer", "software developer", "swe", "developer", "programmer", "new grad"),
     ("software", "code", "coding", "object-oriented", "design patterns", "features",
      "collaborate", "product")),
)
DOMAIN_LABELS = {key: label for key, label, _t, _b in DOMAINS}

# ---------------------------------------------------------------------------
# JD section and wording rules
# ---------------------------------------------------------------------------
# Heading classification is checked in this order; the first match wins, so
# "Preferred Qualifications" is preferred even though it says "qualifications".
HEADING_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("info", ("about us", "about the company", "who we are", "benefits", "perks", "compensation",
              "salary", "pay range", "equal opportunity", "eeo", "how to apply", "our values",
              "why join", "why work", "what we offer", "life at", "diversity", "accommodation",
              "location", "working here")),
    ("preferred", ("nice to have", "nice-to-have", "preferred", "bonus", "plus", "desired",
                   "good to have", "extra credit", "would be great", "ideally", "desirable",
                   "not required")),
    ("mandatory", ("must have", "must-have", "mandatory", "hard requirements", "non-negotiable",
                   "essential", "minimum qualifications", "minimum requirements",
                   "basic qualifications")),
    ("required", ("requirements", "required", "qualifications", "what you bring",
                  "what we're looking for", "what we are looking for", "you have", "you bring",
                  "what you'll need", "what you need", "who you are", "skills", "experience",
                  "about you", "you might be", "your background", "tech stack", "our stack",
                  "technologies", "good fit", "ideal candidate", "you'll thrive",
                  "what we look for", "we look for", "looking for")),
    ("responsibilities", ("responsibilities", "what you'll do", "what you will do", "the role",
                          "about the role", "duties", "day to day", "day-to-day", "your impact",
                          "what you'll work on", "in this role", "the job", "your role",
                          "what you will work on", "the opportunity")),
)
SECTION_DEFAULT_CATEGORY = {
    "mandatory": "mandatory",
    "required": "required",
    "responsibilities": "required",
    "preferred": "preferred",
    "intro": "required",
    "unclassified": "required",
}
LOW_CONFIDENCE_SECTIONS = frozenset({"intro", "unclassified"})

MANDATORY_MARKERS = re.compile(
    r"\b(must[- ]have|must|required|requires|requirement|mandatory|essential|"
    r"need to have|necessary|minimum of)\b", re.IGNORECASE)
PREFERRED_MARKERS = re.compile(
    r"\b(nice[- ]to[- ]have|preferred|preferably|bonus|a plus|is a plus|are a plus|"
    r"desirable|desired|ideally|good to have|advantageous|an asset|is an asset)\b|"
    r"\bplus\b(?=\W*$)", re.IGNORECASE)
NOT_REQUIRED_PATTERN = re.compile(
    r"\b(not required|not necessary|no (?:prior )?experience (?:is )?(?:required|necessary)|"
    r"not a requirement|isn't required|is not required)\b", re.IGNORECASE)
NEGATION_CUES = re.compile(
    r"\b(no|not|never|without|cannot|can't|don't|doesn't|didn't|isn't|wasn't|none|neither|"
    r"nor|lacks|unable|n't)\b", re.IGNORECASE)
YEARS_PATTERN = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*years?\b", re.IGNORECASE)
DEGREE_PATTERN = re.compile(
    r"\b(bachelor'?s?|b\.?sc?\.?|b\.?eng|bs/ms|master'?s|m\.?sc|ph\.?d|doctorate|"
    r"undergraduate degree|university degree|degree)\b", re.IGNORECASE)
CERT_PATTERN = re.compile(r"\b(certification|certifications|certified|certificate)\b",
                          re.IGNORECASE)

STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had
has have having he her here hers him his how i if in into is it its itself just me more most my
no nor not of off on once only or other our ours out over own same she should so some such than
that the their theirs them then there these they this those through to too under until up very
was we were what when where which while who whom why will with would you your yours yourself
etc e g ie eg via per using use used within across every each into onto upon he his him
""".split())
COMMON_CAPITALIZED = frozenset("""
We You Our The A An In On At For With And Or Of To As By Is Are Be This That These Those It Its
Experience Strong Excellent Ability Knowledge Understanding Familiarity Proficiency Proven
Demonstrated Solid Good Great Working Hands Deep Bachelor Master Degree Years Year Plus Bonus
Preferred Required Requirements Responsibilities Qualifications Nice Must Have Skills About Role
Team Teams Work Build Design Develop Develops Own Lead Help Support Collaborate Partner Drive
Manage Maintain Improve Write Create Ensure Participate Contribute Communicate Communication
Remote Hybrid Onsite Office Full Time Part Senior Junior Staff Principal Intermediate Engineer
Engineering Developer Software Systems System Data Platform Infrastructure Cloud Security Product
Customer Customers Business Technical Technology Technologies Tools Tool Services Service
Ideally Including Such Other Also Any All Some New Using Use Familiar Comfortable Passion
Interest Interested Bachelors Masters Equivalent Related Field Computer Science Canada Toronto
Ontario United States USA US Remote Monday Friday January February March April May June July
August September October November December Apply Join Benefits Salary Who What Why How When
Where If Your Their They He She Must Should Will Can May Might Etc Eg Ie Inc Ltd LLC Corp Co
""".split())


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------
_PUNCT_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    "−": "-", " ": " ", "•": "-", "●": "-", "▪": "-", "‣": "-",
    "⁃": "-", "·": "-", "\t": " ", "​": "",
})


def normalize_text(text: str) -> str:
    """NFKC-normalize, unify quotes/dashes/bullets, strip trailing spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT_TRANSLATION)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slugify(text: str, max_length: int = MAX_SLUG_LENGTH) -> str:
    """Filesystem-safe slug: lowercase ASCII letters, digits and single dashes."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if len(text) > max_length:
        text = text[:max_length].rstrip("-")
    return text


def stem(word: str) -> str:
    """Tiny deterministic suffix stemmer used for lexical overlap only."""
    word = word.lower()
    for suffix in ("ations", "ation", "ments", "ment", "ings", "ing", "ions", "ion", "edly",
                   "ers", "er", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiousz":
                word = word[:-1]  # modelling -> model, planned -> plan
            break
    if len(word) > 4 and word.endswith(("at", "e", "i")):
        word = word[:-2] if word.endswith("at") else word[:-1]
    return word


def word_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9+#]*", text)


def content_stems(text: str) -> set[str]:
    """Stems of non-stopword words with at least three characters."""
    return {stem(tok) for tok in word_tokens(text)
            if len(tok) >= 3 and tok.lower() not in STOPWORDS}


def redact_personal(text: str) -> str:
    """Mask e-mail addresses and phone numbers in evidence excerpts."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    return re.sub(r"\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}", "[phone]", text)


def excerpt(text: str, limit: int = 220) -> str:
    text = redact_personal(collapse_ws(text.replace("`", "").replace("**", "")))
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


MARKDOWN_LINK_TARGET = re.compile(r"\]\((?:[^()\s]|\([^()\s]*\))*\)")
BARE_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)


def strip_links(text: str) -> str:
    """Drop link targets and bare URLs: '.../request.html' never asks for HTML."""
    return BARE_URL.sub(" ", MARKDOWN_LINK_TARGET.sub("]", text))


def split_clauses(text: str) -> list[str]:
    parts = re.split(r";|(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Skill detection
# ---------------------------------------------------------------------------
class SkillFinder:
    """Longest-alias-first, boundary-aware detection of vocabulary skills."""

    _LEFT = r"(?<![A-Za-z0-9_])"
    _RIGHT = r"(?![A-Za-z0-9_])"

    def __init__(self, skills: Sequence[SkillSpec]) -> None:
        self.ci_lookup: dict[str, str] = {}
        self.cs_lookup: dict[str, str] = {}
        for spec in skills:
            if spec.kind != "language" and spec.display not in spec.case_sensitive:
                self.ci_lookup.setdefault(collapse_ws(spec.display.lower()), spec.key)
            for alias in spec.aliases:
                self.ci_lookup.setdefault(collapse_ws(alias.lower()), spec.key)
            for alias in spec.case_sensitive:
                self.cs_lookup.setdefault(collapse_ws(alias), spec.key)
        # Display names that are short common words must stay case-sensitive.
        for alias in list(self.ci_lookup):
            if alias in {"go", "rust", "swift", "helm", "chef", "puppet", "packer", "nomad",
                         "bicep", "jest", "dbt", "carla", "fhir", "dns", "dax", "sap", "react",
                         "claude", "gemini", "snowflake", "spark", "vault", "lambda", "rl", "ml",
                         "bi", "iam", "nlp", "iac", "js", "ros", "rest"}:
                del self.ci_lookup[alias]
        self.ci_regex = self._compile(self.ci_lookup, re.IGNORECASE)
        self.cs_regex = self._compile(self.cs_lookup, 0)

    def _compile(self, lookup: dict[str, str], flags: int) -> re.Pattern[str] | None:
        if not lookup:
            return None
        aliases = sorted(lookup, key=lambda a: (-len(a), a))
        body = "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in aliases)
        return re.compile(f"{self._LEFT}(?:{body}){self._RIGHT}", flags)

    def find(self, text: str) -> list[SkillMention]:
        found: list[SkillMention] = []
        for regex, lookup, lower in ((self.ci_regex, self.ci_lookup, True),
                                     (self.cs_regex, self.cs_lookup, False)):
            if regex is None:
                continue
            for match in regex.finditer(text):
                token = collapse_ws(match.group(0))
                key = lookup.get(token.lower() if lower else token)
                if key is None:
                    continue
                if key in {"go", "react"} and re.match(r"\s*(?:-to\b|to\b)", text[match.end():]):
                    continue  # "Go-to market", "React to incidents"
                if key == "excel" and re.match(r"\s+(?:in|at)\b", text[match.end():]):
                    continue  # "Excel in fast-paced environments" is the verb
                found.append(SkillMention(key, match.start(), match.end(), match.group(0)))
        found.sort(key=lambda m: (m.start, -(m.end - m.start), m.key))
        accepted: list[SkillMention] = []
        for mention in found:
            if any(mention.start < other.end and other.start < mention.end for other in accepted):
                continue
            accepted.append(mention)
        return accepted

    def keys(self, text: str) -> list[str]:
        seen: list[str] = []
        for mention in self.find(text):
            if mention.key not in seen:
                seen.append(mention.key)
        return seen


SKILL_FINDER = SkillFinder(SKILLS)


def mention_is_negated(text: str, mention: SkillMention) -> bool:
    """True when a negation cue precedes the mention inside the same clause."""
    start = max(text.rfind(sep, 0, mention.start) for sep in (";", ".", ",", ":", "(", "\n"))
    clause = text[start + 1: mention.start]
    return bool(NEGATION_CUES.search(clause))


# ---------------------------------------------------------------------------
# Master profile parsing
# ---------------------------------------------------------------------------
META_SECTION_PATTERN = re.compile(r"legend|interview log|remaining open items|open items|"
                                  r"resume kit",
                                  re.IGNORECASE)
EXPLANATORY_ASIDE = re.compile(r"\b(only|via|e\.g|i\.e|such as|see|except)\b", re.IGNORECASE)
MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DATE_RANGE_PATTERN = re.compile(
    r"\b([A-Z][a-z]{2})[a-z]*\.?\s+(\d{4})\s*(?:to|-|--|–)\s*([A-Z][a-z]{2})[a-z]*\.?\s+(\d{4})")


def _section_kind(title: str) -> str:
    lowered = title.lower()
    for needle, kind in (("experience", "experience"), ("project", "project"),
                         ("research", "research"), ("education", "education"),
                         ("leadership", "leadership"), ("skills", "skills"),
                         ("identity", "identity"), ("do-not-claim", "do_not_claim"),
                         ("do not claim", "do_not_claim")):
        if needle in lowered:
            return kind
    return "other"


def _strip_heading_number(title: str) -> tuple[str, str]:
    match = re.match(r"^\s*(\d+[a-z]?(?:\.\d+)*)\.?\s+(.*)$", title)
    if match:
        return match.group(1), match.group(2).strip()
    return "", title.strip()


def _entry_key_tokens(title: str) -> tuple[str, ...]:
    head = re.split(r"[:(]", title, maxsplit=1)[0]
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", head)]
    generic = {"inc", "ltd", "corp", "the", "and", "other", "notes", "site"}
    words = [w for w in words if w not in generic and w not in STOPWORDS]
    return tuple(words[:1])


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?\"])\s+(?=[A-Z\"`])", text.strip(), maxsplit=1)
    return parts[0] if parts else text


def _strip_markup(text: str) -> str:
    return TAG_PATTERN.sub("", text).replace("**", "").replace("`", "").strip()


def classify_master_lines(raw_lines: list[str]) -> tuple[list[MasterLine], list[MasterEntry],
                                                          list[str]]:
    """Assign kind, section, entry and effective tag to every master line."""
    lines: list[MasterLine] = []
    entries: list[MasterEntry] = []
    warnings: list[str] = []
    section = ""
    section_kind = "other"
    section_entry: MasterEntry | None = None
    current_entry: MasterEntry | None = None
    meta = True
    block_tag: str | None = None
    bullet_stack: list[tuple[int, str | None]] = []
    table_kind: str | None = None
    last_paragraph = ""

    def close_entry(end: int) -> None:
        nonlocal current_entry
        if current_entry is not None:
            current_entry.end_line = end
            current_entry = None

    for number, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        line = MasterLine(number=number, raw=raw, content=stripped, kind="paragraph")
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if not stripped:
            line.kind = "blank"
            block_tag = None
            bullet_stack.clear()
            table_kind = None
        elif heading:
            level = len(heading.group(1))
            line.kind = "heading"
            line.heading_level = level
            line.content = heading.group(2).strip()
            block_tag = None
            bullet_stack.clear()
            table_kind = None
            if level == 2:
                close_entry(number - 1)
                if section_entry is not None:
                    section_entry.end_line = number - 1
                    section_entry = None
                number_label, section = _strip_heading_number(line.content)
                meta = bool(META_SECTION_PATTERN.search(section))
                section_kind = _section_kind(section)
            elif level == 3 and not meta:
                close_entry(number - 1)
                if section_entry is not None:
                    section_entry.end_line = number - 1
                    section_entry = None
                number_label, title = _strip_heading_number(line.content)
                kind = "research" if title.lower().startswith("research") else section_kind
                current_entry = MasterEntry(
                    id=number_label or slugify(title), title=title, section=section, kind=kind,
                    start_line=number, end_line=len(raw_lines),
                    key_tokens=_entry_key_tokens(title))
                entries.append(current_entry)
        elif stripped.startswith("|"):
            cells = _table_cells(stripped)
            next_raw = raw_lines[number].strip() if number < len(raw_lines) else ""
            if re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
                line.kind = "table_sep"
            elif re.fullmatch(r"\|?[\s:|-]+\|?", next_raw) and next_raw.startswith("|"):
                line.kind = "table_header"
                lowered_section = section.lower()
                if "do-not-claim" in lowered_section or "do not claim" in lowered_section:
                    table_kind = "do_not_claim"
                elif section_kind == "skills":
                    table_kind = "exposure" if "exposure" in last_paragraph.lower() else "skills"
                else:
                    table_kind = "other"
            else:
                line.kind = "table"
                line.table_kind = table_kind or "other"
                exact = [TAG_PATTERN.fullmatch(cell) for cell in cells]
                cell_tag = next((m.group(1) for m in exact if m), None)
                if cell_tag:
                    line.own_tag = cell_tag
                elif line.table_kind == "skills":
                    line.own_tag = TAG_TABLE
                elif line.table_kind == "exposure":
                    line.own_tag = TAG_EXPOSURE
                line.tag = line.own_tag
                line.content = " | ".join(c for c in cells if not TAG_PATTERN.fullmatch(c))
                if line.tag == TAG_NO:
                    line.statement = cells[0] if cells else ""
        else:
            bullet = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", raw)
            if bullet:
                indent = len(bullet.group(1).replace("\t", "    "))
                body = bullet.group(3).strip()
                line.kind = "bullet"
                line.indent = indent
                lead = re.match(r"^`?\[(V|R|OLD|NO|GAP)\]`?\s*(.*)$", body)
                if lead:
                    line.own_tag = lead.group(1)
                    body = lead.group(2)
                elif re.search(r"`?\[NO\]`?", body):
                    line.own_tag = TAG_NO
                line.content = body
                while bullet_stack and bullet_stack[-1][0] >= indent:
                    bullet_stack.pop()
                if line.own_tag:
                    line.tag = line.own_tag
                    if line.tag == TAG_NO:
                        line.statement = _first_sentence(_strip_markup(body))
                elif bullet_stack:
                    line.tag = bullet_stack[-1][1]
                    if line.tag == TAG_NO:
                        line.statement = _strip_markup(body)
                else:
                    line.tag = block_tag
                    if line.tag == TAG_NO:
                        line.statement = _strip_markup(body)
                bullet_stack.append((indent, line.tag))
            else:
                found = TAG_PATTERN.search(stripped)
                is_bold_label = bool(re.fullmatch(r"\*\*[^*]*(?:`[^`]*`[^*]*)*\*\*", stripped))
                line.kind = "bold" if is_bold_label else "paragraph"
                line.own_tag = found.group(1) if found else None
                line.tag = line.own_tag
                line.content = stripped
                if line.tag == TAG_NO:
                    after = stripped[found.end():] if found else stripped
                    after = after.lstrip("*: ").strip()
                    line.statement = _first_sentence(_strip_markup(after)) if after else ""
                block_tag = line.own_tag
                bullet_stack.clear()
                last_paragraph = stripped
        line.section = section
        line.meta = meta
        if not meta and current_entry is None and section and line.kind not in ("blank",):
            if section_entry is None and not (line.kind == "heading" and line.heading_level == 2):
                number_label = _strip_heading_number(
                    next((l.content for l in reversed(lines) if l.kind == "heading"
                          and l.heading_level == 2), section))[0]
                section_entry = MasterEntry(
                    id=f"sec-{number_label or slugify(section)}", title=section, section=section,
                    kind=section_kind, start_line=number, end_line=len(raw_lines),
                    key_tokens=_entry_key_tokens(section))
                entries.append(section_entry)
        if not meta:
            if current_entry is not None:
                line.entry_id = current_entry.id
            elif section_entry is not None:
                line.entry_id = section_entry.id
        lines.append(line)
    close_entry(len(raw_lines))
    if section_entry is not None:
        section_entry.end_line = len(raw_lines)
    untagged = [l.number for l in lines if not l.meta and l.kind == "bullet" and l.tag is None
                and l.entry_id is not None]
    if untagged:
        warnings.append(
            f"{len(untagged)} untagged bullets in the master profile are treated as unverified "
            f"(not positive evidence); first lines: {untagged[:8]}")
    return lines, entries, warnings


def _mention_text(line: MasterLine) -> str:
    text = line.content
    return TAG_PATTERN.sub(" ", text)


def parse_master(path: Path, strict_no: bool = False) -> MasterProfile:
    """Parse the master profile into tagged lines, entries and skill evidence."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MissingPrerequisiteError(f"Master profile not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingPrerequisiteError(f"Master profile is unreadable ({path}): {exc}") from exc
    raw = normalize_text(raw)
    if not raw.strip():
        raise MissingPrerequisiteError(f"Master profile is empty: {path}")
    lines, entries, warnings = classify_master_lines(raw.split("\n"))
    if not entries:
        raise MissingPrerequisiteError(
            f"Master profile has no '##' sections or '###' entries to parse: {path}")

    by_token: dict[str, list[str]] = {}
    for entry in entries:
        for token in entry.key_tokens:
            by_token.setdefault(token, []).append(entry.id)
    for line in lines:
        if line.kind == "table" and line.table_kind == "skills":
            cells = line.content.split(" | ")
            evidence_cell = cells[-1].lower() if cells else ""
            refs: list[str] = []
            for token, ids in sorted(by_token.items()):
                if re.search(rf"\b{re.escape(token)}\b", evidence_cell):
                    refs.extend(i for i in ids if i not in refs)
            if re.search(r"\ball projects\b", evidence_cell):
                refs.extend(e.id for e in entries if e.kind == "project" and e.id not in refs)
            line.refs = tuple(refs)

    occurrences: dict[str, list[Occurrence]] = {}
    no_statements: list[tuple[int, str, str | None]] = []
    hard_keys: dict[str, list[int]] = {}
    soft_no: dict[str, list[Occurrence]] = {}

    def add(key: str, occ: Occurrence) -> None:
        occurrences.setdefault(key, []).append(occ)

    for line in lines:
        if line.meta or line.kind in ("blank", "table_sep", "table_header"):
            continue
        text = _mention_text(line)
        if line.kind in ("heading", "bold"):
            for key in SKILL_FINDER.keys(text):
                add(key, Occurrence(key, line.number, "heading", line.entry_id))
            continue
        if line.tag == TAG_NO:
            statement = line.statement or ""
            no_statements.append((line.number, statement, line.entry_id))
            stmt_mentions = SKILL_FINDER.find(statement)
            for mention in stmt_mentions:
                hard = False
                if line.kind == "table":
                    if line.table_kind in ("skills", "exposure"):
                        hard = True
                    elif line.table_kind == "do_not_claim":
                        hard = not _inside_explanatory_aside(statement, mention)
                occ = Occurrence(mention.key, line.number, "no", line.entry_id, hard_no=hard)
                add(mention.key, occ)
                if hard:
                    hard_keys.setdefault(mention.key, []).append(line.number)
                else:
                    soft_no.setdefault(mention.key, []).append(occ)
            remainder = text.replace(statement, " ", 1) if statement else text
            for key in SKILL_FINDER.keys(remainder):
                if key not in {m.key for m in stmt_mentions}:
                    add(key, Occurrence(key, line.number, "untagged", line.entry_id))
            continue
        kind = {
            TAG_VERIFIED: "positive", TAG_REPO: "positive", TAG_TABLE: "positive",
            TAG_EXPOSURE: "exposure", TAG_OLD: "old", TAG_GAP: "gap",
        }.get(line.tag or "", "untagged")
        for mention in SKILL_FINDER.find(text):
            occ_kind = kind
            if kind == "positive" and line.kind != "table" and mention_is_negated(text, mention):
                occ_kind = "negated"
            add(mention.key, Occurrence(mention.key, line.number, occ_kind, line.entry_id))

    def has_kind(key: str, kinds: set[str]) -> bool:
        return any(o.kind in kinds for o in occurrences.get(key, []))

    global_no: dict[str, list[int]] = {k: sorted(set(v)) for k, v in hard_keys.items()}
    scoped_no: dict[str, list[Occurrence]] = {}
    for key, occs in soft_no.items():
        if key in global_no:
            global_no[key] = sorted(set(global_no[key]) | {o.line for o in occs})
            continue
        if strict_no or not has_kind(key, {"positive", "exposure"}):
            global_no[key] = sorted({o.line for o in occs})
        else:
            scoped_no[key] = occs
    for key in sorted(scoped_no):
        lines_ref = sorted({o.line for o in scoped_no[key]})
        warnings.append(
            f"[NO] marker for '{SKILL_INDEX[key].display}' (lines {lines_ref}) is scoped to its "
            f"entry because the skill has positive evidence elsewhere; bullets from that entry "
            f"can never claim it")
    return MasterProfile(path=path, lines=lines, entries=entries, occurrences=occurrences,
                         global_no=dict(sorted(global_no.items())),
                         scoped_no=dict(sorted(scoped_no.items())),
                         no_statements=no_statements, warnings=warnings)


def _inside_explanatory_aside(text: str, mention: SkillMention) -> bool:
    """True when the mention sits in a parenthetical such as '(exposure only, via X)'."""
    open_index = text.rfind("(", 0, mention.start)
    close_before = text.rfind(")", 0, mention.start)
    if open_index == -1 or close_before > open_index:
        return False
    close_index = text.find(")", mention.end)
    aside = text[open_index + 1: close_index if close_index != -1 else len(text)]
    return bool(EXPLANATORY_ASIDE.search(aside))


def experience_months(master: MasterProfile) -> int:
    """Distinct calendar months covered by experience headings (informational only)."""
    covered: set[int] = set()
    for entry in master.entries:
        if entry.kind != "experience":
            continue
        match = DATE_RANGE_PATTERN.search(entry.title)
        if not match:
            continue
        m1, y1, m2, y2 = match.groups()
        start = MONTHS.get(m1.lower()[:3])
        end = MONTHS.get(m2.lower()[:3])
        if start and end:
            covered.update(range(int(y1) * 12 + start, int(y2) * 12 + end + 1))
    return len(covered)


# ---------------------------------------------------------------------------
# Job description ingestion
# ---------------------------------------------------------------------------
ROLE_NOUNS = re.compile(
    r"\b(engineer|engineering|developer|analyst|manager|scientist|architect|specialist|"
    r"administrator|lead|intern|internship|consultant|designer|sre|devops|technician|co-?op|"
    r"coordinator|director|programmer|tester|sdet|associate)\b", re.IGNORECASE)
GENERIC_ABOUT = re.compile(
    r"^(the role|the team|the job|the position|this role|the opportunity|you|us|our team|"
    r"the company|our company|our mission|what we do)\b", re.IGNORECASE)
LANGUAGE_CUES = re.compile(
    r"\b(fluent|fluency|speak|speaking|spoken|written|verbal|language|languages|bilingual|"
    r"proficien\w*|native)\b", re.IGNORECASE)


def _clean_meta_value(value: str) -> str:
    value = re.sub(r"[*_`#>\[\]]", "", value)
    value = collapse_ws(value).strip(" -:|")
    return value[:80]


def _heading_text(line: str) -> str | None:
    stripped = line.strip()
    md = re.match(r"^#{1,6}\s+(.*)$", stripped)
    if md:
        return _clean_meta_value(md.group(1))
    bold = re.fullmatch(r"(\*\*|__)(.+?)\1:?", stripped)
    if bold:
        return _clean_meta_value(bold.group(2))
    if re.match(r"^([-*+]|\d+\.)\s", stripped):
        return None
    if stripped.endswith(":") and 0 < len(stripped.split()) <= 8:
        return _clean_meta_value(stripped[:-1])
    letters = re.sub(r"[^A-Za-z]", "", stripped)
    if letters and letters.isupper() and len(stripped.split()) <= 6 and len(letters) >= 4:
        return _clean_meta_value(stripped)
    return None


def classify_heading(text: str) -> str:
    lowered = text.lower()
    for section_type, needles in HEADING_RULES:
        for needle in needles:
            if re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", lowered):
                return section_type
    return "unclassified"


def _inline_label(text: str) -> tuple[str, str] | None:
    """Split 'Nice to have: Kubernetes' into ('preferred', 'Kubernetes')."""
    match = re.match(r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?([A-Za-z][A-Za-z '’/-]{2,40}?)(?:\*\*|__)?"
                     r"\s*:\s*(?:\*\*|__)?\s*(.+)$", text)
    if not match or len(match.group(1).split()) > 5:
        return None
    section_type = classify_heading(match.group(1))
    if section_type in ("unclassified", "info", "responsibilities"):
        return None
    return section_type, match.group(2)


def read_job_description(path: Path, workspace: Path) -> JobDescription:
    """Validate and read a JD file; never modifies it."""
    if path.suffix.lower() not in JD_EXTENSIONS:
        raise InvalidInputError(
            f"Unsupported job description type '{path.suffix or '(none)'}': {path}. "
            f"Use a .md or .txt file.")
    if not path.exists():
        raise InvalidInputError(f"Job description not found: {path}")
    if not path.is_file():
        raise InvalidInputError(f"Job description is not a regular file: {path}")
    try:
        size = path.stat().st_size
        if size > MAX_JD_BYTES:
            raise InvalidInputError(
                f"Job description is too large ({size} bytes > {MAX_JD_BYTES}): {path}")
        data = path.read_bytes()
    except PermissionError as exc:
        raise InvalidInputError(f"Job description is not readable: {path}") from exc
    if b"\x00" in data:
        raise InvalidInputError(f"Job description looks binary, not text: {path}")
    try:
        raw = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidInputError(f"Job description is not valid UTF-8 text: {path}") from exc
    text = normalize_text(raw)
    if not text.strip():
        raise InvalidInputError(f"Job description is empty: {path}")
    if not re.search(r"[A-Za-z]{3,}", text):
        raise InvalidInputError(f"Job description contains no readable words: {path}")

    try:
        display = path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        display = path.name
    lines, warnings = segment_job_description(text)
    partial = any(l.section_title == "metadata" and PARTIAL_DESCRIPTION.search(l.text)
                  for l in lines)
    if partial:
        warnings.append("The JD file marks its description as partial (a source snippet or "
                        "listing only); requirements are likely missing, so the fit score "
                        "covers only the text provided")
    company, company_source, role, role_source = identify_company_and_role(text, path)
    if company_source == "fallback":
        warnings.append("Company name not found in explicit metadata or headings; "
                        "using 'unknown-company'")
    if role_source == "filename":
        warnings.append(f"Role title not found in metadata or headings; using the file name "
                        f"'{path.stem}'")
    return JobDescription(path=path, display_path=display, raw_text=text, lines=lines,
                          sha256=sha256_text(text), company=company,
                          company_source=company_source, role=role, role_source=role_source,
                          warnings=warnings, partial_description=partial)


def identify_company_and_role(text: str, path: Path) -> tuple[str, str, str, str]:
    company = role = ""
    company_source = role_source = ""
    lines = text.split("\n")
    body_start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, min(len(lines), 60)):
            if lines[index].strip() == "---":
                body_start = index + 1
                break
            match = re.match(r"^\s*([A-Za-z _-]+)\s*:\s*(.+)$", lines[index])
            if not match:
                continue
            key = match.group(1).strip().lower()
            value = _clean_meta_value(match.group(2).strip("'\""))
            if key in ("company", "employer", "organization", "organisation") and value:
                company, company_source = value, "front_matter"
            elif key in ("role", "title", "job title", "position", "job_title") and value:
                role, role_source = value, "front_matter"
    meta_pattern = re.compile(
        r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(company|employer|organization|organisation|role|"
        r"job title|title|position)(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.+)$", re.IGNORECASE)
    for line in lines[body_start: body_start + 60]:
        match = meta_pattern.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        value = _clean_meta_value(match.group(2))
        if not value:
            continue
        if key in ("company", "employer", "organization", "organisation") and not company:
            company, company_source = value, "metadata"
        elif key in ("role", "job title", "title", "position") and not role:
            role, role_source = value, "metadata"
    first_heading = None
    for line in lines[body_start:]:
        if line.strip():
            first_heading = _heading_text(line) if line.strip().startswith(("#", "**", "__")) \
                else None
            break
    if first_heading:
        at = re.match(r"^(.+?)\s+(?:at|@)\s+(.+)$", first_heading)
        if at and ROLE_NOUNS.search(at.group(1)):
            if not role:
                role, role_source = _clean_meta_value(at.group(1)), "heading"
            if not company:
                company, company_source = _clean_meta_value(at.group(2)), "heading"
        elif not role and ROLE_NOUNS.search(first_heading):
            role, role_source = first_heading, "heading"
    if not company:
        for line in lines[body_start:]:
            heading = _heading_text(line)
            if not heading:
                continue
            about = re.match(r"^about\s+(.+)$", heading, re.IGNORECASE)
            if about and not GENERIC_ABOUT.match(about.group(1)):
                company, company_source = _clean_meta_value(about.group(1)), "about_heading"
                break
    if not company:
        company, company_source = "unknown-company", "fallback"
    if not role:
        role = collapse_ws(re.sub(r"[_\-.]+", " ", path.stem)) or "unknown-role"
        role_source = "filename"
    return company, company_source, role, role_source


# "Label: value" lines before the first section heading that describe the posting
# (who, where, when, from which source). They are never requirements.
METADATA_LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(company|employer|organi[sz]ation|role|job title|title|"
    r"position|source|location|url|link|date ingested|posted|date posted|updated|"
    r"employment type|workplace type|department|team|salary|terms|application url|"
    r"description completeness|generated by|job id)(?:\*\*|__)?\s*:", re.IGNORECASE)
PARTIAL_DESCRIPTION = re.compile(r"description completeness(?:\*\*|__)?\s*:\s*partial",
                                 re.IGNORECASE)


def segment_job_description(text: str) -> tuple[list[JDLine], list[str]]:
    """Attach a section type to every JD line using heading rules.

    YAML front matter and metadata label lines before the first section heading
    are typed "info", so they name the posting without being scored.
    """
    lines: list[JDLine] = []
    warnings: list[str] = []
    section_type = "intro"
    section_title = ""
    seen_heading = False
    raw_lines = text.split("\n")
    front_matter_end = 0
    if raw_lines and raw_lines[0].strip() == "---":
        for index in range(1, min(len(raw_lines), 60)):
            if raw_lines[index].strip() == "---":
                front_matter_end = index + 1
                break
    for number, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if number <= front_matter_end or (not seen_heading and METADATA_LINE.match(stripped)):
            lines.append(JDLine(number, raw, stripped, "info", "metadata", False, False))
            continue
        heading = _heading_text(stripped)
        if heading is not None and not (number == 1 and ROLE_NOUNS.search(heading)
                                        and classify_heading(heading) == "unclassified"):
            seen_heading = True
            section_title = heading
            section_type = classify_heading(heading)
            lines.append(JDLine(number, raw, heading, section_type, section_title, True, False))
            continue
        if heading is not None:
            lines.append(JDLine(number, raw, heading, "intro", "", True, False))
            continue
        is_bullet = bool(re.match(r"^([-*+]|\d+[.)])\s+", stripped))
        body = re.sub(r"^([-*+]|\d+[.)])\s+", "", stripped)
        lines.append(JDLine(number, raw, body, section_type, section_title, False, is_bullet))
    if not seen_heading:
        warnings.append("No recognizable section headings; requirement categories rely on "
                        "explicit wording only (low confidence)")
    return lines, warnings


# ---------------------------------------------------------------------------
# Requirement extraction
# ---------------------------------------------------------------------------
IGNORED_ACRONYMS = frozenset({"US", "USA", "EST", "PST", "EOE", "FTE", "OK", "CEO", "CTO", "CFO",
                              "HR", "TBD", "ASAP", "AM", "PM", "UTC", "EU", "UK", "NA", "OR",
                              "AND", "II", "III", "IV", "LLC", "INC", "LTD", "PTO", "RRSP", "WFH",
                              "PhD", "MSc", "BSc", "BEng", "MEng", "MBA", "BS", "MS", "BA", "MA",
                              "STEM", "GPA"})
CATEGORY_RANK = {"mandatory": 3, "required": 2, "preferred": 1}


def _clause_category(clause: str, mention_start: int, default: str) -> tuple[str, str]:
    mandatory = list(MANDATORY_MARKERS.finditer(clause))
    preferred = list(PREFERRED_MARKERS.finditer(clause))
    if mandatory and preferred:
        for seg in re.finditer(r"[^,]+", clause):
            if not seg.start() <= mention_start < seg.end():
                continue
            own_m = [m for m in mandatory if seg.start() <= m.start() < seg.end()]
            own_p = [m for m in preferred if seg.start() <= m.start() < seg.end()]
            if own_p and not own_m:
                return "preferred", f"wording marker '{own_p[0].group(0).lower()}' in its phrase"
            if own_m and not own_p:
                return "mandatory", f"wording marker '{own_m[0].group(0).lower()}' in its phrase"
        best = min([(abs(m.start() - mention_start), 0, "mandatory", m.group(0))
                    for m in mandatory]
                   + [(abs(m.start() - mention_start), 1, "preferred", m.group(0))
                      for m in preferred])
        return best[2], f"nearest wording marker '{best[3].lower()}'"
    if mandatory:
        return "mandatory", f"wording marker '{mandatory[0].group(0).lower()}'"
    if preferred:
        return "preferred", f"wording marker '{preferred[0].group(0).lower()}'"
    return default, ""


# Skills listed after one of these cues are alternatives or a stack description, not
# separate demands: "We work mostly in Java, Ruby and Go" becomes one requirement that any
# one of them satisfies. Skills that only illustrate an umbrella skill named earlier in the
# clause ("cloud platforms such as AWS or GCP") are left to that umbrella requirement.
ALTERNATIVE_CUE = re.compile(
    r"\b(?:(?:we|our teams?|the team)\s+(?:mostly\s+|mainly\s+|primarily\s+|currently\s+|"
    r"typically\s+)?(?:(?:work|works|build|code|develop|program|write)\s+"
    r"(?:mostly\s+|mainly\s+|primarily\s+)?(?:in|with|on|using)|uses?)|"
    r"our (?:tech(?:nology)? )?stack (?:includes|is|spans)|(?:at least )?one (?:or more )?of|"
    r"any (?:one )?of|such as|e\.g\.?|for example|including but not limited to)(?![A-Za-z])",
    re.IGNORECASE)
# Pay-transparency text (often placed inside a requirements section) is never a requirement.
PAY_TEXT = re.compile(
    r"\b(?:salary|salaries|pay (?:range|band|scale|transparency)|base pay|base compensation|"
    r"compensation (?:range|package|band)|total (?:compensation|rewards)|starting "
    r"(?:salary|pay|range)|hourly (?:rate|range|wage)|on-target earnings|wage range)\b",
    re.IGNORECASE)
MONEY_RANGE = re.compile(
    r"(?:[$\u20ac\u00a3]|\b(?:USD|CAD|EUR|GBP)\s?)\s?\d[\d,]*(?:\.\d+)?\s*[kK]?\s*"
    r"(?:-|\u2013|\u2014|to)\s*(?:[$\u20ac\u00a3]|\b(?:USD|CAD|EUR|GBP)\s?)?\s?\d")


def is_pay_text(text: str) -> bool:
    return bool(PAY_TEXT.search(text) or MONEY_RANGE.search(text))


def _company_word(word: str, exclude: set[str]) -> bool:
    """'Stripes' and "Stripe's" are the company's own name, not a requirement."""
    low = word.lower()
    forms = {low}
    for suffix in ("'s", "es", "s"):
        if low.endswith(suffix) and len(low) > len(suffix) + 2:
            forms.add(low[: -len(suffix)])
    return bool(forms & exclude)


def _unknown_terms(clause: str, mentions: list[SkillMention], exclude: set[str]) -> list[str]:
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z0-9+#./-]*[A-Za-z0-9+#]|[A-Za-z]", clause))
    picked: list[tuple[int, int, str]] = []
    for index, match in enumerate(tokens):
        word = re.sub(r"'s$", "", match.group(0)).rstrip("./-")
        if not word or any(match.start() < m.end and m.start < match.end() for m in mentions):
            continue
        if word in COMMON_CAPITALIZED or word.lower() in STOPWORDS or _company_word(word, exclude):
            continue
        letters = re.sub(r"[^A-Za-z]", "", word)
        is_caps = letters.isupper() and 2 <= len(letters) <= 8
        is_camel = bool(re.search(r"[a-z][A-Z]", word))
        has_digit = any(ch.isdigit() for ch in word) and any(ch.isalpha() for ch in word)
        at_start = index == 0
        if word in IGNORED_ACRONYMS:
            continue
        if is_camel or is_caps or has_digit or (word[0].isupper() and not at_start):
            picked.append((match.start(), match.end(), word))
    phrases: list[str] = []
    current: list[tuple[int, int, str]] = []
    for item in picked:
        if current and clause[current[-1][1]:item[0]] == " " and len(current) < 4:
            current.append(item)
            continue
        if current:
            phrases.append(" ".join(w for _s, _e, w in current))
        current = [item]
    if current:
        phrases.append(" ".join(w for _s, _e, w in current))
    return phrases


def _is_parenthetical_member(clause: str, mention: SkillMention,
                             mentions: list[SkillMention]) -> bool:
    for parent in mentions:
        if parent.end > mention.start or mention.key not in SKILL_INDEX[parent.key].members:
            continue
        if re.fullmatch(r"\s*\([^()]*", clause[parent.end:mention.start]):
            return True
    return False


def extract_requirements(jd: JobDescription) -> tuple[list[Requirement], list[dict[str, Any]],
                                                       list[dict[str, Any]], list[str]]:
    """Return (requirements, unscored lines, not-required mentions, warnings)."""
    found: dict[tuple[str, str], Requirement] = {}
    unscored: list[dict[str, Any]] = []
    not_required: list[dict[str, Any]] = []
    warnings: list[str] = []
    exclude = {w.lower() for w in word_tokens(jd.company + " " + jd.role)}
    company_skills = set(SKILL_FINDER.keys(jd.company))  # a posting by "GitLab" names GitLab
    pay_lines = 0

    def record(kind: str, ident: str, name: str, key: str | None, category: str,
               line: JDLine, reason: str, confidence: str,
               members: list[str] | None = None) -> None:
        req = found.get((kind, ident))
        if req is None:
            found[(kind, ident)] = Requirement(
                name=name, key=key, kind=kind, category=category, jd_lines=[line.number],
                jd_excerpt=excerpt(line.text, 200), reason=reason, confidence=confidence,
                members=list(members or []))
            return
        if line.number not in req.jd_lines:
            req.jd_lines.append(line.number)
        if CATEGORY_RANK[category] > CATEGORY_RANK[req.category]:
            req.category, req.reason, req.confidence = category, reason, confidence
            req.jd_excerpt = excerpt(line.text, 200)

    for line in jd.lines:
        if line.is_heading or line.section_type == "info":
            continue
        if is_pay_text(line.text):
            pay_lines += 1
            continue
        section_type = line.section_type
        text = strip_links(line.text)
        inline = _inline_label(text)
        if inline:
            section_type, text = inline
        default = SECTION_DEFAULT_CATEGORY.get(section_type, "required")
        section_label = line.section_title or ("inline label" if inline else "introduction")
        requirement_section = section_type in ("mandatory", "required", "preferred")
        line_scored = False
        for clause in split_clauses(text):
            if NOT_REQUIRED_PATTERN.search(clause):
                for key in SKILL_FINDER.keys(clause):
                    not_required.append({"skill": SKILL_INDEX[key].display, "jd_line": line.number,
                                         "text": excerpt(clause, 160)})
                line_scored = True
                continue
            mentions = [m for m in SKILL_FINDER.find(clause) if m.key not in company_skills]
            has_marker = bool(MANDATORY_MARKERS.search(clause) or PREFERRED_MARKERS.search(clause))
            consumed = _record_alternatives(clause, mentions, default, section_label,
                                            section_type, line, record)
            line_scored = line_scored or bool(consumed)
            for mention in mentions:
                spec = SKILL_INDEX[mention.key]
                if id(mention) in consumed:
                    continue
                if _is_parenthetical_member(clause, mention, mentions):
                    continue  # "AWS (EC2, S3)" is one requirement, not three
                if spec.kind == "language" and not LANGUAGE_CUES.search(clause):
                    continue
                category, marker_reason = _clause_category(clause, mention.start, default)
                reason = marker_reason or f"section '{section_label}' ({section_type})"
                confidence = "low" if (section_type in LOW_CONFIDENCE_SECTIONS
                                       and not marker_reason) else "high"
                kind = "language" if spec.kind == "language" else (
                    "skill_category" if spec.members else "skill")
                record(kind, spec.key, spec.display, spec.key, category, line, reason, confidence)
                line_scored = True
            if not (requirement_section or has_marker):
                continue
            category, marker_reason = _clause_category(clause, 0, default)
            reason = marker_reason or f"section '{section_label}' ({section_type})"
            years = YEARS_PATTERN.search(clause)
            if years and re.search(r"experience|background|industry|professional", clause, re.I):
                minimum = int(years.group(1))
                if minimum > 0:
                    record("years_experience", "years", f"{minimum}+ years of experience", None,
                           category, line, reason, "high")
                    line_scored = True
            if DEGREE_PATTERN.search(clause):
                record("education", "degree", "Degree requirement", None, category, line, reason,
                       "medium")
                line_scored = True
            if CERT_PATTERN.search(clause):
                name = f"Certification: {excerpt(clause, 80)}"
                record("certification", slugify(clause, 60), name, None, category, line, reason,
                       "medium")
                line_scored = True
            for term in _unknown_terms(clause, mentions, exclude):
                record("unknown_named", term.lower(), term, None, category, line,
                       reason + "; outside the controlled vocabulary", "low")
                line_scored = True
        if not line_scored and (requirement_section or section_type == "responsibilities"):
            unscored.append({"jd_line": line.number, "section": section_label,
                             "text": excerpt(line.text, 200)})
    _drop_overlapping_groups(found)
    requirements = sorted(found.values(),
                          key=lambda r: (-CATEGORY_RANK[r.category], r.jd_lines[0], r.name))
    if not requirements:
        warnings.append("No requirements could be extracted; fit score is 0 (low-confidence "
                        "analysis, inspect the JD manually)")
    if unscored:
        warnings.append(f"{len(unscored)} requirement/responsibility lines contain no "
                        f"recognizable skill or named term and were not scored; see "
                        f"'unscored_requirement_lines'")
    low = [r.name for r in requirements if r.confidence == "low"]
    if low:
        warnings.append(f"{len(low)} requirements have low parsing confidence: {low[:10]}")
    if pay_lines:
        warnings.append(f"{pay_lines} salary or pay-range lines were skipped")
    return requirements, unscored, not_required, warnings


def _record_alternatives(clause: str, mentions: list[SkillMention], default: str,
                         section_label: str, section_type: str, line: JDLine,
                         record: Callable[..., None]) -> set[int]:
    """Record skills after an alternative/stack cue as one either-or requirement.

    Returns the ids of mentions that are now accounted for, so they are not also
    recorded one by one.
    """
    cue = ALTERNATIVE_CUE.search(clause)
    if not cue:
        return set()
    umbrellas = [m for m in mentions if m.end <= cue.start() and SKILL_INDEX[m.key].members]
    after = [m for m in mentions if m.start >= cue.end()
             and SKILL_INDEX[m.key].kind != "language"]
    examples = {id(m) for m in after
                if any(m.key in SKILL_INDEX[u.key].members for u in umbrellas)}
    rest = [m for m in after if id(m) not in examples]
    keys = list(dict.fromkeys(m.key for m in rest))
    if len(keys) < 2:
        return examples
    category, marker_reason = _clause_category(clause, cue.start(), default)
    reason = (marker_reason or f"section '{section_label}' ({section_type})") + \
        f"; alternatives after '{cue.group(0).strip().lower()}'"
    names = [SKILL_INDEX[k].display for k in keys]
    record("skill_group", "|".join(sorted(keys)), "One of: " + ", ".join(names), None,
           category, line, reason, "medium", members=keys)
    return examples | {id(m) for m in rest}


def _drop_overlapping_groups(found: dict[tuple[str, str], Requirement]) -> None:
    """Count a skill once when it is asked for both on its own and inside a group.

    A member that is also a separate requirement at the same or a higher category
    makes the group redundant, so the group is dropped. A weaker separate mention
    (a preferred skill that is also in a required group) is folded into the group.
    """
    singles = {req.key: slot for slot, req in found.items()
               if req.kind in ("skill", "skill_category", "language")}
    for slot, group in list(found.items()):
        if group.kind != "skill_group":
            continue
        overlap = [singles[k] for k in group.members if singles.get(k) in found]
        if not overlap:
            continue
        if any(CATEGORY_RANK[found[o].category] >= CATEGORY_RANK[group.category]
               for o in overlap):
            del found[slot]
            continue
        for o in overlap:
            group.jd_lines.extend(n for n in found.pop(o).jd_lines if n not in group.jd_lines)
        group.jd_lines.sort()


# ---------------------------------------------------------------------------
# Evidence matching
# ---------------------------------------------------------------------------
def _evidence_item(master: MasterProfile, line_no: int) -> dict[str, Any]:
    line = master.line(line_no)
    entry = master.entry(line.entry_id)
    return {
        "line": line_no,
        "section": line.section,
        "entry": entry.title if entry else None,
        "tag": line.tag,
        "excerpt": excerpt(line.raw),
    }


def _evidence_lines(master: MasterProfile, key: str, kinds: set[str]) -> list[int]:
    return sorted({o.line for o in master.occurrences.get(key, []) if o.kind in kinds})


def match_requirement(req: Requirement, master: MasterProfile) -> None:
    """Set status, evidence and explanation for one requirement in place."""
    if req.kind in ("skill", "skill_category", "language") and req.key:
        _match_skill(req, master)
    elif req.kind == "skill_group":
        _match_group(req, master)
    elif req.kind == "years_experience":
        months = experience_months(master)
        req.status = STATUS_AMBIGUOUS
        req.explanation = (
            f"JD asks for {req.name}. The master profile's experience headings cover {months} "
            f"distinct months of co-op and team terms; the pipeline does not convert co-op or "
            f"part-time terms into full-time years, so this cannot be established "
            f"deterministically.")
    elif req.kind == "education":
        _match_education(req, master)
    elif req.kind == "certification":
        hits = [l.number for l in master.lines if not l.meta and l.tag in POSITIVE_TAGS
                and re.search(r"\bcertif(?:ied|ication|ications)\b", l.content, re.I)]
        if hits:
            req.status = STATUS_AMBIGUOUS
            req.evidence = [_evidence_item(master, n) for n in hits[:5]]
            req.evidence_count = len(hits)
            req.explanation = "Master mentions a certification; confirm it matches manually."
        else:
            req.status = STATUS_NOT_EVIDENCED
            req.explanation = "The master profile lists no professional certifications."
    else:
        _match_unknown(req, master)


def _match_group(req: Requirement, master: MasterProfile) -> None:
    """Either-or requirement: positive evidence for any one member satisfies it."""
    names = ", ".join(SKILL_INDEX[k].display for k in req.members)
    statuses = []
    for key in req.members:
        probe = Requirement(SKILL_INDEX[key].display, key, "skill", req.category, req.jd_lines,
                            req.jd_excerpt, req.reason)
        _match_skill(probe, master)
        if probe.status == STATUS_MATCHED:
            req.status = STATUS_MATCHED
            req.matched_via = probe.matched_via or probe.name
            req.evidence, req.evidence_count = probe.evidence, probe.evidence_count
            req.conflicts = probe.conflicts
            req.explanation = f"Any one of {names} is enough; {probe.name} has positive evidence."
            return
        statuses.append(probe.status)
    if statuses and all(s == STATUS_NO for s in statuses):
        req.status = STATUS_NO
    elif STATUS_AMBIGUOUS in statuses:
        req.status = STATUS_AMBIGUOUS
    else:
        req.status = STATUS_NOT_EVIDENCED
    req.explanation = f"Any one of {names} would count; none has positive evidence."


def _match_skill(req: Requirement, master: MasterProfile) -> None:
    key = req.key or ""
    spec = SKILL_INDEX[key]
    if key in master.global_no:
        lines = master.global_no[key]
        req.status = STATUS_NO
        req.evidence = [_evidence_item(master, n) for n in lines[:5]]
        req.evidence_count = len(lines)
        req.explanation = (f"'{spec.display}' is marked [NO] in the master profile "
                           f"(lines {lines}); [NO] overrides every positive match rule.")
        return
    positive = _evidence_lines(master, key, {"positive"})
    via: list[str] = []
    if not positive and spec.members:
        for member in spec.members:
            if member in master.global_no:
                continue
            member_lines = _evidence_lines(master, member, {"positive"})
            if member_lines:
                via.append(member)
                positive.extend(n for n in member_lines if n not in positive)
        positive.sort()
    if positive:
        req.status = STATUS_MATCHED
        req.evidence = [_evidence_item(master, n) for n in positive[:5]]
        req.evidence_count = len(positive)
        if via:
            req.matched_via = ", ".join(SKILL_INDEX[m].display for m in via)
            req.explanation = (f"Category satisfied by positive evidence for member skill(s): "
                               f"{req.matched_via}.")
        else:
            req.explanation = (f"Positive ([V]/[R]/skills table) evidence on "
                               f"{len(positive)} master line(s).")
        for scoped_key in [key, *via]:
            for occ in master.scoped_no.get(scoped_key, []):
                entry = master.entry(occ.entry_id)
                req.conflicts.append({
                    "skill": SKILL_INDEX[scoped_key].display, "line": occ.line,
                    "entry": entry.title if entry else None,
                    "statement": excerpt(master.line(occ.line).statement or
                                         master.line(occ.line).raw, 160),
                    "effect": "bullets from this entry cannot claim the skill",
                })
        return
    occs = master.occurrences.get(key, [])
    kinds = {o.kind for o in occs}
    member_note = ""
    if spec.members:
        names = ", ".join(SKILL_INDEX[m].display for m in spec.members)
        member_note = f" No positive evidence for any member skill ({names})."
    if "heading" in kinds and not kinds & {"old", "gap", "untagged", "exposure", "negated"}:
        lines = _evidence_lines(master, key, {"heading"})
        req.status = STATUS_AMBIGUOUS
        req.evidence = [_evidence_item(master, n) for n in lines[:5]]
        req.evidence_count = len(lines)
        req.explanation = ("Appears only in a heading/title; no positive statement demonstrates "
                           "it, so it is not counted." + member_note)
    elif "exposure" in kinds:
        lines = _evidence_lines(master, key, {"exposure"})
        req.status = STATUS_NOT_EVIDENCED
        req.evidence = [_evidence_item(master, n) for n in lines[:5]]
        req.evidence_count = len(lines)
        req.explanation = ("Exposure-level only (usable in 'familiar with'/'interests', never as "
                           "demonstrated experience)." + member_note)
    elif kinds & {"old", "gap", "untagged"}:
        lines = _evidence_lines(master, key, {"old", "gap", "untagged"})
        req.status = STATUS_AMBIGUOUS
        req.evidence = [_evidence_item(master, n) for n in lines[:5]]
        req.evidence_count = len(lines)
        req.explanation = ("Only unconfirmed ([OLD]/[GAP]/untagged) mentions; confirm in the "
                           "master profile before it can count." + member_note)
    elif "negated" in kinds:
        lines = _evidence_lines(master, key, {"negated"})
        req.status = STATUS_NOT_EVIDENCED
        req.evidence = [_evidence_item(master, n) for n in lines[:5]]
        req.evidence_count = len(lines)
        req.explanation = "Only negated mentions (e.g. 'no ...'); not positive evidence."
    else:
        req.status = STATUS_NOT_EVIDENCED
        req.explanation = ("No mention in positive master evidence; adjacent or related skills "
                           "are never substituted." + member_note)


def _match_education(req: Requirement, master: MasterProfile) -> None:
    clause = " ".join(req.jd_excerpt.lower().split())
    degree_lines = [l.number for l in master.lines
                    if not l.meta and l.tag in POSITIVE_TAGS and l.entry_id
                    and (master.entry(l.entry_id) or MasterEntry("", "", "", "", 0, 0, ())).kind
                    == "education" and re.search(r"\b(bsc|bachelor|degree)\b", l.content, re.I)]
    req.evidence = [_evidence_item(master, n) for n in degree_lines[:3]]
    req.evidence_count = len(degree_lines)
    if not degree_lines:
        req.status = STATUS_NOT_EVIDENCED
        req.explanation = "No degree found in positive master evidence."
        return
    wants_graduate = re.search(r"\b(master'?s|m\.?sc|ph\.?d|doctorate)\b", clause)
    wants_bachelor = re.search(r"\b(bachelor'?s?|b\.?sc?|undergraduate|university degree)\b",
                               clause)
    if wants_graduate and not wants_bachelor:
        req.status = STATUS_NOT_EVIDENCED
        req.explanation = "JD asks for a graduate degree; the master profile shows a BSc."
        return
    science = re.sub(r"computer science", "", clause)
    if re.search(r"\b(physics|stem|natural sciences?|sciences?|any field|any discipline)\b",
                 science):
        req.status = STATUS_MATCHED
        req.explanation = "JD accepts physics/science/STEM degrees; master shows a BSc in Physics."
    elif re.search(r"computer science|software engineering|engineering|mathematics|math",
                   clause):
        if re.search(r"related|equivalent|similar|quantitative|technical (?:field|discipline)",
                     clause):
            req.status = STATUS_AMBIGUOUS
            req.explanation = ("JD names a specific field 'or related'; whether a BSc in Physics "
                               "qualifies is a human judgment.")
        else:
            req.status = STATUS_NOT_EVIDENCED
            req.explanation = "JD names a specific field the master BSc (Physics) does not match."
    else:
        req.status = STATUS_MATCHED
        req.explanation = "JD asks for a degree without a specific field; master shows a BSc."


def _match_unknown(req: Requirement, master: MasterProfile) -> None:
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(req.name)}(?![A-Za-z0-9])", re.IGNORECASE)
    positive, negative, other = [], [], []
    for line in master.lines:
        if line.meta or line.kind in ("blank", "table_sep", "table_header"):
            continue
        if line.tag == TAG_NO and pattern.search(line.statement or ""):
            negative.append(line.number)
        elif pattern.search(line.content):
            (positive if line.tag in POSITIVE_TAGS else other).append(line.number)
    if negative:
        req.status = STATUS_NO
        lines = negative
        req.explanation = "Named in a [NO] statement of the master profile."
    elif positive or other:
        req.status = STATUS_AMBIGUOUS
        lines = positive or other
        req.explanation = ("Literal mention found, but the term is outside the controlled "
                           "vocabulary; confirm manually before it can count.")
    else:
        req.status = STATUS_NOT_EVIDENCED
        lines = []
        req.explanation = "Term not found in the master profile."
    req.evidence = [_evidence_item(master, n) for n in lines[:5]]
    req.evidence_count = len(lines)


# ---------------------------------------------------------------------------
# Scoring and domain classification
# ---------------------------------------------------------------------------
def compute_score(requirements: Sequence[Requirement]) -> tuple[int, dict[str, Any]]:
    """Weighted coverage, rounded half up with integer arithmetic."""
    total = sum(r.weight for r in requirements)
    matched = sum(r.weight for r in requirements if r.status == STATUS_MATCHED)
    score = 0 if total == 0 else (200 * matched + total) // (2 * total)
    by_category: dict[str, dict[str, int]] = {}
    for category in ("mandatory", "required", "preferred"):
        items = [r for r in requirements if r.category == category]
        by_category[category] = {
            "weight_each": CATEGORY_WEIGHTS[category],
            "count": len(items),
            "matched": sum(1 for r in items if r.status == STATUS_MATCHED),
            "weight_total": sum(r.weight for r in items),
            "weight_matched": sum(r.weight for r in items if r.status == STATUS_MATCHED),
        }
    breakdown = {
        "formula": "round_half_up(100 * matched_weight / total_weight); 0 when total_weight == 0",
        "weights": dict(CATEGORY_WEIGHTS),
        "matched_weight": matched,
        "total_weight": total,
        "by_category": by_category,
        "threshold": FIT_THRESHOLD,
    }
    return score, breakdown


def verdict_for(score: int) -> str:
    return VERDICT_PROCEED if score >= FIT_THRESHOLD else VERDICT_REJECT


def _phrase_in(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def classify_domain(jd: JobDescription, requirements: Sequence[Requirement]) -> dict[str, Any]:
    title = jd.role.lower()
    scores: dict[str, int] = {}
    indicators: dict[str, list[str]] = {}
    title_hit: dict[str, bool] = {}
    body_lines = [l.text.lower() for l in jd.lines
                  if not l.is_heading and l.section_type != "info"]
    for key, _label, title_words, body_words in DOMAINS:
        hits = [w for w in title_words if _phrase_in(w, title)][:2]
        title_hit[key] = bool(hits)
        body = 0
        body_found: list[str] = []
        for line in body_lines:
            for word in body_words:
                if _phrase_in(word, line):
                    body += 1
                    if word not in body_found:
                        body_found.append(word)
        skill_points = 0
        skill_found: list[str] = []
        for req in requirements:
            req_keys = [req.key] if req.key else req.members
            if any(key in SKILL_INDEX[k].domains for k in req_keys):
                skill_points += req.weight
                skill_found.append(req.name)
        scores[key] = DOMAIN_TITLE_WEIGHT * len(hits) + min(body, DOMAIN_BODY_CAP) + skill_points
        indicators[key] = ([f"title:{w}" for w in hits] + [f"text:{w}" for w in body_found[:8]]
                           + [f"skill:{s}" for s in skill_found[:8]])
    ranked = sorted(scores, key=lambda k: (-scores[k], [d[0] for d in DOMAINS].index(k)))
    first = ranked[0]
    limitations = ["Keyword and skill-frequency heuristic; it does not read intent and is "
                   "reported separately from the fit score."]
    if scores[first] < DOMAIN_MIN_SCORE:
        primary = DOMAIN_UNKNOWN
        secondary: list[str] = []
        limitations.append("Too few domain indicators to classify.")
    else:
        second = ranked[1]
        if (scores[second] >= 0.9 * scores[first] and not title_hit[first]):
            primary = DOMAIN_UNKNOWN
            secondary = [DOMAIN_LABELS[first], DOMAIN_LABELS[second]]
            limitations.append("Top two domains are within 10% and the title does not decide.")
        else:
            primary = DOMAIN_LABELS[first]
            cutoff = max(DOMAIN_SECONDARY_MIN, DOMAIN_SECONDARY_RATIO * scores[first])
            secondary = [DOMAIN_LABELS[k] for k in ranked[1:] if scores[k] >= cutoff]
    return {
        "primary": primary,
        "secondary": secondary,
        "scores": {DOMAIN_LABELS[k]: scores[k] for k in ranked},
        "indicators": {DOMAIN_LABELS[k]: indicators[k] for k in ranked if indicators[k]},
        "limitations": limitations,
    }


# ---------------------------------------------------------------------------
# Analysis report
# ---------------------------------------------------------------------------
@dataclass
class Analysis:
    jd: JobDescription
    master: MasterProfile
    requirements: list[Requirement]
    score: int
    verdict: str
    breakdown: dict[str, Any]
    domain: dict[str, Any]
    unscored: list[dict[str, Any]]
    not_required: list[dict[str, Any]]
    warnings: list[str]
    coverage: CoverageModel | None = None
    kit: resume_kit.ResumeKit | None = None    # section 9; None in legacy (--no-kit) mode
    kit_audit: KitAudit | None = None
    family: FamilyDecision | None = None
    kit_hooks: dict[str, frozenset[str]] = field(default_factory=dict)


def analyze(jd: JobDescription, master: MasterProfile,
            kit: resume_kit.ResumeKit | None = None) -> Analysis:
    requirements, unscored, not_required, warnings = extract_requirements(jd)
    for req in requirements:
        match_requirement(req, master)
    score, breakdown = compute_score(requirements)
    domain = classify_domain(jd, requirements)
    analysis = Analysis(jd=jd, master=master, requirements=requirements, score=score,
                        verdict=verdict_for(score), breakdown=breakdown, domain=domain,
                        unscored=unscored, not_required=not_required,
                        warnings=[*jd.warnings, *warnings],
                        coverage=build_coverage_model(jd, master, kit), kit=kit)
    if kit is not None:
        analysis.kit_audit = audit_kit(kit, master)
        analysis.family = classify_family(jd, kit)
        analysis.kit_hooks = kit_hooks(kit, analysis.kit_audit)
        extend_profile_with_kit(analysis)
    return analysis


def extend_profile_with_kit(analysis: Analysis) -> None:
    """Kit bullets are profile evidence too, at their entry's strength, with their themes
    and 9.6 synonyms (blocked bullets never count)."""
    model, kit, audit, master = analysis.coverage, analysis.kit, analysis.kit_audit, \
        analysis.master
    if model is None or kit is None or audit is None:
        return
    strengths: dict[str, tuple[int, str]] = {}
    for group in kit.groups:
        entry = master.entry(audit.entries.get(group.key))
        if entry is not None:
            strengths[group.key] = (master_line_strength(master, master.line(entry.start_line))
                                    if entry.kind == "project" else STRENGTH_JOB
                                    if entry.kind == "experience" else STRENGTH_MENTION,
                                    entry.id)
    for req in model.requirements:
        for term in req.terms:
            for key in (term.members or (term.key,)):
                table = model.profile.setdefault(key, {})
                for bullet in kit.bullets.values():
                    if bullet.id in audit.blocked or bullet.group not in strengths:
                        continue
                    credit = hook_credit(key, bullet.plain,
                                         analysis.kit_hooks.get(bullet.id, frozenset()))
                    if not credit:
                        continue
                    strength, entry_id = strengths[bullet.group]
                    proof = Proof(Fraction(strength, 3) * credit, strength, bullet.plain,
                                  master.entry(entry_id).title, "kit", bullet.line, bullet.id)
                    old = table.get(entry_id)
                    if old is None or proof.credit > old.credit:
                        table[entry_id] = proof
            if term.kind == "any_of":
                model.profile[term.key] = _merge_members(term, model.profile)


def _requirement_dict(req: Requirement) -> dict[str, Any]:
    return {
        "name": req.name,
        "canonical_skill": req.key,
        "kind": req.kind,
        "category": req.category,
        "weight": req.weight,
        "status": req.status,
        "matched_via": req.matched_via,
        "jd_lines": req.jd_lines,
        "jd_excerpt": req.jd_excerpt,
        "classification_reason": req.reason,
        "confidence": req.confidence,
        "evidence_count": req.evidence_count,
        "evidence": req.evidence,
        "explanation": req.explanation,
        "negative_marker_conflicts": req.conflicts,
        "members": [SKILL_INDEX[k].display for k in req.members],
    }


def build_report(analysis: Analysis, workspace: Path) -> dict[str, Any]:
    reqs = analysis.requirements
    counts = {status: sum(1 for r in reqs if r.status == status) for status in STATUSES}
    master = analysis.master
    try:
        master_display = master.path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        master_display = master.path.name
    confidence = "high"
    if len(reqs) < 5 or any(r.confidence == "low" for r in reqs):
        confidence = "medium"
    if not reqs or len(reqs) < 3 or analysis.jd.partial_description:
        confidence = "low"
    global_no = [{"skill": SKILL_INDEX[k].display, "lines": v}
                 for k, v in master.global_no.items()]
    scoped = [{"skill": SKILL_INDEX[k].display,
               "lines": sorted({o.line for o in occs}),
               "entries": sorted({(master.entry(o.entry_id) or MasterEntry(
                   "", "", "", "", 0, 0, ())).title for o in occs})}
              for k, occs in master.scoped_no.items()]
    report: dict[str, Any] = {
        "company": analysis.jd.company,
        "role": analysis.jd.role,
        "source_job_description": analysis.jd.display_path,
        "fit_score": analysis.score,
        "verdict": analysis.verdict,
        "primary_domain": analysis.domain["primary"],
        "secondary_domains": analysis.domain["secondary"],
        "strong_matches": [
            {"name": r.name, "category": r.category, "weight": r.weight,
             "matched_via": r.matched_via, "evidence": r.evidence[:3],
             "explanation": r.explanation, "negative_marker_conflicts": r.conflicts}
            for r in reqs if r.status == STATUS_MATCHED],
        "missing_gaps": [
            {"name": r.name, "category": r.category, "weight": r.weight, "status": r.status,
             "jd_lines": r.jd_lines, "explanation": r.explanation}
            for r in reqs if r.status != STATUS_MATCHED],
        "required_skills": [
            {"name": r.name, "category": r.category, "weight": r.weight, "status": r.status}
            for r in reqs if r.category in ("mandatory", "required")],
        "preferred_skills": [
            {"name": r.name, "category": r.category, "weight": r.weight, "status": r.status}
            for r in reqs if r.category == "preferred"],
        "evidence_coverage": {
            "total_requirements": len(reqs),
            "matched_requirements": counts[STATUS_MATCHED],
            "explicitly_no": counts[STATUS_NO],
            "not_evidenced": counts[STATUS_NOT_EVIDENCED],
            "ambiguous": counts[STATUS_AMBIGUOUS],
        },
        "score_breakdown": analysis.breakdown,
        "parsing_warnings": analysis.warnings,
        "score_confidence": confidence,
        "description_completeness": "partial" if analysis.jd.partial_description else "as provided",
        "score_disclaimer": ("The fit score is the weighted share of parsed JD requirements "
                             "backed by positive master-profile evidence. It is not a "
                             "prediction of hiring success."),
        "company_source": analysis.jd.company_source,
        "role_source": analysis.jd.role_source,
        "requirements": [_requirement_dict(r) for r in reqs],
        "unscored_requirement_lines": analysis.unscored,
        "not_required_mentions": analysis.not_required,
        "domain_details": analysis.domain,
        "negative_markers": {"global": global_no, "scoped_conflicts": scoped},
        "master_profile_warnings": master.warnings,
        "role_family": analysis.family.as_dict() if analysis.family else None,
        "resume_kit": None if analysis.kit is None else {
            "heading_line": analysis.kit.heading_line,
            "bullets": len(analysis.kit.bullets),
            "new_bullets": analysis.kit.new_bullets,
            "warnings": analysis.kit.warnings,
            "audit": analysis.kit_audit.as_dict() if analysis.kit_audit else {}},
        "inputs": {
            "pipeline_version": PIPELINE_VERSION,
            "job_description_sha256": analysis.jd.sha256,
            "master_profile": master_display,
            "master_profile_sha256": sha256_text(
                "\n".join(line.raw for line in master.lines)),
        },
        "resume": {"generated": False, "reason": "not attempted"},
    }
    return report


# ---------------------------------------------------------------------------
# Reverse gap analysis (below the threshold, or on request with --analyze-gap)
# ---------------------------------------------------------------------------
# Recommendations never add a claim. They say where a fact would go *if it is
# true*, and what the score would become; the master profile stays the only
# source of career facts and is never edited by the pipeline.
OCCURRENCE_GAP_STATUS = (("gap", "GAP"), ("old", "OLD"), ("untagged", "UNTAGGED"),
                         ("exposure", "EXPOSURE_ONLY"), ("heading", "HEADING_ONLY"),
                         ("negated", "NEGATED"))
GROUP_LABELS = {"ai_ml": "AI/ML", "backend": "backend", "cloud_devops": "cloud/DevOps",
                "data": "data", "databases": "databases", "frontend": "frontend",
                "hardware": "hardware", "observability_security": "observability/security",
                "os": "operating systems", "process": "engineering process",
                "spoken": "languages", "testing": "testing", "tools": "tooling"}
MAX_GAP_RECOMMENDATIONS = 12
WORK_ENTRY_KINDS = frozenset({"experience", "project", "research", "leadership"})


def status_needs_target(status: str) -> bool:
    """Statuses that point at an existing mention (MISSING picks its own target)."""
    return status in ("GAP", "OLD", "UNTAGGED", "UNCONFIRMED", "EXPOSURE_ONLY")


def _projected_score(breakdown: dict[str, Any], extra_weight: int) -> int:
    total = breakdown["total_weight"]
    matched = breakdown["matched_weight"] + extra_weight
    return 0 if total == 0 else (200 * matched + total) // (2 * total)


def _entry_short_name(entry: MasterEntry) -> str:
    return re.split(r":", entry.title, maxsplit=1)[0].strip()


def _gap_master_status(req: Requirement, master: MasterProfile) -> tuple[str, list[int]]:
    """How the master profile currently treats an unmatched requirement."""
    if req.status == STATUS_NO:
        return "NO", [e["line"] for e in req.evidence]
    if req.kind == "years_experience":
        return "YEARS_NOT_INFERRED", []
    if req.kind == "education":
        return ("DEGREE_UNCLEAR" if req.status == STATUS_AMBIGUOUS else "DEGREE_MISMATCH",
                [e["line"] for e in req.evidence])
    keys = [req.key] if req.key else list(req.members)
    if keys and keys[0]:
        expanded = [k for key in keys for k in (key, *SKILL_INDEX[key].members)]
        occs = [o for key in expanded for o in master.occurrences.get(key, [])]
        for kind, label in OCCURRENCE_GAP_STATUS:
            lines = sorted({o.line for o in occs if o.kind == kind})
            if lines:
                return label, lines
        return "MISSING", []  # an all-[NO] group already has status explicitly_no
    lines = [e["line"] for e in req.evidence]
    if not lines:
        return "MISSING", []
    tag = master.line(lines[0]).tag
    return ({TAG_GAP: "GAP", TAG_OLD: "OLD"}.get(tag or "", "UNTAGGED")
            if tag not in POSITIVE_TAGS else "UNCONFIRMED", lines)


def _entry_skills(master: MasterProfile) -> dict[str, set[str]]:
    """Skills with positive evidence per master entry (skills-table refs included)."""
    result: dict[str, set[str]] = {}
    for key, occs in master.occurrences.items():
        if key in master.global_no:
            continue
        for occ in occs:
            if occ.kind != "positive":
                continue
            for entry_id in {occ.entry_id, *master.line(occ.line).refs}:
                if entry_id:
                    result.setdefault(entry_id, set()).add(key)
    return result


def _suggest_entry(master: MasterProfile, keys: Sequence[str],
                   entry_skills: dict[str, set[str]]) -> tuple[MasterEntry | None, list[str]]:
    """The experience or project entry whose evidence is closest to the missing skill."""
    groups = {SKILL_INDEX[k].group for k in keys}
    domains = {d for k in keys for d in SKILL_INDEX[k].domains}
    best: tuple[int, int, MasterEntry | None, list[str]] = (0, 0, None, [])
    for order, entry in enumerate(master.entries):
        if entry.kind not in ("experience", "project"):
            continue
        skills = sorted(entry_skills.get(entry.id, set()) - set(keys),
                        key=lambda k: SKILL_INDEX[k].display.lower())
        related = [k for k in skills if SKILL_INDEX[k].kind == "skill"
                   and SKILL_INDEX[k].group in groups]
        nearby = [k for k in skills if set(SKILL_INDEX[k].domains) & domains]
        score = 2 * len(related) + len(nearby)
        if score > best[0]:
            best = (score, -order, entry, [SKILL_INDEX[k].display for k in (related or nearby)])
    return best[2], best[3][:3]


def build_gap_analysis(analysis: Analysis, trigger: str) -> dict[str, Any]:
    master = analysis.master
    breakdown = analysis.breakdown
    total = breakdown["total_weight"]
    match_score = round(100 * breakdown["matched_weight"] / total, 2) if total else 0.0
    entry_skills = _entry_skills(master)
    rows: list[dict[str, Any]] = []
    for order, req in enumerate(analysis.requirements):
        if req.status == STATUS_MATCHED:
            continue
        status, lines = _gap_master_status(req, master)
        claimable = status not in ("NO", "NEGATED", "YEARS_NOT_INFERRED", "DEGREE_MISMATCH")
        rows.append({"order": order, "req": req, "status": status, "lines": lines,
                     "claimable": claimable})

    def describe(row: dict[str, Any]) -> dict[str, Any]:
        req = row["req"]
        return {"skill": req.name, "requirement_kind": req.kind, "category": req.category,
                "weight": req.weight, "jd_status": req.status, "master_status": row["status"],
                "master_lines": [_evidence_item(master, n) for n in row["lines"][:5]],
                "jd_lines": req.jd_lines, "jd_excerpt": req.jd_excerpt,
                "confidence": req.confidence,
                "score_if_evidenced": _projected_score(breakdown, req.weight)
                if row["claimable"] else None,
                "claimable": row["claimable"]}

    ranked = sorted(rows, key=lambda r: (-r["req"].weight, r["order"]))
    claimable = [r for r in ranked if r["claimable"]]
    unlock: list[dict[str, Any]] = []
    gained = 0
    for row in claimable:
        if _projected_score(breakdown, gained) >= FIT_THRESHOLD:
            break
        gained += row["req"].weight
        unlock.append(row)
    projected = _projected_score(breakdown, gained)
    reachable = projected >= FIT_THRESHOLD
    recommendations: list[dict[str, Any]] = []
    targets: dict[int, MasterEntry | None] = {}
    for row in ranked:
        req = row["req"]
        name = req.name
        lines = row["lines"]
        where = ", ".join(str(n) for n in lines[:3])
        gain = _projected_score(breakdown, req.weight)
        gain_text = (f"that alone would move the score from {analysis.score} to {gain}"
                     if row["claimable"] else "")
        keys = [req.key] if req.key else list(req.members)
        entry = master.entry(master.line(lines[0]).entry_id) if lines else None
        if entry is not None and entry.kind not in WORK_ENTRY_KINDS:
            entry = None  # a skills-table or do-not-claim row is not where work is described
        if entry is None and status_needs_target(row["status"]) and keys and keys[0]:
            entry = _suggest_entry(master, keys, entry_skills)[0]
        status = row["status"]
        action = "do_not_claim"
        if status == "NO":
            entry = None
            text = (f"{name} is marked [NO] in the master profile (line {where}). It is never "
                    f"claimed and no edit is recommended; treat it as a real gap for this role.")
        elif status == "YEARS_NOT_INFERRED":
            action = "not_actionable"
            text = (f"'{name}': years are never inferred from co-op terms. Address the length "
                    f"of your experience in a cover letter or interview instead.")
        elif status in ("DEGREE_MISMATCH", "DEGREE_UNCLEAR"):
            action = "not_actionable" if status == "DEGREE_MISMATCH" else "human_judgment"
            text = f"'{name}': {req.explanation}"
        elif status in ("GAP", "OLD", "UNTAGGED", "UNCONFIRMED"):
            action = "confirm_existing_mention"
            label = {"UNTAGGED": "untagged", "UNCONFIRMED": "not tagged [V]/[R]"}.get(
                status, f"tagged [{status}]")
            text = (f"{name} is mentioned on line {where}"
                    + (f" in '{_entry_short_name(entry)}'" if entry else "")
                    + f" but is {label}. If you can confirm it, re-tag that line [V] (or [R] "
                      f"for repository evidence); {gain_text}.")
        elif status == "EXPOSURE_ONLY":
            action = "deepen_evidence"
            text = (f"{name} is exposure-level only (line {where}). It counts once a [V] line "
                    + (f"in '{_entry_short_name(entry)}' " if entry else "")
                    + f"shows hands-on use; {gain_text}.")
        elif status == "NEGATED":
            action = "acknowledge_gap"
            text = (f"The master profile states you have not used {name} (line {where}). Treat "
                    f"it as a real gap for this role; nothing is suggested against your own "
                    f"statement.")
        elif status == "HEADING_ONLY":
            action = "confirm_existing_mention"
            text = (f"{name} appears only in a heading (line {where}). If you have used it, add "
                    f"a [V] line under that entry that shows what you did; {gain_text}.")
        else:
            action = "add_evidence_if_true"
            target, related = (_suggest_entry(master, keys, entry_skills)
                               if keys and keys[0] else (None, []))
            entry = target
            if req.kind == "unknown_named":
                text = (f"'{name}' is outside the skill vocabulary (low-confidence extraction). "
                        f"If it is a real requirement you meet, add a [V] line naming it; "
                        f"{gain_text}.")
            elif target is not None:
                group = GROUP_LABELS.get(SKILL_INDEX[keys[0]].group, "related")
                text = (f"No line in {master.path.name} mentions {name}. If you have real "
                        f"{name} experience, adding it to '{_entry_short_name(target)}' would "
                        f"fit best: that entry already evidences {', '.join(related)} "
                        f"({group} work); {gain_text}. Nothing is claimed until that [V] line "
                        f"exists.")
            else:
                text = (f"No line in {master.path.name} mentions {name}. If you have used it, "
                        f"add a [V] line to the entry where you did; {gain_text}.")
        targets[row["order"]] = entry
        recommendations.append({
            "requirement": name, "category": req.category, "weight": req.weight,
            "action": action, "target_entry": (_entry_short_name(entry) if entry else None),
            "target_entry_id": entry.id if entry else None,
            "score_if_evidenced": gain if row["claimable"] else None,
            "recommendation": text})
    if unlock:
        parts = []
        for row in unlock:
            target = targets.get(row["order"])
            parts.append(row["req"].name + (f" (in '{_entry_short_name(target)}')"
                                            if target else ""))
        verb = "would unlock" if reachable else "would still leave the score below"
        summary = (f"Evidencing {', '.join(parts)}, if true, would raise the score from "
                   f"{analysis.score} to {projected}, which {verb} the {FIT_THRESHOLD} threshold "
                   f"for this {analysis.domain['primary']} role.")
        recommendations.insert(0, {"requirement": None, "category": None, "weight": gained,
                                   "action": "unlock_plan", "target_entry": None,
                                   "target_entry_id": None, "score_if_evidenced": projected,
                                   "recommendation": summary})
    elif analysis.score < FIT_THRESHOLD:
        recommendations.insert(0, {"requirement": None, "category": None, "weight": 0,
                                   "action": "unlock_plan", "target_entry": None,
                                   "target_entry_id": None, "score_if_evidenced": None,
                                   "recommendation": "No claimable gap remains: every unmatched "
                                                     "requirement is [NO] or not inferable."})
    return {
        "company": analysis.jd.company,
        "role": analysis.jd.role,
        "source_job_description": analysis.jd.display_path,
        "trigger": trigger,
        "match_score": match_score,
        "fit_score": analysis.score,
        "threshold": FIT_THRESHOLD,
        "verdict": analysis.verdict,
        "primary_domain": analysis.domain["primary"],
        "missing_critical_skills": [describe(r) for r in ranked
                                    if r["req"].category in ("mandatory", "required")],
        "missing_preferred_skills": [describe(r) for r in ranked
                                     if r["req"].category == "preferred"],
        "unlock_plan": {"reachable": reachable, "projected_score": projected,
                        "requirements": [r["req"].name for r in unlock],
                        "claimable_weight_available": sum(r["req"].weight for r in claimable)},
        "actionable_recommendations": recommendations[:MAX_GAP_RECOMMENDATIONS + 1],
        "policy": ("Recommendations are conditional: add a [V]/[R] line only for work you "
                   "actually did. [NO] skills are never suggested. The pipeline never edits "
                   "the master profile."),
    }


# ---------------------------------------------------------------------------
# Requirement coverage: structured requirements, profile fit and resume fit
# ---------------------------------------------------------------------------
# A finer model next to the skill-coverage fit_score. Every JD bullet becomes a
# typed requirement (hard, core, responsibility, preferred) whose terms are
# vocabulary skills or concepts: practices and responsibilities such as
# "rollout" or "fixing code outside the main code base" that tech names miss.
#
# Evidence strength: 3 shipped in a job, 2 a project with users or tests, 1 a
# skills-table row, coursework or a project without either. Profile fit reads
# [V]/[R] master lines; resume fit reads only the rendered resume, where a
# bullet or the summary proves a term and a skills-list mention earns 0.3.
# Every match quotes the line it relied on, and a deterministic context judge
# rejects false friends ("stress tested", "co-op evaluations", "Autonomous
# Robotics") and negated clauses. No LLM is involved: the pipeline calls none.
REQ_TYPE_WEIGHTS = {"core": WEIGHT_MANDATORY, "responsibility": WEIGHT_REQUIRED,
                    "preferred": WEIGHT_PREFERRED}
STRENGTH_JOB = 3
STRENGTH_PROJECT = 2
STRENGTH_MENTION = 1
SKILLS_ONLY_CREDIT = Fraction(3, 10)       # skills list or project stack line, resume side
CORE_UNMET_CAP = Fraction(9)                # never 10/10 while a core requirement is unmet
HARD_REQUIREMENT_CAP = Fraction(4)          # "with hard requirements" score when one is not met
UNDERSELL_MARGIN = Fraction(1)
COHERENCE_WEIGHT = Fraction(4, 5)   # one coherent body of work counts for more
PROJECT_PROOF = re.compile(r"\b\d[\d,]*\+? (?:\w+ ){0,2}(?:users|signups|tests?|test functions)\b|"
                           r"\bpytest\b", re.IGNORECASE)
DEPENDENT_BULLET = re.compile(
    r"^(?:(?:it|this|that|these|those|they|its|their)\b|"
    r"\w+ (?:it|this|that|these|those|them)\b|"
    r"\w+ the (?:api|report|alerts|tool|script|cleanup|mismatches)\b)", re.IGNORECASE)


def _rx(*patterns: str) -> re.Pattern[str] | None:
    if not patterns:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(patterns) + r")(?![a-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class Concept:
    key: str
    display: str
    synonyms: tuple[str, ...]                 # human-readable, reported per requirement
    jd: re.Pattern[str]                       # asks for it in a job description
    evidence: re.Pattern[str] | None          # proves it in a master or resume line
    exclude: re.Pattern[str] | None = None    # contexts that never prove it
    jd_exclude: re.Pattern[str] | None = None  # JD contexts that do not ask for it
    groups: tuple[str, ...] = ()              # skill groups this concept makes relevant
    title: tuple[str, ...] = ()               # job-title words that make it the focus
    focus: str = ""                           # summary wording when it is the focus
    supersedes: tuple[str, ...] = ()          # vocabulary skills it replaces on a JD line
    both_groups: tuple[str, ...] = ()         # proven by a line naming skills of each group
    generic: bool = False                     # a process word: half weight within a line
    gap_label: str = ""                       # short name when listed as an unmet ask


def _concept(key: str, display: str, synonyms: str, jd: Sequence[str],
             evidence: Sequence[str], **extra: Any) -> Concept:
    for name in ("exclude", "jd_exclude"):
        extra[name] = _rx(*extra.get(name, ()))
    return Concept(key, display, tuple(s.strip() for s in synonyms.split("|")), _rx(*jd),
                   _rx(*evidence), **extra)


CONCEPTS: tuple[Concept, ...] = (
    _concept("customers", "Working with customers",
             "customers|clients|client teams|retail customers|teams shipping software for",
             [r"customers?", r"clients?", r"end users",
              r"public sector (?:customers|organi[sz]ations)"],
             [r"customers?", r"clients?", r"client teams", r"retailers?",
              r"(?:teams|groups) (?:shipping|building) software for",
              r"for \d+ (?:banks|clients|customers|hospitals|retailers)"],
             exclude=[r"customer data list", r"client[- ]side"],
             jd_exclude=[r"client[- ]side", r"clients? librar\w+", r"client sdks?"]),
    _concept("deployment", "Deployment and rollout",
             "deploy|rollout|release|preflight|quality gate|publish|upgrade across pipelines",
             [r"deploy\w*", r"roll[- ]?outs?", r"roll(?:ing)? out", r"releases?", r"go[- ]live"],
             [r"deploy\w*", r"roll(?:ed|ing)? ?out", r"rollouts?", r"release gates?",
              r"publish(?:ed|es|ing)? to", r"preflight", r"quality gates?",
              r"upgrade(?:d)? (?:\w+ )?across", r"promotes? a model"],
             jd_exclude=[r"forward deployed engineers?"], groups=("cloud_devops",),
             title=("deployed", "deployment", "release"), focus="deployment"),
    _concept("on_premises", "On-premises or private cloud",
             "on-premises|on-prem|private cloud|self-hosted|air-gapped",
             [r"on[- ]prem\w*", r"private cloud", r"self[- ]hosted", r"air[- ]gapped"],
             [r"on[- ]prem\w*", r"private cloud", r"self[- ]hosted", r"air[- ]gapped"],
             groups=("cloud_devops", "os"), gap_label="on-premises deployment"),
    _concept("debugging", "Debugging and root cause",
             "debug|troubleshoot|root cause|triage|incident|diagnose|error codes",
             [r"debug\w*", r"troubleshoot\w*", r"root[- ]caus\w*", r"investigat\w* failures?",
              r"fix(?:ing)? issues", r"incidents?", r"diagnos\w*", r"triag\w*"],
             [r"debug\w*", r"troubleshoot\w*", r"root cause", r"error codes?", r"triage",
              r"incident (?:calls|response)", r"diagnos\w*",
              r"(?:fix|fixed|fixing) (?:\w+ )?(?:yaml|java) faults?",
              r"trac(?:ed|ing) (?:the )?\w+ to"],
             supersedes=("troubleshooting", "incident")),
    _concept("code_outside", "Fixing code outside the main code base",
             "fix other teams' code|failing pipelines|route code issues to developers",
             [r"outside (?:of )?(?:the )?main code ?base", r"(?:other|another) teams?'? code",
              r"unfamiliar code(?:bases?)?", r"read and understand"],
             [r"rout(?:e|ed|ing) (?:\w+ ){0,3}to developers",
              r"(?:fix|fixed|fixing) (?:\w+ )?(?:yaml|java) faults?",
              r"(?:other teams'?|developers'?) (?:pipelines|code)", r"failing pipelines"]),
    _concept("full_stack", "Working across the stack",
             "full stack|frontend and backend|across the stack",
             [r"across the stack", r"full[- ]stack", r"front[- ]?end and back[- ]?end",
              r"client[- ]side or server[- ]side"],
             [], groups=("frontend", "backend", "databases"),
             both_groups=("frontend", "backend")),
    _concept("production", "Production systems",
             "in production|production code|prod|stayed in production",
             [r"in production", r"production (?:code|systems?|ml|services?|environments?|models?)",
              r"production[- ]grade", r"through production"],
             [r"production", r"prod", r"live at"],
             exclude=[r"production line", r"production (?:workers|floor)"], generic=True),
    _concept("ai_agents", "AI agents",
             "agents|agentic|tool calling|function calling|MCP",
             [r"agents?", r"agentic", r"agent[- ]based", r"tool (?:use|calling)", r"mcp"],
             [r"agents?", r"agentic", r"tool calling", r"function calling", r"mcp servers?"],
             exclude=[r"user[- ]agents?", r"autonomous (?:vehicle|robotics)"],
             jd_exclude=[r"user[- ]agents?", r"(?:sales|support|service|travel) agents?"],
             groups=("ai_ml",), gap_label="AI agents"),
    _concept("llm_apps", "LLM and generative AI applications",
             "LLM|generative AI|AI applications|RAG|Groq|Llama|Mistral|Ollama",
             [r"llms?", r"large language models?", r"generative ai", r"gen ?ai",
              r"ai (?:applications?|workspace|platforms?|products?|systems?|solutions?)",
              r"foundation models?"],
             [r"llms?", r"large language models?", r"groq", r"llama", r"mistral", r"ollama",
              r"gemini", r"gpt", r"rag", r"ai itinerary planner"],
             groups=("ai_ml",), title=("ai engineer", "llm", "generative ai", "applied ai"),
             focus="LLM applications", supersedes=("genai", "llm")),
    _concept("sensitive_data", "Security and sensitive data",
             "sensitive data|security|compliance|secrets|credentials|regulated",
             [r"sensitive (?:enterprise )?data", r"data security", r"secure(?:ly)?", r"security",
              r"compliance", r"regulated", r"privacy"],
             [r"security compliance", r"masked,? protected", r"secret server",
              r"credential incidents?", r"gmp[- ]regulated", r"bcrypt", r"jwt", r"sast", r"dast",
              r"vulnerabilit\w+", r"constant time", r"secrets?"],
             jd_exclude=[r"security clearance", r"clearance", r"security[- ]first",
                         r"social security"], groups=("observability_security",)),
    _concept("experimentation", "Experimentation",
             "experiments|experiment tracking|ablations",
             [r"experiment\w*"],
             [r"experiments", r"experiment (?:tracking|runs?)", r"tracks? params and metrics",
              r"ablations?"],
             exclude=[r"to experiment"]),
    _concept("online_experiments", "Online experiments (A/B tests)",
             "online experiments|A/B tests|incremental impact|causal inference|holdouts",
             [r"online experiments?", r"a/b test\w*", r"ab test\w*", r"split test\w*",
              r"incremental impact", r"causal inference", r"holdouts?"],
             [r"a/b test\w*", r"online experiments?", r"holdouts?", r"causal inference",
              r"split test\w*"], gap_label="A/B testing"),
    _concept("software_quality", "Tested, quality code",
             "tests|pytest|test suites|quality gates|coverage|eval harness|mypy",
             [r"(?:well[- ])?tested (?:production )?code", r"well[- ]tested", r"testing",
              r"unit tests?", r"test (?:coverage|suites?|automation|frameworks?|design)",
              r"(?:writ\w+|add\w*) (?:\w+ ){0,2}tests",
              r"quality(?: assurance)?", r"maintainable"],
             [r"\d[\d,]*\+? (?:\w+ ){0,2}tests?", r"pytest", r"unit tests?", r"test suites?",
              r"quality gates?", r"validation (?:gates?|suite)", r"\d+% coverage",
              r"tested (?:scripts|the deployment)", r"smoke tested", r"eval harness", r"mypy",
              r"contract tests", r"\d+ test functions"],
             exclude=[r"stress tested", r"co-?op evaluations?"],
             jd_exclude=[r"(?:data|feature|recommendation|model|content) quality",
                         r"test automation", r"test(?:ing)? frameworks?"],
             groups=("testing",), generic=True,
             supersedes=("unit_testing", "integration_testing", "regression_testing",
                         "qa_practice")),
    _concept("end_to_end", "End-to-end ownership",
             "end to end|own initiative|self-initiated|owned|stayed in production",
             [r"end[- ]to[- ]end", r"entire (?:product )?lifecycle", r"ownership",
              r"from (?:conceptuali[sz]ation|problem definition|ideation|design) (?:through|to)",
              r"take ownership", r"own (?:work|the|problems)"],
             [r"end to end", r"own initiative", r"self[- ]initiated", r"stayed in production",
              r"owned", r"from scratch"], generic=True),
    _concept("monitoring", "Monitoring and observability",
             "monitoring|alerting|Prometheus|Grafana|telemetry|health checks|p95 latency",
             [r"monitor\w*", r"observability", r"alerting", r"telemetry"],
             [r"monitor\w*", r"prometheus", r"grafana", r"telemetry",
              r"(?:latency|error|dropped data|uptime|health|pagerduty) alerts?",
              r"alerting (?:on|for|rules?)",
              r"health (?:checks?|reports?)", r"p9\d latency", r"/metrics", r"admin dashboard"],
             groups=("observability_security",), supersedes=("monitoring",)),
    _concept("reliability", "Reliability, latency and cost",
             "reliability|latency|timeouts|outages|cost|free tiers",
             [r"reliab\w*", r"latency", r"performan(?:t|ce)", r"cost", r"uptime", r"scalab\w*",
              r"stab(?:le|ility)", r"flak\w+", r"execution time"],
             [r"p9\d", r"latency", r"timeouts?", r"\$0 a month", r"free (?:tiers?|quotas?)",
              r"cold (?:starts?|restart)", r"keep-alive", r"outages?", r"reliab\w*",
              r"ci failures", r"faster", r"stab(?:le|ility)", r"idempotent"],
             exclude=[r"stable[- ]baselines\w*"],
             jd_exclude=[r"performance reviews?", r"cost of living", r"performance test\w*",
                         r"(?:business|portfolio|product|merchant|checkout|team|financial) "
                         r"performance", r"performance (?:metrics|monitoring|marketing)"],
             generic=True),
    _concept("data_pipelines", "Data and feature pipelines",
             "data pipelines|ETL|ingestion|backfill|feature pipelines",
             [r"data (?:or feature )?pipelines?", r"feature pipelines?", r"etl", r"elt",
              r"data ingestion", r"data processing", r"pipelines? for training"],
             [r"etl", r"data pipelines?", r"pipeline over", r"ingests?", r"ingestion",
              r"backfill", r"feature pipelines?"],
             groups=("data", "databases")),
    _concept("data_quality", "Data quality and validation",
             "validation gates|schema checks|row counts|nulls|duplicates|mismatches",
             [r"data freshness", r"feature quality", r"data quality", r"data validation",
              r"consistency between training and production", r"data leakage"],
             [r"validation gates?", r"row counts", r"(?:tracking )?mismatches", r"spot checks",
              r"schema checks?"]),
    _concept("ml_modeling", "Machine learning models",
             "machine learning|trained|fine-tuned|PPO|behaviour cloning|epochs|PyTorch",
             [r"machine learning", r"ml", r"train(?:ing)? (?:and evaluat\w+ )?(?:ml )?models?",
              r"design, train", r"models? for", r"modell?ing", r"deep learning"],
             [r"fine-tuned", r"trained (?:an? )?(?:ai )?\w+", r"ppo", r"behaviou?r cloning",
              r"epochs", r"model registry", r"yolov\w+", r"pytorch", r"stable-baselines3",
              r"training steps", r"timesteps", r"(?:batch |model )?inference"],
             jd_exclude=[r"statistical model\w*", r"data models?"], groups=("ai_ml",),
             title=("machine learning", "ml engineer", "data scientist", "applied scientist"),
             focus="machine learning", supersedes=("machine_learning",)),
    _concept("model_evaluation", "Model evaluation",
             "eval harness|release gate|evaluated on unseen data|validation suite",
             [r"evaluat\w*", r"evals?"],
             [r"eval harness", r"release gates?", r"evaluated on unseen", r"unseen (?:track|data)",
              r"validation suite", r"within \d+(?:\.\d+)?% (?:mean )?error", r"held[- ]out",
              r"gate[- ]range completion", r"evaluat\w*"],
             exclude=[r"co-?op evaluations?", r"evaluations?: outstanding"],
             jd_exclude=[r"evaluating tradeoffs", r"evaluate (?:candidates|applicants)"],
             groups=("ai_ml",)),
    _concept("policy_learning", "Policy learning (RL)",
             "policy learning|reinforcement learning|PPO|behaviour cloning",
             [r"policy[- ]learning", r"reinforcement learning", r"rl"],
             [r"ppo", r"polic(?:y|ies)", r"reinforcement learning", r"behaviou?r cloning"],
             exclude=[r"privacy polic(?:y|ies)", r"polic(?:y|ies) (?:documents?|manuals?)"],
             groups=("ai_ml",), gap_label="reinforcement learning"),
    _concept("reward_design", "Reward design", "reward design|reward shaping|penalties",
             [r"reward (?:design|shaping|functions?|modell?ing)", r"rewards?"],
             [r"reward\w*", r"penalt\w*", r"penali[sz]\w*"], groups=("ai_ml",)),
    _concept("exploration", "Exploration", "exploration|entropy|log_std",
             [r"exploration", r"explore[- ]exploit"], [r"exploration", r"entropy", r"log_std"],
             groups=("ai_ml",)),
    _concept("bandits", "Contextual bandits", "contextual bandits|multi-armed bandits",
             [r"(?:contextual |multi[- ]armed )?bandits?"],
             [r"(?:contextual |multi[- ]armed )?bandits?"], groups=("ai_ml",), gap_label="contextual bandits"),
    _concept("recommendation", "Recommendation and ranking",
             "recommendation systems|ranking models|personalization",
             [r"recommend\w*", r"ranking", r"personali[sz]\w*", r"recommender"],
             [r"recommend\w*", r"ranking (?:models?|systems?)", r"personali[sz]\w*",
              r"recommender", r"suggests? (?:[\w']+ ){0,4}(?:near|for) (?:the )?users?",
              r"suggests? (?:venues|events|products|items|content|actions)"],
             jd_exclude=[r"recommendations? (?:letters?|from)"], groups=("ai_ml",), gap_label="recommendation systems"),
    _concept("embeddings", "Embeddings and vector search",
             "embeddings|vector search|FAISS|MiniLM|sentence-transformers",
             [r"embeddings?", r"vector (?:search|databases?|stores?)", r"semantic search"],
             [r"embeddings?", r"embedd(?:ed|ing)", r"minilm", r"sentence-transformers", r"faiss",
              r"vector"],
             exclude=[r"embedded (?:systems?|software|firmware|devices?|c|linux|hardware)"],
             groups=("ai_ml",)),
    _concept("structured_extraction", "Structured extraction",
             "structured output|Pydantic schema|JSON checks",
             [r"structured (?:extraction|outputs?|data extraction)", r"information extraction"],
             [r"pydantic (?:doc )?schema", r"json (?:checks|schema)", r"structured (?:llm )?docs",
              r"extracts?"]),
    _concept("ml_tooling", "ML tooling (registry, retraining, rollout)",
             "MLflow|model registry|release gate|training automation",
             [r"training automation", r"retrain\w*", r"model (?:registry|monitoring|serving)",
              r"mlops", r"reusable tooling", r"feature (?:stores?|systems?)",
              r"ml infrastructure"],
             [r"mlflow", r"model registry", r"release gates?", r"tracks? params",
              r"warm-started"],
             groups=("ai_ml",), supersedes=("mlops",)),
    _concept("leadership", "Leading work", "led|mentor|VP of|ran a walkthrough",
             [r"lead", r"leading", r"mentor\w*", r"technical leadership"],
             [r"led", r"mentor\w*", r"vp of", r"ran a \w+ (?:walkthrough|workshop)"],
             jd_exclude=[r"engineering manager", r"team leads? will"], generic=True),
    _concept("collaboration", "Cross-team collaboration",
             "worked with developers|N teams|presented to managers|escalated|coordinated",
             [r"collaborat\w*", r"partner(?:ing)? with", r"cross[- ]functional", r"stakeholders?",
              r"work(?:ing)? (?:closely )?with (?:data scientists|product|engineering|sales|"
              r"marketing|partners|teams|the engineering)", r"data science partners"],
             [r"worked with developers", r"\d+ teams", r"presented (?:it )?to", r"escalated",
              r"with the warehouse", r"shared it with the team", r"to developers",
              r"platform team", r"supply chain lead", r"coordinat\w*", r"collaborator"],
             generic=True),
    _concept("documentation", "Documentation", "runbooks|Confluence|handoff docs",
             [r"documentation", r"runbooks?", r"technical writing", r"technical specifications?",
              r"tech specs?", r"specs", r"design docs?", r"adrs?", r"rfcs?", r"wikis?",
              r"best practices"],
             [r"runbooks?", r"confluence", r"handoff docs", r"document\w*"],
             supersedes=("documentation",), generic=True),
    _concept("automation", "Automation", "automated|auto-remediation|cron|scheduled",
             [r"automat\w*"], [r"automat\w*", r"auto-remediation", r"cron", r"scheduled"],
             jd_exclude=[r"test automation", r"automated tests"], generic=True),
    _concept("problem_solving", "Framing open-ended problems",
             "root cause|own initiative|pivoted|made the case",
             [r"open[- ]ended (?:business )?problems?", r"ambiguous problems?",
              r"identify problems", r"problem definition", r"complex problems",
              r"greatest problems"],
             [r"root cause", r"pivot\w*", r"made the case", r"traced (?:the )?\w+ to"],
             generic=True),
    _concept("integration", "Integration", "integrated|connected to|via the API",
             [r"integrat\w*"],
             [r"integrat\w*", r"connected (?:it|the \w+) to", r"via the [\w ]+ api",
              r"upserted via", r"connected to"],
             jd_exclude=[r"integration (?:and \w+ )?tests?\w*"], generic=True),
)
CONCEPTS += (
    _concept("on_call", "On-call and incident response",
             "on-call|incident calls|incident response",
             [r"on[- ]call", r"incident response", r"pager\w*"],
             [r"on[- ]call", r"incident calls", r"incident response"],
             supersedes=("on_call", "incident"), gap_label="on-call"),
    _concept("test_automation", "Test automation and frameworks",
             "test frameworks|eval harness|test suites|automated tests|contract tests",
             [r"test automation", r"test(?:ing)? frameworks?", r"test(?:ing)? infra\w*",
              r"automated tests", r"test (?:execution|stability|orchestration)",
              r"testing capabilities"],
             [r"eval harness", r"\d[\d,]*\+? (?:\w+ ){0,2}tests", r"pytest",
              r"contract tests", r"validation suite", r"test functions",
              r"validation gates?", r"release gates?", r"preflight (?:stage|script|checks?)"],
             exclude=[r"stress tested", r"co-?op evaluations?"], groups=("testing",),
             title=("test infra", "test", "qa", "sdet", "quality"), focus="test automation",
             supersedes=("test_automation", "unit_testing", "integration_testing",
                         "regression_testing")),
    _concept("e2e_testing", "End-to-end and integration testing",
             "E2E tests|integration tests|contract tests|smoke tests",
             [r"e2e", r"end[- ]to[- ]end (?:tests?|testing|test automation)",
              r"integration (?:and \w+ )?tests?\w*"],
             [r"e2e", r"end[- ]to[- ]end tests?", r"integration tests?", r"contract tests"],
             groups=("testing",), gap_label="end-to-end test automation"),
    _concept("test_design", "Test design and strategy",
             "test design|test strategy|eval harness|validation suite|contract tests",
             [r"test design", r"testing techniques", r"testing methodolog\w*",
              r"test strateg\w*", r"test plans?", r"test cases?"],
             [r"eval harness", r"validation suite", r"contract tests", r"\d+ case",
              r"test cases?", r"(?:conversation|router) tests"],
             groups=("testing",), gap_label="test design"),
    _concept("performance_testing", "Performance and load testing",
             "performance tests|load tests|benchmarks",
             [r"performance test\w*", r"load test\w*", r"stress test\w* (?:software|services?|apis?)"],
             [r"performance test\w*", r"load test\w*", r"benchmark\w*"],
             groups=("testing",), gap_label="performance testing"),
)
CONCEPTS += (
    _concept("ci_pipelines", "CI pipelines", "CI/CD|pipeline stages|preflight|gated runs",
             [r"ci(?:/cd)? pipelines?", r"ci/cd", r"continuous integration",
              r"build pipelines?"],
             [r"ci/cd", r"\.gitlab-ci", r"preflight", r"pipeline runs", r"ci templates?",
              r"shared templates", r"failing pipelines", r"github actions", r"gitlab ci",
              r"ci failures", r"ci stability", r"runners?"],
             groups=("cloud_devops",), supersedes=("cicd",), gap_label="CI pipelines"),
    _concept("llm_grounding", "Grounded LLM output", "grounded|only narrates|picks real",
             [r"grounded in", r"grounding",
              r"grounded (?:recommendations|answers|responses|outputs?|generation)"],
             [r"grounded", r"grounding", r"only narrates", r"picks real \w+", r"retrieval"],
             groups=("ai_ml",)),
)
CONCEPTS += (
    _concept("financial_services", "Financial services industry",
             "banks|bank client teams|financial services|fintech",
             [r"financial services", r"fintech", r"banks?", r"banking",
              r"financial institutions?", r"capital markets"],
             [r"banks?", r"banking", r"rbc", r"scotiabank", r"bmo", r"td", r"bns",
              r"national bank", r"financial services"],
             jd_exclude=[r"bank holidays?", r"food banks?"], gap_label="financial services"),
    _concept("consumer_lending", "Consumer lending and credit",
             "consumer lending|fair lending|credit risk|underwriting|loans",
             [r"consumer lending", r"fair lending", r"lending (?:regulations|principles|products?)",
              r"credit (?:risk|policy|decisions?|products?|underwriting)", r"underwriting",
              r"loans?"],
             [r"lending", r"loans?", r"underwrit\w*", r"credit risk"],
             gap_label="consumer lending"),
    _concept("risk_flagging", "Flagging risks and anomalies",
             "anomalies|risks|fraud|loss prevention|audits|mismatches",
             [r"anomal\w*", r"flag\w* (?:risks?|anomalies|issues)", r"risks? and anomalies",
              r"fraud\w*", r"loss prevention", r"risk (?:analysis|analytics|assessment|management|"
              r"models?|appetite|monitoring)"],
             [r"anomal\w*", r"(?:tracking )?mismatches", r"non-delivery claims", r"audit(?:ing|ed)",
              r"flag(?:ged|s)? (?:\w+ ){0,2}(?:stalled|risks?|mismatches|anomalies)", r"fraud\w*",
              r"loss prevention"],
             gap_label="risk analytics"),
    _concept("data_analysis", "Data analysis and insights",
             "analysis|insights|projected savings|modeled loads|audited records",
             [r"data analy\w*", r"analytics", r"insights?", r"financial analysis",
              r"analy[sz]e (?:data|performance|metrics|repayment\w*|behaviou?r)",
              r"analytical (?:work|lifecycle|frameworks?)"],
             [r"analy[sz](?:is|es|ed|ing|e)", r"insights?", r"projected \$", r"modell?ed",
              r"auditing"],
             exclude=[r"data analyst"], supersedes=("data_analysis",)),
    _concept("metrics_tracking", "Tracking performance and user metrics",
             "KPI dashboards|metrics|DAU and MAU|performance monitoring",
             [r"performance metrics", r"kpis?", r"metrics", r"user behaviou?r", r"engagement",
              r"retention"],
             [r"kpis?", r"dau", r"mau", r"retention", r"metrics", r"failure rate"]),
    _concept("ai_tooling", "AI tools with verified output",
             "Claude Code|AI-assisted development|copilots|verified output",
             [r"ai tool\w*", r"copilots?", r"cursor", r"claude code", r"ai[- ]assisted",
              r"ai[- ]driven (?:solutions|tools)", r"ai fluency"],
             [r"claude code", r"built (?:it )?with (?:ai|claude)", r"copilots?", r"with ai help"],
             supersedes=("claude",), gap_label="AI-assisted development"),
    _concept("statistical_modeling", "Statistical modelling",
             "statistics|statistical models|regression|probability",
             [r"statistic\w*", r"statistical model\w*", r"regression", r"forecast\w*",
              r"hypothesis test\w*"],
             [r"statistic\w*", r"probabilit\w*", r"regression", r"hypothesis test\w*"],
             supersedes=("statistics",), gap_label="statistical modelling"),
    _concept("public_sector", "Public sector and government",
             "public sector|government data|public government APIs",
             [r"public sector", r"government", r"public service", r"civic"],
             [r"public sector", r"government (?:apis?|data|records|sources)",
              r"public government apis?"], gap_label="public sector"),
    _concept("api_integration", "Integrating tools and APIs",
             "APIs|REST calls|integrations with tools and data sources",
             [r"apis", r"api integrations?", r"third[- ]party apis?"],
             [r"apis?", r"rest calls", r"python-gitlab"],
             jd_exclude=[r"rest(?:ful)? apis", r"cohere apis"]),
    _concept("configuration", "Environment configuration and readiness",
             "configured executors|preflight checks|runners meet job requirements",
             [r"configuration(?! files?)", r"configur\w+ management", r"environment setup",
              r"provisioning environments?"],
             [r"configur(?:ed|ation|ing)", r"preflight", r"meets? (?:the )?job requirements",
              r"up to date"]),
    _concept("data_mining", "Data mining", "data mining|pattern discovery",
             [r"data mining", r"mine data"],
             [r"data mining", r"mined", r"(?:pandas|sql) (?:\w+ ){0,2}(?:engine|etl|pipeline) over",
              r"(?:caught|surfaced|found) (?:\w+ ){0,2}\d+ (?:\w+ ){0,2}(?:mismatches|anomal\w*)"],
             gap_label="data mining"),
)
CONCEPT_INDEX = {c.key: c for c in CONCEPTS}
REQUIREMENT_CUES = re.compile(r"\b(?:\d+\+? years|experience (?:in|with)|proficien\w+|"
                              r"hands-on|familiar\w* with|knowledge of|background in|"
                              r"understanding of|ability to|you have)\b", re.IGNORECASE)
FALSE_FRIENDS = re.compile(r"(?<![a-z])(?:autonomous (?:vehicle|robotics)\w*|co-?op evaluations?|"
                           r"stress[- ]tested)(?![a-z])", re.IGNORECASE)
CLAUSE_BREAK = re.compile(r"[;!?]|\.(?=\s|$)|:(?=\s)|\bbut\b", re.IGNORECASE)

HARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clearance", re.compile(r"\b(?:security clearance|top secret|secret clearance|clearance|"
                             r"reliability status)\b", re.IGNORECASE)),
    ("citizenship", re.compile(r"\b(?:must be an? \w+ citizen|citizenship (?:is )?required|"
                               r"permanent residen\w+)\b", re.IGNORECASE)),
    ("work_authorization", re.compile(r"\b(?:authori[sz]ed to work|work authori[sz]ation|"
                                      r"eligible to work|right to work|sponsorship|visa)\b",
                                      re.IGNORECASE)),
    ("location", re.compile(r"\b(?:on[- ]?site|relocat\w+|proximity to|must (?:be )?(?:located|"
                            r"based)|\d+(?:\s*-\s*\d+)?% travel|^\W*location\W*:)", re.IGNORECASE)),
)
CANADA_TERMS = re.compile(r"\b(?:canada|canadian|ontario|quebec|british columbia|alberta|toronto|"
                          r"ottawa|vancouver|montr[eé]al|calgary|waterloo|mississauga)\b",
                          re.IGNORECASE)
US_TERMS = re.compile(r"\b(?:united states|usa|u\.s\.|us remote|remote in united states|"
                      r"san francisco|new york|seattle|chicago|austin|boston|los angeles|denver|"
                      r"atlanta|washington,? d\.?c\.?)\b", re.IGNORECASE)
JD_LOCATION_FIELD = re.compile(r"^\W*(?:\*\*)?location(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+)$",
                               re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class CoverageTerm:
    key: str                  # "skill:<key>", "concept:<key>" or "any:<k1>|<k2>"
    display: str
    kind: str                 # skill | concept | any_of
    members: tuple[str, ...] = ()


@dataclass
class CoverageRequirement:
    id: str
    text: str
    type: str                 # core | responsibility | preferred
    weight: int
    section: str
    jd_line: int
    terms: list[CoverageTerm]
    mode: str = "all_of"      # all_of | any_of


@dataclass
class HardRequirement:
    type: str                 # years | degree | clearance | citizenship | location |
                              # work_authorization | start_date
    text: str
    jd_line: int
    status: str               # met | not_met | unknown
    detail: str
    lines: list[int] = field(default_factory=list)
    affects_fit: bool = True  # work authorization is a separate note: it never caps the fit


@dataclass
class Proof:
    credit: Fraction          # 0..1: strength / 3, or SKILLS_ONLY_CREDIT
    strength: int
    quote: str
    where: str
    role: str                 # master | bullet | summary | skills
    line: int | None = None
    kit_id: str | None = None  # the resume kit bullet that proves it


@dataclass
class CoverageModel:
    requirements: list[CoverageRequirement]
    hard: list[HardRequirement]
    unscored: list[dict[str, Any]]
    region: str
    work_authorization: str
    focus: tuple[str, str] | None          # (term key, display) the summary names
    profile: dict[str, dict[str, Proof]]   # term key -> master entry -> strongest proof
    rejected: dict[str, list[str]]         # judge rejections per term key (profile side)

    def best(self, term_key: str) -> Proof | None:
        return _best(self.profile.get(term_key, {}))

    def term_weights(self) -> dict[str, Fraction]:
        """Requirement weight spread over its terms by term weight (any-of: one term)."""
        weights: dict[str, Fraction] = {}
        for req in self.requirements:
            total = sum((term_weight(t) for t in req.terms), Fraction(0))
            for term in req.terms:
                share = req.weight * term_weight(term) / total
                weights[term.key] = weights.get(term.key, Fraction(0)) + share
        return weights


def term_weight(term: CoverageTerm) -> Fraction:
    """Specific asks count fully; generic process words (production, ownership) count half."""
    concept = CONCEPT_INDEX.get(term.key.split(":", 1)[1]) if term.kind == "concept" else None
    return Fraction(1, 2) if concept is not None and concept.generic else Fraction(1)


def _best(proofs: dict[str, Proof]) -> Proof | None:
    return max(proofs.values(), key=lambda p: (p.credit, p.strength), default=None)


def _merge_members(term: CoverageTerm, table: dict[str, dict[str, Proof]]) -> dict[str, Proof]:
    merged: dict[str, Proof] = {}
    for key in term.members:
        for entry, proof in table.get(key, {}).items():
            if entry not in merged or proof.credit > merged[entry].credit:
                merged[entry] = proof
    return merged


@dataclass
class ResumeLine:
    text: str
    role: str                 # summary | bullet | skills | stack | exposure | other
    section: str              # template section kind
    entry: str = ""
    strength: int = 0
    unit: tuple | None = None  # ("bullet", si, ei, bullet_index) for plan-built lines
    hooks: frozenset[str] = frozenset()  # kit themes and synonyms of the bullet it shows
    kit_id: str | None = None


def _overlaps(pattern: re.Pattern[str] | None, text: str, start: int, end: int) -> str | None:
    if pattern is None:
        return None
    for hit in pattern.finditer(text):
        if hit.start() < end and start < hit.end():
            return hit.group(0)
    return None


def judge_match(concept: Concept | None, text: str, start: int, end: int) -> str | None:
    """None when a match may count; otherwise the reason it was rejected."""
    friend = _overlaps(FALSE_FRIENDS, text, start, end) or (
        _overlaps(concept.exclude, text, start, end) if concept else None)
    if friend:
        return f"'{text[start:end]}' is part of '{friend}'"
    left = max([m.end() for m in CLAUSE_BREAK.finditer(text, 0, start)], default=0)
    right_hit = CLAUSE_BREAK.search(text, end)
    clause = text[left:right_hit.start() if right_hit else len(text)]
    negation = NEGATION_CUES.search(clause)
    if negation:
        return f"negated by '{negation.group(0)}' in '{collapse_ws(clause)[:80]}'"
    return None


def concept_proven(concept: Concept, text: str) -> tuple[bool, list[str]]:
    """Whether a line proves a concept, plus the judge's rejections along the way."""
    rejected: list[str] = []
    if concept.both_groups:
        groups = {SKILL_INDEX[k].group for k in SKILL_FINDER.keys(text)}
        return all(g in groups for g in concept.both_groups), rejected
    if concept.evidence is None:
        return False, rejected
    for match in concept.evidence.finditer(text):
        reason = judge_match(concept, text, match.start(), match.end())
        if reason is None:
            return True, rejected
        rejected.append(reason)
    return False, rejected


def _jd_concepts(text: str) -> list[Concept]:
    found = []
    for concept in CONCEPTS:
        hits = [m for m in concept.jd.finditer(text)
                if not _overlaps(concept.jd_exclude, text, m.start(), m.end())]
        if hits:
            found.append(concept)
    return found


LIST_JOIN = re.compile(r"^(?:,\s*|\s+)or\s+(?:[\w/-]+\s+){0,2}$|"
                       r"^,\s*(?:and\s+)?(?:(?:the|a|an|broader|other|related|wider)\s+)?$",
                       re.IGNORECASE)


def _concept_alternatives(text: str, concepts: Sequence[Concept]) -> list[tuple[str, ...]]:
    """Concepts named as alternatives in one list ("A, B, or C"): any one meets the ask."""
    spans = sorted((m.start(), m.end(), c.key) for c in concepts for m in c.jd.finditer(text)
                   if not _overlaps(c.jd_exclude, text, m.start(), m.end()))
    groups: list[tuple[str, ...]] = []
    run: list[tuple[int, int, str]] = spans[:1]
    joins: list[str] = []

    def close() -> None:
        keys = tuple(dict.fromkeys(k for _s, _e, k in run))
        if len(keys) >= 2 and any("or" in j.lower().split() for j in joins):
            groups.append(keys)

    for span in spans[1:]:
        gap = text[run[-1][1]:span[0]]
        if span[0] >= run[-1][1] and LIST_JOIN.match(gap):
            run.append(span)
            joins.append(gap)
        elif span[0] >= run[-1][1]:
            close()
            run, joins = [span], []
    if run:
        close()
    return groups


def _line_terms(text: str, company_skills: set[str], preferred: bool
                ) -> tuple[list[CoverageTerm], str]:
    """Terms a JD line asks for, and whether they are all needed or any one is."""
    concepts = _jd_concepts(text)
    superseded = {k for c in concepts for k in c.supersedes}
    mentions = []
    for mention in SKILL_FINDER.find(text):
        spec = SKILL_INDEX[mention.key]
        if mention.key in company_skills or mention.key in superseded:
            continue
        if spec.kind == "language" and not LANGUAGE_CUES.search(text):
            continue
        mentions.append(mention)
    alternatives = _concept_alternatives(text, concepts)
    grouped_concepts = {key for group in alternatives for key in group}
    terms: list[CoverageTerm] = [CoverageTerm(f"concept:{c.key}", c.display, "concept")
                                 for c in concepts if c.key not in grouped_concepts]
    for group in alternatives:  # "consumer lending, fintech, or financial services"
        keys = tuple(f"concept:{k}" for k in group)
        names = ", ".join(CONCEPT_INDEX[k].display for k in group)
        terms.append(CoverageTerm("any:" + "|".join(keys), f"One of: {names}", "any_of", keys))
    cue = ALTERNATIVE_CUE.search(text)
    grouped = [m for m in mentions if cue and m.start >= cue.end()]
    if len({m.key for m in grouped}) < 2:
        grouped = []
    seen: set[str] = set()
    for mention in mentions:
        if mention in grouped or mention.key in seen:
            continue
        if _is_parenthetical_member(text, mention, mentions):
            continue
        seen.add(mention.key)
        terms.append(CoverageTerm(f"skill:{mention.key}", SKILL_INDEX[mention.key].display,
                                  "skill"))
    if grouped:
        keys = list(dict.fromkeys(f"skill:{m.key}" for m in grouped))
        names = ", ".join(SKILL_INDEX[k.split(":", 1)[1]].display for k in keys)
        terms.append(CoverageTerm("any:" + "|".join(keys), f"One of: {names}", "any_of",
                                  tuple(keys)))
    mode = "any_of" if preferred and re.search(r"\bor\b", text, re.IGNORECASE) else "all_of"
    return terms, mode


def _job_location_text(jd: JobDescription) -> str:
    fields = [m.group(1) for m in JD_LOCATION_FIELD.finditer(jd.raw_text)]
    return "; ".join(collapse_ws(strip_links(f)).strip("* ") for f in fields)


def job_region(jd: JobDescription) -> str:
    text = _job_location_text(jd) or " ".join(
        l.text for l in jd.lines if HARD_PATTERNS[3][1].search(l.text))
    canada, us = bool(CANADA_TERMS.search(text)), bool(US_TERMS.search(text))
    if not (canada or us):  # fall back to the whole posting
        body = jd.raw_text
        canada, us = bool(CANADA_TERMS.search(body)), bool(US_TERMS.search(body))
        if canada and us:
            return "unknown"
    return "Canada and US" if canada and us else "Canada" if canada else "US" if us else "unknown"


def _master_identity(master: MasterProfile) -> tuple[MasterLine | None, MasterLine | None, str]:
    """(citizenship line, clearance line, home city) from positive master lines."""
    citizenship = clearance = None
    city = ""
    for line in master.lines:
        if line.meta or line.tag not in POSITIVE_TAGS:
            continue
        text = _line_text(line)
        if citizenship is None and re.search(r"\bcitizenship\b|\bcitizen\b", text, re.I):
            citizenship = line
        if clearance is None and re.search(r"\bclearance\b", text, re.I):
            clearance = line
        entry = master.entry(line.entry_id)
        if not city and entry is not None and entry.kind == "identity":
            match = re.match(r"\s*([A-Z][a-z]+(?: [A-Z][a-z]+)*), [A-Z]{2}\b", text)
            if match:
                city = match.group(1)
    return citizenship, clearance, city


US_CLEARANCE = re.compile(r"\b(?:u\.?s\.? (?:government |security )?clearance|ts/sci|dod|"
                          r"public trust|u\.?s\.? citizen\w*)\b", re.I)
NO_SPONSORSHIP = re.compile(r"\b(?:sponsorship (?:is )?not (?:available|provided|offered)|"
                            r"(?:not|unable to|cannot|can't|won't) (?:provide |offer )?"
                            r"(?:visa )?sponsor\w*|no (?:visa )?sponsorship)\b", re.I)
START_DATE = re.compile(r"\b(?:start(?:ing)? (?:date|in|on|by)|available to start|"
                        r"(?:summer|fall|autumn|winter|spring) 20\d\d (?:start|cohort|term)|"
                        r"start(?:s)? (?:in|on) (?:january|february|march|april|may|june|july|"
                        r"august|september|october|november|december))\b", re.I)


def kit_clearance_statement(kit: resume_kit.ResumeKit | None) -> tuple[str, int] | None:
    """The 9.7 clearance wording ("Canadian citizen, willing to obtain clearance")."""
    for rule in (kit.rules if kit else []):
        quoted = re.search(r'"([^"]+)"', rule.text)
        if rule.subject.lower().startswith("clearance") and quoted:
            return quoted.group(1), rule.line
    return None


def _hard_requirements(jd: JobDescription, master: MasterProfile, region: str,
                       kit: resume_kit.ResumeKit | None = None) -> list[HardRequirement]:
    citizenship, clearance, city = _master_identity(master)
    found: list[HardRequirement] = []
    seen: set[str] = set()
    location_text = _job_location_text(jd)
    for line in jd.lines:
        if line.is_heading or line.section_type == "info" or is_pay_text(line.text):
            continue
        text = strip_links(line.text)
        years = YEARS_PATTERN.search(text)
        if years and "years" not in seen and re.search(
                r"experience|background|industry|professional", text, re.I) and \
                line.section_type in ("mandatory", "required", "preferred", "unclassified"):
            needed = int(years.group(1))
            months = experience_months(master)
            status = "not_met" if needed * 12 > months else "unknown"
            found.append(HardRequirement(
                "years", excerpt(text, 200), line.number, status,
                f"Needs {needed}+ years; the master profile shows {months} months of co-op and "
                f"team terms, which are never counted as full-time years."))
            seen.add("years")
        if DEGREE_PATTERN.search(text) and "degree" not in seen and \
                line.section_type in ("mandatory", "required"):
            probe = Requirement("Degree requirement", None, "education", "required",
                                [line.number], excerpt(text, 200), "")
            _match_education(probe, master)
            status = {STATUS_MATCHED: "met", STATUS_NOT_EVIDENCED: "not_met"}.get(probe.status,
                                                                                 "unknown")
            found.append(HardRequirement("degree", excerpt(text, 200), line.number, status,
                                         probe.explanation,
                                         [e["line"] for e in probe.evidence]))
            seen.add("degree")
        for kind, pattern in HARD_PATTERNS:
            if kind in seen or kind == "work_authorization" or not pattern.search(text):
                continue
            seen.add(kind)
            if kind == "clearance":
                willing = kit_clearance_statement(kit)
                canadian = citizenship is not None and "canadian" in \
                    _line_text(citizenship).lower()
                if US_CLEARANCE.search(text) or (region == "US" and not CANADA_TERMS.search(text)):
                    status, lines = "not_met", [citizenship.number] if citizenship else []
                    detail = ("US security clearance requires US citizenship, which the master "
                              "profile does not record: not obtainable.")
                elif canadian and willing is not None and region.startswith("Canada"):
                    status, lines = "met", [citizenship.number]
                    detail = (f"Canadian citizen (master line {citizenship.number}), so eligible; "
                              f"willing to obtain clearance (resume kit line {willing[1]}).")
                elif clearance is not None:
                    status, detail, lines = "unknown", (
                        f"Master line {clearance.number}: '{excerpt(_line_text(clearance), 120)}'"), \
                        [clearance.number]
                elif citizenship is not None and region.startswith("Canada") and \
                        "canadian" in _line_text(citizenship).lower():
                    status, lines = "unknown", [citizenship.number]
                    detail = (f"Canadian citizen (master line {citizenship.number}), so eligible to "
                              f"apply; no clearance held or willingness recorded. Add a [V] line "
                              f"to state willingness to obtain clearance.")
                else:
                    status, detail, lines = "not_met", "No clearance or qualifying citizenship " \
                                                       "in the master profile.", []
                found.append(HardRequirement("clearance", excerpt(text, 200), line.number,
                                             status, detail, lines))
            elif kind == "citizenship":
                owned = _line_text(citizenship).lower() if citizenship else ""
                wanted = re.search(r"\b(canadian|american|u\.?s\.?|british|swiss)\b", text, re.I)
                status = "unknown" if not wanted or not citizenship else (
                    "met" if wanted.group(1).lower().strip(".") in owned else "not_met")
                found.append(HardRequirement("citizenship", excerpt(text, 200), line.number,
                                             status, f"Master: '{excerpt(owned, 120)}'",
                                             [citizenship.number] if citizenship else []))
            elif kind == "location":
                found.append(_location_requirement(text, line.number, location_text, city,
                                                   region))
    if "location" not in seen and location_text:  # only the posting's location field
        number = next((l.number for l in jd.lines
                       if JD_LOCATION_FIELD.search(l.raw)), 0)
        found.append(_location_requirement(f"Location: {location_text}", number,
                                           location_text, city, region))
    start = next((l for l in jd.lines if not l.is_heading and l.section_type != "info"
                  and START_DATE.search(l.text)), None)
    if start is not None:
        available = next((l for l in master.lines if not l.meta and l.tag in POSITIVE_TAGS
                          and re.search(r"\bavailable immediately\b", _line_text(l), re.I)),
                         None)
        found.append(HardRequirement(
            "start_date", excerpt(strip_links(start.text), 200), start.number,
            "met" if available else "unknown",
            f"Available immediately (master line {available.number})." if available else
            "Availability is not recorded in the master profile.",
            [available.number] if available else []))
    if region in ("US", "Canada and US"):
        sponsor = next((l for l in jd.lines if NO_SPONSORSHIP.search(l.text)), None)
        detail = work_authorization_note(master, region)
        if sponsor is not None:
            detail += (f" The posting says no visa sponsorship (JD line {sponsor.number}); "
                       f"confirm TN support with the recruiter.")
        found.append(HardRequirement(
            "work_authorization", excerpt(strip_links(sponsor.text), 200) if sponsor else
            f"Location: {location_text or region}", sponsor.number if sponsor else 0,
            "met" if region == "Canada and US" else "unknown", detail,
            [citizenship.number] if citizenship else [], affects_fit=False))
    return found


def _location_requirement(text: str, number: int, location_text: str, city: str,
                          region: str) -> HardRequirement:
    where = location_text or text
    if city and city.lower() in where.lower():
        status, detail = "met", f"{city} is listed ({where})."
    elif region == "Canada" and "remote" in where.lower():
        status, detail = "met", f"Remote in Canada ({where})."
    else:
        status, detail = "unknown", (f"Home city {city or 'unknown'}; the role is in {where}. "
                                     f"Relocation is not recorded.")
    if re.search(r"\d+(?:\s*-\s*\d+)?% travel", text, re.I):
        detail += " Travel is required; willingness is not recorded."
    return HardRequirement("location", excerpt(text, 200), number, status, detail)


def work_authorization_note(master: MasterProfile, region: str) -> str:
    citizenship, _clearance, _city = _master_identity(master)
    if citizenship is None:
        return "Work authorization is not recorded in the master profile."
    text = _line_text(citizenship)
    where = f"(master line {citizenship.number})"
    if region.startswith("Canada") and re.search(r"canadian", text, re.I):
        note = f"Canadian citizen: no sponsorship needed in Canada {where}."
        if region == "Canada and US":
            note += " US locations need separate authorization."
        return note
    if region == "US":
        tn = re.search(r"[^.]*\bTN\b[^.]*\.", text)
        return (f"US role: {tn.group(0).strip()} {where}" if tn else
                f"US role: authorization is not recorded {where}.")
    return f"{excerpt(text, 160)} {where}"


def _focus(jd: JobDescription, requirements: list[CoverageRequirement]
           ) -> tuple[str, str] | None:
    title = jd.role.lower()
    asked = {t.key for r in requirements for t in r.terms}
    for concept in CONCEPTS:
        if concept.focus and any(word in title for word in concept.title):
            return f"concept:{concept.key}", concept.focus
    for req in sorted(requirements, key=lambda r: -r.weight):
        for term in req.terms:
            concept = CONCEPT_INDEX.get(term.key.split(":", 1)[1]) if term.kind == "concept" \
                else None
            if concept and concept.focus and term.key in asked:
                return term.key, concept.focus
    return None


CONTEXT_WEIGHT = 1   # role-description prose: what the job emphasises, below a listed ask
# "3+ years of experience in test automation." asks for years (a hard requirement); its topic
# is asked again elsewhere, so it is not scored twice. A line that adds more stays scored.
PURE_YEARS_LINE = re.compile(r"^\d+\+?\s*(?:-\s*\d+\s*)?years?\s+of\s+(?:[\w-]+\s+){0,2}"
                             r"experience\s+(?:in|with|as)\s+[^,;:()]+?\.?$", re.IGNORECASE)
ROLE_PROSE_HEADING = re.compile(r"\b(?:why (?:this|the) role|about (?:the|this) (?:role|team|"
                                r"position)|the role|your role|role overview|the opportunity|"
                                r"what you.?ll do|in this role|about the team)\b", re.IGNORECASE)
COMPANY_PROSE_HEADING = re.compile(r"\bwho (?:are we|we are)\b|\babout (?!(?:the|this) "
                                   r"(?:role|team|position))|\bour (?:mission|story)\b",
                                   re.IGNORECASE)
ROLE_PROSE_LINE = re.compile(r"\b(?:you will|you'll|this role|we are seeking|we're seeking|"
                             r"we're looking for|we are looking for)\b", re.IGNORECASE)


def _context_requirements(jd: JobDescription, company_skills: set[str], numbered: int
                          ) -> list[CoverageRequirement]:
    """Role-description prose ("Why this role?", "About the team", "you will ...") as
    weight-1 asks: it says what the job emphasises, and listed bullets never restate it all."""
    found: list[CoverageRequirement] = []
    heading = ""
    for line in jd.lines:
        if line.is_heading:
            heading = line.text
            continue
        if line.is_bullet or line.section_type in ("info", "mandatory", "required", "preferred") \
                or is_pay_text(line.text) or METADATA_LINE.match(line.raw) or \
                COMPANY_PROSE_HEADING.search(heading) or \
                not (ROLE_PROSE_HEADING.search(heading) or ROLE_PROSE_LINE.search(line.text)):
            continue
        for text in re.split(r"(?<=[.!?])\s+(?=[A-Z])", strip_links(line.text)):
            if len(text) < 40:  # one ask per sentence: a paragraph packs several
                continue
            terms, _mode = _line_terms(text, company_skills, False)
            if not terms or all(term_weight(t) < 1 for t in terms):
                continue
            found.append(CoverageRequirement(
                f"R{numbered + len(found) + 1:02d}", collapse_ws(text), "responsibility",
                CONTEXT_WEIGHT, f"{heading or 'introduction'} (role description)", line.number,
                terms))
    return found


def build_coverage_model(jd: JobDescription, master: MasterProfile,
                         kit: resume_kit.ResumeKit | None = None) -> CoverageModel:
    region = job_region(jd)
    company_skills = set(SKILL_FINDER.keys(jd.company))
    sections: dict[int, list[JDLine]] = {}
    current = 0
    for line in jd.lines:
        if line.is_heading:
            current = line.number
            continue
        sections.setdefault(current, []).append(line)
    requirements: list[CoverageRequirement] = []
    unscored: list[dict[str, Any]] = []
    base_type = {"mandatory": "core", "required": "core", "responsibilities": "responsibility",
                 "preferred": "preferred"}
    for heading, lines in sections.items():  # an unlabelled list of asks is a requirements list
        bullets = [l for l in lines if l.is_bullet]
        if bullets and all(l.section_type in ("unclassified", "intro") for l in bullets) and \
                sum(bool(REQUIREMENT_CUES.search(l.text)) for l in bullets) * 5 >= len(bullets) * 2:
            for line in lines:
                line.section_type = "required"
    # Bullets are the canonical list; prose of a type that also has bullets restates them.
    bulleted = {base_type.get(l.section_type) for l in jd.lines if l.is_bullet}
    for _heading, lines in sorted(sections.items()):
        for line in lines:
            kind = base_type.get(line.section_type)
            if kind is None or (kind in bulleted and not line.is_bullet) or \
                    is_pay_text(line.text):
                continue
            text = strip_links(line.text)
            if PURE_YEARS_LINE.match(collapse_ws(text.lstrip("-*• "))):
                unscored.append({"jd_line": line.number, "section": line.section_title,
                                 "text": excerpt(text, 200),
                                 "reason": "years of experience only: a hard requirement"})
                continue
            category, _reason = _clause_category(text, 0, SECTION_DEFAULT_CATEGORY.get(
                line.section_type, "required"))
            req_type = "preferred" if category == "preferred" else (
                "core" if category == "mandatory" else kind)
            terms, mode = _line_terms(text, company_skills, req_type == "preferred")
            if not terms or all(term_weight(t) < 1 for t in terms):
                unscored.append({"jd_line": line.number, "section": line.section_title,
                                 "text": excerpt(text, 200),
                                 "reason": "soft skill or process only" if terms else
                                           "no skill or known concept"})
                continue
            requirements.append(CoverageRequirement(
                f"R{len(requirements) + 1:02d}", collapse_ws(text.lstrip("-*• ")), req_type,
                REQ_TYPE_WEIGHTS[req_type], line.section_title or line.section_type,
                line.number, terms, mode))
    requirements += _context_requirements(jd, company_skills, len(requirements))
    model = CoverageModel(requirements, _hard_requirements(jd, master, region, kit), unscored,
                          region, work_authorization_note(master, region),
                          _focus(jd, requirements), {}, {})
    for req in requirements:
        for term in req.terms:
            for key in (term.members or (term.key,)):
                if key not in model.profile:
                    model.profile[key], rejected = profile_proofs(key, master)
                    if rejected:
                        model.rejected[key] = rejected
            if term.kind == "any_of":
                model.profile[term.key] = _merge_members(term, model.profile)
    return model


# -- evidence ---------------------------------------------------------------
def master_line_strength(master: MasterProfile, line: MasterLine) -> int:
    """3 shipped in a job, 2 a project with users or tests, 1 anything else."""
    cache = master.cache.setdefault("entry_strength", {})
    entry = master.entry(line.entry_id)
    if line.kind == "table" or entry is None:
        return STRENGTH_MENTION
    if entry.id not in cache:
        if entry.kind == "experience":
            cache[entry.id] = STRENGTH_JOB
        elif entry.kind == "project":
            text = " ".join(_line_text(l) for l in master.lines
                            if l.entry_id == entry.id and l.tag in POSITIVE_TAGS)
            cache[entry.id] = STRENGTH_PROJECT if PROJECT_PROOF.search(text) else STRENGTH_MENTION
        else:
            cache[entry.id] = STRENGTH_MENTION
    return cache[entry.id]


def _positive_master_lines(master: MasterProfile) -> list[MasterLine]:
    if "evidence_lines" not in master.cache:
        master.cache["evidence_lines"] = [
            l for l in master.lines if not l.meta and l.tag in POSITIVE_TAGS
            and l.kind in ("bullet", "paragraph", "table")]
    return master.cache["evidence_lines"]


def _master_proofs(master: MasterProfile, line: MasterLine) -> list[tuple[str, Proof]]:
    """(entry id, proof) pairs: a skills-table row speaks for each entry it names."""
    quote = excerpt(_line_text(line), 200)
    if line.kind == "table" and line.refs:
        pairs = []
        for ref in line.refs:
            entry = master.entry(ref)
            if entry is None or not entry.start_line:
                continue
            strength = master_line_strength(master, master.line(entry.start_line))
            pairs.append((ref, Proof(Fraction(strength, 3), strength, quote, entry.title,
                                     "master", line.number)))
        if pairs:
            return pairs
    strength = master_line_strength(master, line)
    entry = master.entry(line.entry_id)
    return [(line.entry_id or line.section, Proof(Fraction(strength, 3), strength, quote,
                                                  entry.title if entry else line.section,
                                                  "master", line.number))]


def profile_proofs(term_key: str, master: MasterProfile) -> tuple[dict[str, Proof], list[str]]:
    """Strongest [V]/[R]/skills-table evidence per master entry, plus judge rejections."""
    kind, key = term_key.split(":", 1)
    found: dict[str, Proof] = {}
    rejected: list[str] = []
    if kind == "skill":
        lines = [master.line(n) for n in _skill_lines(master, key, None)]
    else:
        lines = []
        for line in _positive_master_lines(master):
            if line.kind == "table":
                continue  # inventory rows ("Python | Production | ...") prove skills, not practices
            ok, why = concept_proven(CONCEPT_INDEX[key], _line_text(line))
            rejected += [f"line {line.number}: {reason}" for reason in why]
            if ok:
                lines.append(line)
    for line in lines:
        if line.kind not in ("bullet", "paragraph", "table"):
            continue
        for entry, proof in _master_proofs(master, line):
            old = found.get(entry)
            if old is None or (proof.strength, len(proof.quote)) > (old.strength, len(old.quote)):
                found[entry] = proof  # equal strength: quote the more informative line
    return found, rejected[:5]


def resume_proofs(term_key: str, lines: Sequence[ResumeLine]
                  ) -> tuple[dict[str, Proof], list[str]]:
    """Strongest proof per resume entry (bullets and summary count fully; lists at 0.3)."""
    kind, key = term_key.split(":", 1)
    found: dict[str, Proof] = {}
    rejected: list[str] = []
    for line in lines:
        if line.role not in ("bullet", "summary", "skills", "stack"):
            continue
        if kind == "skill":
            keys = set(SKILL_FINDER.keys(line.text))
            spec = SKILL_INDEX[key]
            ok = key in keys or bool(keys & set(spec.members))
        else:
            ok, why = concept_proven(CONCEPT_INDEX[key], line.text)
            rejected += [f"'{excerpt(line.text, 70)}': {reason}" for reason in why]
        inferred = False
        if not ok and line.role == "bullet" and line.hooks:
            ok = inferred = proves_with_hooks(term_key, line.text, line.hooks)
        if not ok:
            continue
        if line.role in ("bullet", "summary"):
            proof = Proof(Fraction(line.strength, 3) * (HOOK_CREDIT if inferred else 1),
                          line.strength, line.text, line.entry or line.section, line.role,
                          kit_id=line.kit_id)
        else:
            proof = Proof(SKILLS_ONLY_CREDIT, 0, line.text, line.entry or line.section,
                          line.role)
        where = line.entry or line.section
        if where not in found or proof.credit > found[where].credit:
            found[where] = proof
    return found, rejected[:5]


# -- resume text --------------------------------------------------------------
RESUME_SECTION_TITLES = frozenset({
    "summary", "profile", "technical skills", "skills", "experience", "work experience",
    "projects", "research", "leadership", "education", "awards", "additional",
    "certifications"})


def _project_strengths(master: MasterProfile | None) -> dict[str, int]:
    if master is None:
        return {}
    found = {}
    for entry in master.entries:
        if entry.kind == "project" and entry.start_line:
            name = re.split(r"[:(]", entry.title, maxsplit=1)[0].strip().lower()
            found[name] = master_line_strength(master, master.line(entry.start_line))
    return found


def _resume_strength(section: str, entry: str, projects: dict[str, int]) -> int:
    if section == "experience":
        return STRENGTH_JOB
    if section == "projects":
        name = entry.lower()
        for project, strength in projects.items():
            if project and (project == name or project in name or name in project):
                return strength
        return STRENGTH_PROJECT
    return STRENGTH_MENTION


def resume_lines_from_text(text: str, master: MasterProfile | None = None,
                           kit: resume_kit.ResumeKit | None = None,
                           hooks: dict[str, frozenset[str]] | None = None) -> list[ResumeLine]:
    """Structure pdftotext output: sections, entry names, bullets (wrapped lines joined)."""
    projects = _project_strengths(master)
    lines: list[ResumeLine] = []
    section = ""
    entry = ""
    summary: list[str] = []
    previous_blank = True
    expect_entry = True   # the first non-bullet line after a section start or a bullet
    for raw in normalize_text(text).replace("\f", "\n").split("\n"):
        stripped = raw.strip()
        if not stripped:
            previous_blank = True
            continue
        if stripped.lower() in RESUME_SECTION_TITLES:
            section, entry, expect_entry = template_section_kind(stripped), "", True
            previous_blank = True
            continue
        if section == "summary":
            summary.append(stripped)
        elif stripped.startswith("- "):
            lines.append(ResumeLine(stripped[2:].strip(), "bullet", section, entry,
                                    _resume_strength(section, entry, projects)))
            expect_entry = True
        elif lines and lines[-1].role == "bullet" and not previous_blank and \
                stripped[:1].islower():
            lines[-1].text += " " + stripped  # a wrapped bullet continues on the next line
        elif section == "skills" and ":" in stripped:
            lines.append(ResumeLine(stripped, "skills", section))
        elif section == "additional":
            lines.append(ResumeLine(stripped, "exposure", section))
        elif section in ("experience", "projects", "research", "leadership") and (
                expect_entry or " | " in stripped):
            name, _sep, stack = stripped.partition(" | ")
            entry = re.sub(r"\s*\(.*?\)\s*$", "", name).strip()
            expect_entry = False
            if stack:
                lines.append(ResumeLine(stack, "stack", section, entry))
        else:
            lines.append(ResumeLine(stripped, "other", section, entry))
        previous_blank = False
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", " ".join(summary))
    lines[:0] = [ResumeLine(s, "summary", "summary", "", STRENGTH_PROJECT)
                 for s in sentences if s.strip()]
    for line in lines:
        if line.role == "bullet" and kit is not None:
            line.kit_id = identify_kit_bullet(line.text, kit)
            line.hooks = (hooks or {}).get(line.kit_id or "", frozenset())
    return lines


KIT_MATCH_THRESHOLD = 0.75   # share of content words a PDF line shares with a kit bullet


def identify_kit_bullet(text: str, kit: resume_kit.ResumeKit) -> str | None:
    """The kit bullet a resume line shows, by wording (the same fact, maybe older wording)."""
    stems = content_stems(text)
    best: tuple[float, str] | None = None
    for bullet in kit.bullets.values():
        other = content_stems(bullet.plain)
        if not stems or not other:
            continue
        score = len(stems & other) / max(len(stems), len(other))
        if score >= KIT_MATCH_THRESHOLD and (best is None or score > best[0]):
            best = (score, bullet.id)
    return best[1] if best else None


def resume_lines_from_plan(plan: ResumePlan, removed: set[tuple],
                           master: MasterProfile | None = None) -> list[ResumeLine]:
    """The same structure, built from what render_resume emits for this plan."""
    projects = _project_strengths(master)
    lines: list[ResumeLine] = []
    for si, entries in active_model(plan, removed):
        section = plan.template.sections[si]
        kind = template_section_kind(section.title)
        if section.kind == "paragraph":
            if plan.summary_text is not None:
                lines += [ResumeLine(latex_to_plain(s), "summary", "summary", "",
                                     STRENGTH_PROJECT) for s in plan.summary_sentences]
            continue
        if section.kind == "keyvalue":
            for li in plan.kv_order.get(si, range(len(section.kv_lines))):
                items = plan.kv_items.get((si, li), [])
                if items:
                    role = "skills" if kind == "skills" else "exposure"
                    text = f"{section.kv_lines[li].label}: " + ", ".join(
                        latex_to_plain(i) for i in items)
                    lines.append(ResumeLine(text, role, kind))
            continue
        for ei, bullets in entries:
            ep = plan.entries[(si, ei)]
            header = latex_to_plain((ep.header_raw or section.entries[ei].header_raw))
            name, _sep, stack = latex_to_plain(section.entries[ei].args[0]).partition(" | ")
            name = re.sub(r"\s*\(.*?\)\s*$", "", name).strip()
            if kind in TECH_LIST_SECTIONS and " | " in header:
                emph = EMPH_PATTERN.search(ep.header_raw or "")
                if emph:
                    stack = latex_to_plain(read_group(ep.header_raw, emph.end())[0])
                lines.append(ResumeLine(stack, "stack", kind, name))
            strength = _resume_strength(kind, name, projects)
            for cand in bullets:
                lines.append(ResumeLine(cand.plain, "bullet", kind, name, strength,
                                        ("bullet", si, ei, cand.bullet_index), cand.hooks,
                                        cand.kit_id))
    return lines


def dependency_lint(lines: Sequence[ResumeLine], plan: ResumePlan | None = None,
                    kit: resume_kit.ResumeKit | None = None) -> list[str]:
    """A bullet that leans on another (Keep with, or "Drove it ...") must sit directly after
    it, or after another bullet of the same block (S09 after S08, both kept with S07)."""
    problems = []
    for index, line in enumerate(lines):
        if line.role != "bullet":
            continue
        previous = lines[index - 1] if index else None
        if plan is not None and line.unit is not None:
            ep = plan.entries[(line.unit[1], line.unit[2])]
            by_index = {c.bullet_index: c for c in ep.candidates}
            cand = by_index.get(line.unit[3])
            if cand is None or cand.parent_index is None:
                continue
            before = [by_index[p.unit[3]] for p in lines[:index]
                      if p.unit is not None and p.unit[:3] == line.unit[:3]]
            ok = previous is not None and previous.unit is not None and \
                previous.unit[:3] == line.unit[:3] and follows_its_parent(cand, before, by_index)
        elif kit is not None and line.kit_id and kit.bullets[line.kit_id].keep_with:
            parent = kit.bullets[line.kit_id].keep_with
            block = {parent} | {b.id for b in kit.bullets.values() if b.keep_with == parent}
            ok = previous is not None and previous.role == "bullet" and \
                previous.entry == line.entry and previous.kit_id in block
        elif DEPENDENT_BULLET.match(line.text) and line.section not in ("projects", "research"):
            ok = previous is not None and previous.role == "bullet" and \
                previous.entry == line.entry  # under a project header "it" is the project
        else:
            continue
        if not ok:
            problems.append(f"'{excerpt(line.text, 80)}' depends on a bullet that does not "
                            f"directly precede it")
    return problems


# -- scores -------------------------------------------------------------------
def _tenths(value: Fraction) -> float:
    return float(Fraction(int(value * 10 + Fraction(1, 2)), 10))


def _requirement_credit(req: CoverageRequirement, table: dict[str, dict[str, Proof]]
                        ) -> Fraction:
    """Coverage of one requirement in [0, 1].

    Any-of: the best term. All-of with several terms: the mean of (a) each term's
    best evidence anywhere and (b) the best single entry, so one coherent body of
    experience counts more than pieces gathered from unrelated places.
    """
    if not req.terms:
        return Fraction(0)

    def credit(term: CoverageTerm, entry: str | None = None) -> Fraction:
        proofs = table.get(term.key, {})
        if entry is not None:
            proof = proofs.get(entry)
            return proof.credit if proof else Fraction(0)
        best = _best(proofs)
        return best.credit if best else Fraction(0)

    if req.mode == "any_of":
        return max(credit(t) for t in req.terms)
    weights = {t.key: term_weight(t) for t in req.terms}
    total = sum(weights.values(), Fraction(0))
    independent = sum((weights[t.key] * credit(t) for t in req.terms), Fraction(0)) / total
    if len(req.terms) == 1:
        return independent
    entries = {e for t in req.terms for e in table.get(t.key, {})}
    coherent = max((sum((weights[t.key] * credit(t, e) for t in req.terms), Fraction(0)) / total
                    for e in entries), default=Fraction(0))
    return (1 - COHERENCE_WEIGHT) * independent + COHERENCE_WEIGHT * coherent


def _fit_from(model: CoverageModel, table: dict[str, dict[str, Proof]]
              ) -> tuple[Fraction, bool]:
    total = sum((r.weight for r in model.requirements), 0)
    if not total:
        return Fraction(0), False
    covered = sum((r.weight * _requirement_credit(r, table) for r in model.requirements),
                  Fraction(0))
    fit = max(Fraction(1), 10 * covered / total)
    core_unmet = any(r.type == "core" and _requirement_credit(r, table) == 0
                     for r in model.requirements)
    if core_unmet:
        fit = min(fit, CORE_UNMET_CAP)
    return fit, core_unmet


def assess_fit(model: CoverageModel, resume: Sequence[ResumeLine] | None) -> dict[str, Any]:
    """Profile fit, resume fit (when a resume exists), hard flags and calibration."""
    profile_fit, core_unmet = _fit_from(model, model.profile)
    resume_table: dict[str, dict[str, Proof]] = {}
    resume_rejected: dict[str, list[str]] = {}
    if resume is not None:
        for req in model.requirements:
            for term in req.terms:
                for key in (term.members or (term.key,)):
                    if key not in resume_table:
                        resume_table[key], why = resume_proofs(key, resume)
                        if why:
                            resume_rejected[key] = why
                if term.kind == "any_of":
                    resume_table[term.key] = _merge_members(term, resume_table)
    hard_not_met = [h for h in model.hard if h.status == "not_met" and h.affects_fit]
    scorable = any(r.weight for r in model.requirements)
    result: dict[str, Any] = {
        "scale": "1 to 10",
        "profile_fit": _tenths(profile_fit) if scorable else None,
        "profile_fit_with_hard_requirements": (_tenths(
            min(profile_fit, HARD_REQUIREMENT_CAP) if hard_not_met else profile_fit)
            if scorable else None),
        "resume_fit": None,
        "resume_fit_raw": None,
        "resume_fit_with_hard_requirements": None,
        "core_requirement_unmet": core_unmet,
        "underselling": None,
        "hard_requirements": [dataclasses.asdict(h) for h in model.hard],
        "hard_requirement_flags": [f"{h.type}: {h.status}" for h in model.hard
                                   if h.status != "met"],
        "work_authorization_note": model.work_authorization,
        "region": model.region,
        "calibration": [],
    }
    if not scorable:
        result["calibration"].append("no requirement line named a skill or known concept "
                                     "(the lexicon is English); fit is not scorable")
    if resume is not None and scorable:
        raw, _resume_core_unmet = _fit_from(model, resume_table)
        capped = min(raw, profile_fit)
        result.update(resume_fit=_tenths(capped), resume_fit_raw=_tenths(raw),
                      resume_fit_with_hard_requirements=_tenths(
                          min(capped, HARD_REQUIREMENT_CAP) if hard_not_met else capped),
                      underselling=bool(profile_fit - capped > UNDERSELL_MARGIN))
        if raw > profile_fit:
            result["calibration"].append(f"resume fit {_tenths(raw)} capped at profile fit "
                                         f"{_tenths(profile_fit)}")
        if result["underselling"]:
            result["calibration"].append(
                f"resume undersells the profile by {_tenths(profile_fit - capped)} points")
    if core_unmet:
        result["calibration"].append(f"a core requirement has no evidence: capped at "
                                     f"{_tenths(CORE_UNMET_CAP)}")
    rows = []
    for req in model.requirements:
        terms = []
        for term in req.terms:
            p, r = model.best(term.key), _best(resume_table.get(term.key, {}))
            terms.append({
                "key": term.key, "display": term.display, "kind": term.kind,
                "synonyms": list(CONCEPT_INDEX[term.key.split(":", 1)[1]].synonyms)
                if term.kind == "concept" else [term.display],
                "profile": _proof_dict(p), "resume": _proof_dict(r),
                "rejected_matches": (model.rejected.get(term.key, []) +
                                     resume_rejected.get(term.key, []))[:5]})
        profile_credit = _requirement_credit(req, model.profile)
        resume_credit = _requirement_credit(req, resume_table)
        rows.append({
            "id": req.id, "text": req.text, "type": req.type, "weight": req.weight,
            "section": req.section, "jd_line": req.jd_line, "mode": req.mode,
            "profile_coverage": round(float(profile_credit), 3),
            "resume_coverage": round(float(resume_credit), 3) if resume is not None else None,
            "matched": profile_credit > 0, "terms": terms})
    result["requirements"] = rows
    result["unscored_lines"] = model.unscored
    result["top_changes"] = []
    return result


def _proof_dict(proof: Proof | None) -> dict[str, Any] | None:
    if proof is None:
        return None
    return {"credit": round(float(proof.credit), 3), "strength": proof.strength,
            "role": proof.role, "where": proof.where, "line": proof.line, "quote": proof.quote,
            "kit_id": proof.kit_id}


def _text_proves(term_key: str, text: str) -> bool:
    kind, key = term_key.split(":", 1)
    if kind == "concept":
        return concept_proven(CONCEPT_INDEX[key], text)[0]
    if kind == "any":
        return any(_text_proves(member, text) for member in key.split("|"))
    keys = set(SKILL_FINDER.keys(text))
    return key in keys or bool(keys & set(SKILL_INDEX[key].members))


def top_changes(model: CoverageModel, fit: dict[str, Any],
                candidates: Sequence[tuple[str, str, int]] = (),
                rejected: Sequence[tuple[str, str, int, str]] = ()) -> list[dict[str, Any]]:
    """Up to 3 verified changes that would raise resume fit the most, chosen greedily.

    ``candidates`` are (bullet text, entry, strength) the template could still add;
    a term no candidate proves points at the master line that proves it instead.
    """
    weights = model.term_weights()
    total = sum((r.weight for r in model.requirements), 0) or 1
    have: dict[str, Fraction] = {}
    can: dict[str, Fraction] = {}
    display: dict[str, str] = {}
    for row in fit["requirements"]:
        for term in row["terms"]:
            key = term["key"]
            display[key] = term["display"]
            have[key] = Fraction(str(term["resume"]["credit"])) if term["resume"] else Fraction(0)
            can[key] = Fraction(str(term["profile"]["credit"])) if term["profile"] else Fraction(0)
    open_terms = [k for k in weights if can.get(k, 0) > have.get(k, 0)]
    actions: list[tuple[str, dict[str, Fraction]]] = []
    for text, entry, strength, *kit in candidates:
        hooks, kit_id = (kit + [frozenset(), None])[:2]
        proves = {k: min(Fraction(strength, 3) * credit, can[k]) for k in open_terms
                  if (credit := hook_credit(k, text, hooks or frozenset()))}
        if proves:
            label = f"kit bullet {kit_id} " if kit_id else "the verified bullet "
            actions.append((f"Add {label}'{text}' ({entry})", proves))
    reachable = {k for _a, proves in actions for k in proves}
    for text, entry, strength, reason in rejected:  # template bullets the verifier refused
        proves = {k: min(Fraction(strength, 3), can[k]) for k in open_terms
                  if k not in reachable and _text_proves(k, text)}
        if proves:
            actions.append((f"Restore the template bullet '{excerpt(text, 110)}' ({entry}), "
                            f"rejected because {reason}: add a [V] master line that supports "
                            f"it, or reword it", proves))
    reachable |= {k for _a, proves in actions for k in proves}
    for key in open_terms:
        proof = model.best(key)
        if key not in reachable and proof is not None and proof.kit_id:
            actions.append((f"Use kit bullet {proof.kit_id}: '{excerpt(proof.quote, 110)}' (it "
                            f"is outside this job's family or did not fit; add a wording for "
                            f"this family in 9.5 if it applies)", {key: can[key]}))
        elif key not in reachable and proof is not None and proof.line is not None:
            actions.append((f"Turn master line {proof.line} into a bullet: "
                            f"'{excerpt(proof.quote, 110)}'", {key: can[key]}))

    def gain(proves: dict[str, Fraction]) -> Fraction:
        return sum((10 * weights[k] * max(Fraction(0), v - have[k]) / total
                    for k, v in proves.items()), Fraction(0))

    chosen = []
    for _ in range(3):
        best = max(actions, key=lambda a: (gain(a[1]), a[0]), default=None)
        if best is None or gain(best[1]) <= 0:
            break
        chosen.append({"change": best[0], "resume_fit_gain": _tenths(gain(best[1])),
                       "proves": [display[k] for k, v in best[1].items() if v > have[k]]})
        for key, value in best[1].items():
            have[key] = max(have[key], value)
    return chosen

def unused_candidates(plan: ResumePlan, removed: set[tuple], master: MasterProfile | None
                      ) -> list[tuple[str, str, int]]:
    """Verified template bullets the final resume does not show: (text, entry, strength)."""
    projects = _project_strengths(master)
    shown = {line.unit for line in resume_lines_from_plan(plan, removed, master)}
    found = []
    for (si, ei), ep in sorted(plan.entries.items()):
        section = plan.template.sections[si]
        kind = template_section_kind(section.title)
        name = re.sub(r"\s*\(.*?\)\s*$", "",
                      latex_to_plain(section.entries[ei].args[0]).partition(" | ")[0]).strip()
        for cand in ep.candidates:
            if ("bullet", si, ei, cand.bullet_index) not in shown and cand.parent_index is None:
                found.append((cand.plain, name, _resume_strength(kind, name, projects),
                              cand.hooks, cand.kit_id))
    return found


def rejected_candidates(plan: ResumePlan, master: MasterProfile | None
                        ) -> list[tuple[str, str, int, str]]:
    """Template bullets the claim verifier refused: (text, entry, strength, first reason)."""
    projects = _project_strengths(master)
    found = []
    for (si, ei), ep in sorted(plan.entries.items()):
        kind = template_section_kind(plan.template.sections[si].title)
        name = _entry_name(plan, si, ei)
        for item in ep.rejected:
            if item.get("reasons"):
                found.append((item["bullet"], name, _resume_strength(kind, name, projects),
                              item["reasons"][0]))
    return found


def fit_summary(fit: dict[str, Any]) -> dict[str, Any]:
    """The analysis.json view of a fit assessment (the full table is in requirements.json)."""
    keys = ("scale", "profile_fit", "profile_fit_with_hard_requirements", "resume_fit",
            "resume_fit_raw", "resume_fit_with_hard_requirements", "underselling",
            "core_requirement_unmet", "hard_requirement_flags", "work_authorization_note",
            "region", "calibration", "top_changes")
    summary = {key: fit.get(key) for key in keys}
    summary["requirements"] = len(fit["requirements"])
    summary["dependency_lint"] = fit.get("dependency_lint", [])
    summary["selection_check"] = fit.get("selection_check", {})
    summary["rewrites"] = fit.get("rewrites", [])
    return summary


def requirements_document(report: dict[str, Any], fit: dict[str, Any]) -> dict[str, Any]:
    return {"company": report["company"], "role": report["role"],
            "source_job_description": report["source_job_description"],
            **{k: v for k, v in fit.items() if k != "requirements"},
            "requirements": fit["requirements"]}


def score_resume(pdf: Path, analysis: Analysis, report: dict[str, Any],
                 template: ParsedTemplate, tools: ToolSet,
                 runner: Callable[..., subprocess.CompletedProcess],
                 out: Callable[[str], None]) -> int:
    """--score-resume: rate an existing PDF against the job; prints, writes nothing."""
    if not pdf.is_file():
        raise InvalidInputError(f"Resume PDF not found: {pdf}")
    if not tools.pdftotext:
        raise MissingPrerequisiteError("pdftotext is required to read the resume PDF")
    result = runner([tools.pdftotext, "-enc", "UTF-8", str(pdf), "-"], capture_output=True,
                    text=True, timeout=TOOL_TIMEOUT_SECONDS, check=False)
    if result.returncode != 0 or not (result.stdout or "").strip():
        raise InvalidInputError(f"Could not extract text from {pdf}")
    lines = resume_lines_from_text(result.stdout, analysis.master, analysis.kit,
                                   analysis.kit_hooks)
    fit = assess_fit(analysis.coverage, lines)
    plan = build_resume_plan(template, analysis.master, analysis)
    shown = {_alnum(line.text) for line in lines}
    missing = [c for c in unused_candidates(plan, set(), analysis.master)
               if _alnum(c[0]) not in shown]
    fit["top_changes"] = top_changes(analysis.coverage, fit, missing,
                                     rejected_candidates(plan, analysis.master))
    fit["dependency_lint"] = dependency_lint(lines, kit=analysis.kit)
    for line in render_fit_report(report, fit).rstrip("\n").split("\n"):
        out(line)
    return EXIT_OK


def render_fit_report(report: dict[str, Any], fit: dict[str, Any]) -> str:
    """Short Markdown report: scores, flags, the requirement table and top changes."""
    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f}/10"

    out = [f"# Fit report: {report['role']} at {report['company']}", "",
           f"- Profile fit: **{fmt(fit['profile_fit'])}** (ignoring hard requirements); "
           f"with hard requirements: {fmt(fit['profile_fit_with_hard_requirements'])}",
           f"- Resume fit: **{fmt(fit['resume_fit'])}**"
           + (f" (raw {fit['resume_fit_raw']:.1f}; underselling)" if fit.get("underselling")
              else ""),
           f"- Skill-coverage gate (fit_score): {report['fit_score']} -> {report['verdict']}",
           f"- Work authorization: {fit['work_authorization_note']}"]
    for note in fit["calibration"]:
        out.append(f"- Calibration: {note}")
    out += ["", "## Hard requirements", ""]
    if not fit["hard_requirements"]:
        out.append("- None found in the posting.")
    for hard in fit["hard_requirements"]:
        out.append(f"- **{hard['type']}: {hard['status'].replace('_', ' ')}** (JD line "
                   f"{hard['jd_line']}): {hard['detail']}")
    out += ["", "## Requirements", "",
            "| ID | Type | W | Requirement | Profile | Evidence used | Resume | Resume line |",
            "|---|---|---|---|---|---|---|---|"]

    def cell(text: str) -> str:
        return text.replace("|", "/").replace("\n", " ")

    for row in fit["requirements"]:
        evidence, proof_line = [], []
        for term in row["terms"]:
            p, r = term["profile"], term["resume"]
            evidence.append(f"{term['display']}: " + (
                f"L{p['line']} ({p['strength']}) {excerpt(p['quote'], 60)}" if p else "gap"))
            if r:
                proof_line.append(f"{term['display']}: {excerpt(r['quote'], 60)}")
        resume_cov = "n/a" if row["resume_coverage"] is None else f"{row['resume_coverage']:.2f}"
        out.append(f"| {row['id']} | {row['type']} | {row['weight']} | "
                   f"{cell(excerpt(row['text'], 90))} | {row['profile_coverage']:.2f} | "
                   f"{cell('; '.join(evidence))} | {resume_cov} | "
                   f"{cell('; '.join(proof_line) or '-')} |")
    if fit.get("rewrites"):
        out += ["", "## Wording rewrites (rewrites.json, re-verified)", ""]
        for item in fit["rewrites"]:
            out.append(f"- {item['kind']}: '{item['original']}' -> '{item['rewritten']}'")
    check = fit.get("selection_check") or {}
    if check.get("violation"):
        out += ["", f"**Selection check failed:** unmapped bullets "
                    f"{check['unmapped_bullets']} were chosen while requirements "
                    f"{check['uncovered_coverable_requirements']} stayed uncovered."]
    out += ["", "## Top changes that would raise resume fit (verified facts only)", ""]
    if not fit["top_changes"]:
        out.append("- None: the resume already shows every verified match.")
    for number, change in enumerate(fit["top_changes"], 1):
        out.append(f"{number}. {change['change']} (proves {', '.join(change['proves'])}; "
                   f"+{change['resume_fit_gain']:.1f})")
    if fit["unscored_lines"]:
        out += ["", f"_{len(fit['unscored_lines'])} JD lines named no skill or known concept "
                    f"and were not scored (see unscored_lines)._"]
    return "\n".join(out) + "\n"


STRENGTH_LABELS = {3: "3 (job)", 2: "2 (project with users or tests)", 1: "1 (mention)",
                   0: "0.3 (skills list)"}


def render_run_report(report: dict[str, Any], fit: dict[str, Any]) -> str:
    """<base>_report.md: family, scores, flags, the requirement table, what was left out
    and why, the top changes, and the kit audit notes for the master profile."""
    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f}/10"

    def cell(text: str) -> str:
        return text.replace("|", "/").replace("\n", " ")

    resume = report.get("resume", {})
    family = report.get("role_family")
    out = [f"# Run report: {report['role']} at {report['company']}", ""]
    if family:
        out.append(f"- Role family: **{family['code']}** ({family['name']}), confidence "
                   f"{family['confidence']:.2f} by {family['method']}: "
                   + "; ".join(family["reasons"]))
        if family["traits"]:
            out.append(f"  - Traits that 9.7 rules read: {', '.join(family['traits'])}")
    else:
        out.append("- Role family: n/a (legacy mode: no resume kit)")
    out += [f"- Profile fit: **{fmt(fit['profile_fit'])}** (ignoring hard requirements); "
            f"with hard requirements: {fmt(fit['profile_fit_with_hard_requirements'])}",
            f"- Resume fit: **{fmt(fit['resume_fit'])}**"
            + (f" (raw {fit['resume_fit_raw']:.1f}; underselling the profile)"
               if fit.get("underselling") else ""),
            f"- Skill-coverage gate (fit_score): {report['fit_score']} -> {report['verdict']}"]
    out += [f"- Calibration: {note}" for note in fit["calibration"]]
    out += ["", f"**Work authorization:** {fit['work_authorization_note']} "
                f"_(a separate note: it never changes the fit or the verdict)_", "",
            "## Hard requirements", ""]
    hard = {h["type"]: h for h in fit["hard_requirements"]}
    for kind in HARD_REQUIREMENT_TYPES:
        item = hard.get(kind)
        if item is None:
            out.append(f"- {kind.replace('_', ' ')}: not stated in the posting")
        else:
            flag = "" if item["status"] == "met" else " **(flag)**"
            out.append(f"- {kind.replace('_', ' ')}: {item['status'].replace('_', ' ')}{flag} "
                       f"(JD line {item['jd_line']}): {item['detail']}")
    out += ["", "## Requirements", "",
            "| ID | Type | W | Requirement | Matched | Bullet | Resume line | Strength |",
            "|---|---|---|---|---|---|---|---|"]
    for row in fit["requirements"]:
        proofs = [t["resume"] for t in row["terms"] if t["resume"]]
        best = max(proofs, key=lambda p: (p["credit"], p["strength"]), default=None)
        ids = list(dict.fromkeys(p["kit_id"] for p in proofs if p.get("kit_id")))
        resume_cov = row["resume_coverage"]
        matched = ("n/a" if resume_cov is None else
                   f"yes ({resume_cov:.2f})" if resume_cov > 0 else
                   f"no (profile {row['profile_coverage']:.2f})" if row["profile_coverage"] > 0
                   else "gap")
        strength = STRENGTH_LABELS.get(best["strength"], str(best["strength"])) if best else "-"
        quote = excerpt(best["quote"], 90) if best else "-"
        out.append(f"| {row['id']} | {row['type']} | {row['weight']} | "
                   f"{cell(excerpt(row['text'], 70))} | {matched} | {', '.join(ids) or '-'} | "
                   f"{cell(quote)} | {strength} |")
    facts = resume.get("summary_facts") or []
    if facts:
        source = resume.get("summary_source") or {}
        out += ["", "## Summary", "",
                f"From the 9.3 `{source.get('family', '?')}` summary (kit line "
                f"{source.get('kit_line', '?')}); each fact and the requirements it answers:", ""]
        out += [f"- {f['fact']}: {', '.join(f['maps_to']) or 'none (dropped)'}" for f in facts]
    considered = resume.get("considered") or []
    trimmed = (resume.get("fitting") or {}).get("removed") or []
    if considered or trimmed:
        out += ["", "## Bullets considered but dropped", ""]
        by_reason: dict[str, list[str]] = {}
        for item in considered:
            reason = re.sub(r"\s*\(line \d+\)", "", item["reason"])
            by_reason.setdefault(reason, []).append(f"{item['kit_id']} ({item['entry']})")
        for reason, ids in sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            out.append(f"- {reason}: {', '.join(ids)}")
        for item in trimmed:
            out.append(f"- trimmed to fit the page: {item['unit']} ({item['reason']})")
        for item in (resume.get("fitting") or {}).get("swaps_after_fitting") or []:
            out.append(f"- swapped after fitting for more coverage: {item}")
    out += ["", "## Top changes that would raise resume fit (verified facts only)", ""]
    if not fit["top_changes"]:
        out.append("- None: the resume already shows every verified match.")
    for number, change in enumerate(fit["top_changes"], 1):
        out.append(f"{number}. {change['change']} (proves {', '.join(change['proves'])}; "
                   f"+{change['resume_fit_gain']:.1f})")
    notes = resume.get("kit_notes") or {}
    audit = (report.get("resume_kit") or {}).get("audit") or {}
    if notes or audit.get("blocked_bullets"):
        out += ["", "## Kit audit (problems to fix in the master profile)", ""]
        for bid, problems in audit.get("blocked_bullets", {}).items():
            out.append(f"- {bid} is never used: {'; '.join(problems)}")
        for bid, problems in notes.items():
            out.append(f"- {bid} (on this resume): {'; '.join(problems)}")
    gates = resume.get("build_gates")
    if gates:
        out += ["", "## Build checks", "",
                "- Gates: " + ", ".join(f"{name.split('_', 2)[2].replace('_', ' ')} "
                                        f"{gate['status']}" for name, gate in gates.items())]
        layout = gates.get("gate_4_layout", {})
        if layout.get("status") not in (None, "not_run"):
            out.append(f"- Layout: {len(layout.get('wrapped', []))} wrapped bullets, "
                       f"{len(layout.get('overlaps', []))} overlaps, "
                       f"{len(layout.get('big_gaps', []))} blank gaps; bottom space "
                       f"{layout.get('bottom_space_mm')} mm")
            out += [f"- Layout warning: {w}" for w in layout.get("warnings", [])]
        probe = resume.get("wrap_probe") or {}
        if probe:
            out.append(f"- Wrap probe: {probe.get('measured', 0)} lines measured before "
                       f"selection (new bullets first: {', '.join(probe.get('new_first', [])) or '-'}); "
                       f"wrapped: {', '.join(probe.get('wrapped', {})) or 'none'}")
    if fit.get("dependency_lint"):
        out += ["", "**Dependency lint:** " + "; ".join(fit["dependency_lint"])]
    if fit["unscored_lines"]:
        out += ["", f"_{len(fit['unscored_lines'])} JD lines were not scored (soft skills, years "
                    f"only, or no known term; see unscored_lines)._"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Template parsing (template.tex is the formatting source, never a fact source)
# ---------------------------------------------------------------------------
ENTRY_MACROS = {"resumeSubheading": 4, "resumeProjectHeading": 2}
ENTRY_MACRO_PATTERN = re.compile(r"\\(resumeSubheading|resumeProjectHeading)(?![A-Za-z])")
ITEM_PATTERN = re.compile(r"\\resumeItem(?![A-Za-z])")
SECTION_PATTERN = re.compile(r"^[ \t]*\\section\*?(?=\{)", re.MULTILINE)
TEMPLATE_SECTION_KINDS = (
    ("experience", "experience"), ("work", "experience"), ("project", "projects"),
    ("research", "research"), ("leadership", "leadership"), ("volunteer", "leadership"),
    ("education", "education"), ("award", "awards"), ("honor", "awards"),
    ("summary", "summary"), ("profile", "summary"), ("skill", "skills"),
    ("additional", "additional"),
)
MASTER_KIND_FOR_SECTION = {
    "experience": ("experience",), "projects": ("project",), "research": ("research",),
    "leadership": ("leadership",), "education": ("education",), "awards": ("education",),
}


def template_section_kind(title: str) -> str:
    lowered = title.lower()
    for needle, kind in TEMPLATE_SECTION_KINDS:
        if needle in lowered:
            return kind
    return "other"


def _strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", text)


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
        elif text[pos] == "%" and (pos == 0 or text[pos - 1] != "\\"):
            newline = text.find("\n", pos)
            pos = len(text) if newline == -1 else newline + 1
        else:
            break
    return pos


def read_group(text: str, pos: int) -> tuple[str, int]:
    """Read a balanced {...} group starting at text[pos] == '{'."""
    if pos >= len(text) or text[pos] != "{":
        raise TemplateStructureError(f"Expected '{{' at offset {pos} in template")
    depth = 0
    index = pos
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1:index], index + 1
        index += 1
    raise TemplateStructureError(f"Unbalanced braces starting at offset {pos} in template")


def read_args(text: str, pos: int, count: int) -> tuple[list[str], int]:
    args: list[str] = []
    for _ in range(count):
        pos = _skip_space(text, pos)
        arg, pos = read_group(text, pos)
        args.append(arg)
    return args, pos


def latex_to_plain(text: str) -> str:
    """Approximate visible text of a LaTeX fragment (for matching, never output)."""
    placeholders = {"$": "\ue000", "%": "\ue001", "&": "\ue002", "_": "\ue003", "#": "\ue004",
                    "{": "\ue005", "}": "\ue006"}
    text = _strip_comments(text)
    text = re.sub(r"\\href\{[^{}]*\}", "", text)
    for pattern, repl in ((r"\\\(\s*\\mathdollar\s*\\\)", "\ue000"), (r"\\mathdollar", "\ue000"),
                          (r"\\textdollar", "\ue000"), (r"\$\|\$", "|"), (r"\\textbar", "|"),
                          (r"\\\\", " "), (r"\\(?:v|h)space\*?\{[^{}]*\}", " ")):
        text = re.sub(pattern, repl, text)
    for escaped, placeholder in placeholders.items():
        text = text.replace("\\" + escaped, placeholder)
    text = text.replace("~", " ").replace("--", "-")
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\[()\[\]]", " ", text)
    text = text.replace("{", "").replace("}", "").replace("$", "")
    for escaped, placeholder in placeholders.items():
        text = text.replace(placeholder, escaped)
    return collapse_ws(text)


def _only_space_or_comments(text: str) -> bool:
    return not _strip_comments(text).strip()


def parse_template(path: Path) -> ParsedTemplate:
    """Split template.tex into preamble, header, typed sections and trailer."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MissingPrerequisiteError(f"Template not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingPrerequisiteError(f"Template is unreadable ({path}): {exc}") from exc
    begin = re.search(r"^[^%\n]*\\begin\{document\}", text, re.MULTILINE)
    end = re.search(r"^[^%\n]*\\end\{document\}", text, re.MULTILINE)
    if not begin or not end or end.start() < begin.end():
        raise TemplateStructureError(
            f"Template {path} lacks a \\begin{{document}} ... \\end{{document}} body")
    begin_index = text.index("\\begin{document}", begin.start())
    end_index = text.index("\\end{document}", end.start())
    preamble = text[:begin_index]
    body = text[begin_index + len("\\begin{document}"):end_index]
    trailer = text[end_index:]
    clean_preamble = _strip_comments(preamble)
    if re.search(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\b(fontspec|unicode-math|polyglossia)\b",
                 clean_preamble) or "\\setmainfont" in clean_preamble:
        engine, reason = "xelatex", "preamble loads fontspec/unicode-math/polyglossia"
    else:
        engine = "pdflatex"
        reason = ("preamble uses pdfTeX-specific setup (inputenc/glyphtounicode/"
                  "\\pdfgentounicode)" if re.search(r"inputenc|glyphtounicode|\\pdfgentounicode",
                                                     clean_preamble)
                  else "no Unicode-engine packages detected")
    starts = [m.start() for m in SECTION_PATTERN.finditer(body)]
    if not starts:
        raise TemplateStructureError(f"Template {path} has no \\section{{...}} blocks")
    header = body[:starts[0]]
    sections: list[TemplateSection] = []
    for index, start in enumerate(starts):
        stop = starts[index + 1] if index + 1 < len(starts) else len(body)
        chunk = body[start:stop]
        open_brace = chunk.index("{")
        title_raw, after = read_group(chunk, open_brace)
        sections.append(_parse_section(latex_to_plain(title_raw), title_raw, chunk, after))
    return ParsedTemplate(path=path, preamble=preamble, header=header, sections=sections,
                          trailer=trailer, engine_requirement=engine, engine_reason=reason)


def _parse_section(title: str, title_raw: str, chunk: str, after: int) -> TemplateSection:
    heading = chunk[:after]
    rest = chunk[after:]
    if "\\resumeSubHeadingListStart" in rest:
        return _parse_list_section(title, heading, rest)
    if re.search(r"\\textbf\{[^{}]*\}\s*\{\s*:", rest):
        return _parse_kv_section(title, heading, rest)
    if re.search(r"\\item\s*\\small\s*\{", rest):
        return _parse_paragraph_section(title, heading, rest)
    raise TemplateStructureError(
        f"Template section '{title}' has an unrecognized structure; its claims cannot be "
        f"verified, so the pipeline refuses to guess")


def _parse_list_section(title: str, heading: str, rest: str) -> TemplateSection:
    start_token, end_token = "\\resumeSubHeadingListStart", "\\resumeSubHeadingListEnd"
    start = rest.index(start_token) + len(start_token)
    end = rest.rfind(end_token)
    if end == -1 or end < start:
        raise TemplateStructureError(f"Section '{title}' opens a heading list but never closes it")
    inner = rest[start:end]
    section = TemplateSection(title=title, kind="list", raw_body=rest,
                              prefix=heading + rest[:start], suffix=rest[end:])
    macros = list(ENTRY_MACRO_PATTERN.finditer(inner))
    if not macros:
        raise TemplateStructureError(f"Section '{title}' has a heading list without entries")
    if not _only_space_or_comments(inner[:macros[0].start()]):
        raise TemplateStructureError(f"Unexpected content before the first entry in '{title}'")
    for index, match in enumerate(macros):
        macro = match.group(1)
        args, pos = read_args(inner, match.end(), ENTRY_MACROS[macro])
        stop = macros[index + 1].start() if index + 1 < len(macros) else len(inner)
        tail = inner[pos:stop]
        bullets, has_list = _parse_item_list(title, tail)
        section.entries.append(TemplateEntry(
            macro=macro, header_raw=inner[match.start():pos], args=args, bullets=bullets,
            index=index, has_item_list=has_list))
    return section


def _parse_item_list(title: str, tail: str) -> tuple[list[TemplateBullet], bool]:
    start_token, end_token = "\\resumeItemListStart", "\\resumeItemListEnd"
    if start_token not in tail:
        if not _only_space_or_comments(tail):
            raise TemplateStructureError(f"Unexpected content after an entry header in '{title}'")
        return [], False
    start = tail.index(start_token)
    end = tail.find(end_token, start)
    if end == -1:
        raise TemplateStructureError(f"Unclosed item list in section '{title}'")
    if not (_only_space_or_comments(tail[:start])
            and _only_space_or_comments(tail[end + len(end_token):])):
        raise TemplateStructureError(f"Unexpected content around an item list in '{title}'")
    body = tail[start + len(start_token):end]
    bullets: list[TemplateBullet] = []
    pos = 0
    for match in ITEM_PATTERN.finditer(body):
        if match.start() < pos:
            continue
        if not _only_space_or_comments(body[pos:match.start()]):
            raise TemplateStructureError(f"Unexpected content between bullets in '{title}'")
        (arg,), pos = read_args(body, match.end(), 1)
        bullets.append(TemplateBullet(raw=arg.strip(), plain=latex_to_plain(arg),
                                      index=len(bullets)))
    if not _only_space_or_comments(body[pos:]):
        raise TemplateStructureError(f"Unexpected content after the last bullet in '{title}'")
    return bullets, True


def _parse_kv_section(title: str, heading: str, rest: str) -> TemplateSection:
    section = TemplateSection(title=title, kind="keyvalue", raw_body=rest)
    pattern = re.compile(r"\\textbf\{")
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        match = pattern.search(rest, pos)
        if not match:
            break
        label_raw, after_label = read_group(rest, match.end() - 1)
        value_start = _skip_space(rest, after_label)
        if value_start >= len(rest) or rest[value_start] != "{":
            pos = after_label
            continue
        value_raw, after_value = read_group(rest, value_start)
        if not value_raw.lstrip().startswith(":"):
            pos = after_value
            continue
        items = _split_items(value_raw.lstrip()[1:].strip())
        section.kv_lines.append(KVLine(label_raw=label_raw, label=latex_to_plain(label_raw),
                                       items=items))
        spans.append((match.start(), after_value))
        pos = after_value
    if not spans:
        raise TemplateStructureError(f"Section '{title}' has no key/value lines")
    for (_s1, e1), (s2, _e2) in zip(spans, spans[1:]):
        if not re.fullmatch(r"\s*\\\\\s*", rest[e1:s2]):
            raise TemplateStructureError(
                f"Unexpected content between key/value lines in section '{title}'")
    separator = rest[spans[0][1]:spans[1][0]] if len(spans) > 1 else " \\\\\n        "
    section.kv_wrapper = (heading + rest[:spans[0][0]], rest[spans[-1][1]:])
    section.prefix = separator
    return section


def _split_items(value: str) -> list[str]:
    items: list[str] = []
    depth = 0
    current = ""
    for char in value:
        if char in "({":
            depth += 1
        elif char in ")}":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    return items


def _parse_paragraph_section(title: str, heading: str, rest: str) -> TemplateSection:
    match = re.search(r"\\item\s*\\small\s*(?=\{)", rest)
    assert match is not None
    text, after = read_group(rest, match.end())
    section = TemplateSection(title=title, kind="paragraph", raw_body=rest)
    section.paragraph_wrapper = (heading + rest[:match.end()] + "{", "}" + rest[after:])
    section.paragraph_text = text
    return section


# ---------------------------------------------------------------------------
# Claim verification: every template phrase must be backed by master evidence
# ---------------------------------------------------------------------------
STANDALONE_NUMBER = re.compile(r"(?<![A-Za-z0-9.,:])\d+(?:[.,:]\d+)*(?![A-Za-z0-9])")
ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


@dataclass
class EvidenceScope:
    entry_id: str | None
    line_numbers: list[int]
    texts: dict[int, str]
    stems: set[str]
    tokens: set[str]
    skills: set[str]
    numbers: dict[str, list[int]]


@dataclass
class ClaimCheck:
    ok: bool
    reasons: list[str]
    codes: list[str]
    support: float
    source_lines: list[int]
    skills: list[str]


def _normalize_number(value: str) -> str:
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", value):
        return value.replace(",", "")
    return value


def _line_text(line: MasterLine) -> str:
    return TAG_PATTERN.sub(" ", line.content).replace("**", "").replace("`", "")


def build_scope(master: MasterProfile, entry_id: str | None,
                include_headings: bool = False) -> EvidenceScope:
    """Positive evidence lines for one master entry (or the whole profile).

    Heading and bold-label lines of the entry always add wording context
    (lexical support only). With ``include_headings`` they also count for
    numbers and tokens, which is used to verify entry headers (dates, names),
    never bullets. Skills always require positive lines.
    """
    numbers: list[int] = []
    headings: list[int] = []
    for line in master.lines:
        if line.meta or line.kind in ("blank", "table_sep", "table_header"):
            continue
        in_scope = entry_id is None or line.entry_id == entry_id or entry_id in line.refs
        if not in_scope:
            continue
        if line.kind in ("heading", "bold") and line.tag in (None, TAG_VERIFIED, TAG_REPO):
            if entry_id is not None:
                headings.append(line.number)
            continue
        if line.tag in POSITIVE_TAGS and line.kind in ("bullet", "paragraph", "table"):
            numbers.append(line.number)
    entry = master.entry(entry_id)
    if entry is not None and entry.start_line >= 1 and entry.start_line not in headings:
        if master.line(entry.start_line).kind == "heading":
            headings.append(entry.start_line)
    texts = {n: _line_text(master.line(n)) for n in numbers}
    heading_texts = {n: _line_text(master.line(n)) for n in sorted(headings)}
    stems: set[str] = set(content_stems(entry.title)) if entry else set()
    for text in heading_texts.values():
        stems |= content_stems(text)
    if include_headings:
        texts.update(heading_texts)
        numbers = sorted(set(numbers) | set(heading_texts))
    tokens: set[str] = set()
    nums: dict[str, list[int]] = {}
    for n, text in texts.items():
        stems |= content_stems(text)
        tokens |= {t.lower() for t in ALNUM_RUN.findall(text)}
        for match in STANDALONE_NUMBER.finditer(text):
            nums.setdefault(_normalize_number(match.group(0)), []).append(n)
    line_set = {n for n in numbers if n not in heading_texts}
    skills = {key for key, occs in master.occurrences.items()
              if any(o.kind == "positive" and o.line in line_set for o in occs)}
    for key in list(skills):
        for spec in SKILLS:
            if key in spec.members:
                skills.add(spec.key)
    return EvidenceScope(entry_id, numbers, texts, stems, tokens, skills, nums)


def _has_global_positive(master: MasterProfile, key: str) -> bool:
    if key in master.global_no:
        return False
    if any(o.kind == "positive" for o in master.occurrences.get(key, [])):
        return True
    return any(m not in master.global_no and any(o.kind == "positive"
                                                 for o in master.occurrences.get(m, []))
               for m in SKILL_INDEX[key].members)


def _no_statements_for(master: MasterProfile, entry_id: str | None) -> list[tuple[int, str]]:
    result = []
    for line_no, statement, stmt_entry in master.no_statements:
        entry = master.entry(stmt_entry)
        if stmt_entry == entry_id or (entry is not None and entry.kind == "do_not_claim"):
            if statement:
                result.append((line_no, statement))
    return result


def verify_claim(plain: str, master: MasterProfile, scope: EvidenceScope,
                 allow_exposure: bool = False) -> ClaimCheck:
    """Check skills, numbers, wording support and [NO] echoes for one phrase."""
    issues: list[tuple[str, str]] = []
    mentions = SKILL_FINDER.find(plain)
    skills = sorted({m.key for m in mentions})
    for key in skills:
        display = SKILL_INDEX[key].display
        if key in master.global_no:
            issues.append(("skill", f"mentions '{display}', which is marked [NO]"))
            continue
        scoped = [o for o in master.scoped_no.get(key, []) if o.entry_id == scope.entry_id]
        if scope.entry_id is not None and scoped:
            issues.append(("skill", f"mentions '{display}', which is marked [NO] for this "
                                    f"entry (line {scoped[0].line})"))
            continue
        if key in scope.skills:
            continue
        if allow_exposure and any(o.kind == "exposure" for o in master.occurrences.get(key, [])):
            continue
        if scope.entry_id is None and _has_global_positive(master, key):
            continue
        issues.append(("skill", f"mentions '{display}' without positive evidence in the "
                                f"source entry"))
    words = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[A-Za-z]+", plain)]
    used_lines: list[int] = []
    for match in STANDALONE_NUMBER.finditer(plain):
        value = _normalize_number(match.group(0))
        candidates = scope.numbers.get(value, [])
        if not candidates:
            issues.append(("number", f"number '{match.group(0)}' does not appear in the "
                                     f"source entry"))
            continue
        before = [t for t in words if t[1] <= match.start()][-4:]
        after = [t for t in words if t[0] >= match.end()][:4]
        window = content_stems(" ".join(t[2] for t in before + after))
        if not window:
            used_lines.append(candidates[0])
            continue
        supporting = [n for n in candidates if window & content_stems(scope.texts[n])]
        if supporting:
            used_lines.append(supporting[0])
        else:
            issues.append(("number", f"number '{match.group(0)}' appears in the source entry "
                                     f"but not in the same context"))
    for token in ALNUM_RUN.findall(plain):
        if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
            if token.lower() not in scope.tokens:
                issues.append(("token", f"token '{token}' does not appear in the source entry"))
    claim_stems = content_stems(plain)
    support = 1.0 if not claim_stems else len(claim_stems & scope.stems) / len(claim_stems)
    if support < LEXICAL_SUPPORT_THRESHOLD:
        issues.append(("lexical", f"wording support {support:.2f} is below "
                                  f"{LEXICAL_SUPPORT_THRESHOLD}"))
    claim_numbers = {_normalize_number(m.group(0)) for m in STANDALONE_NUMBER.finditer(plain)}
    for line_no, statement in _no_statements_for(master, scope.entry_id):
        signature = content_stems(statement) | {
            _normalize_number(m.group(0)) for m in STANDALONE_NUMBER.finditer(statement)}
        if len(signature) < 2:
            continue
        overlap = len(signature & (claim_stems | claim_numbers)) / len(signature)
        if overlap >= NO_CLAIM_OVERLAP_THRESHOLD:
            issues.append(("no_echo", f"echoes the [NO] statement on line {line_no}: "
                                      f"'{excerpt(statement, 80)}'"))
    ranked = sorted(scope.line_numbers,
                    key=lambda n: (-len(claim_stems & content_stems(scope.texts[n])), n))
    source = sorted(set(used_lines) | {n for n in ranked[:3]
                                       if len(claim_stems & content_stems(scope.texts[n])) >= 2})
    return ClaimCheck(ok=not issues, reasons=[msg for _c, msg in issues],
                      codes=[code for code, _m in issues], support=round(support, 2),
                      source_lines=source[:6], skills=skills)


def map_template_entry(entry: TemplateEntry, section_kind: str,
                       master: MasterProfile) -> tuple[str | None, str]:
    """Map a template entry to a master entry by URL, then by word overlap."""
    kinds = MASTER_KIND_FOR_SECTION.get(section_kind, ())
    candidates = [e for e in master.entries if e.kind in kinds] or list(master.entries)
    urls = [u.lower().split("://", 1)[-1].rstrip("/")
            for u in re.findall(r"\\href\{([^{}]*)\}", entry.header_raw)]
    header_plain = latex_to_plain(entry.header_raw)
    header_stems = content_stems(header_plain)
    scored: list[tuple[int, int, int, str]] = []
    for order, cand in enumerate(candidates):
        text = " ".join(master.line(n).raw for n in range(cand.start_line, cand.end_line + 1)
                        if n <= len(master.lines))
        lowered = text.lower()
        url_hit = any(url and url in lowered for url in urls)
        title_overlap = len(header_stems & content_stems(cand.title))
        body_overlap = len(header_stems & content_stems(text))
        score = (100 if url_hit else 0) + 3 * title_overlap + body_overlap
        scored.append((score, title_overlap, -order, cand.id))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < 2:
        return None, f"no master entry matches '{excerpt(header_plain, 60)}'"
    if len(scored) > 1 and scored[0][:2] == scored[1][:2]:
        return None, (f"ambiguous master entry for '{excerpt(header_plain, 60)}' "
                      f"({scored[0][3]} vs {scored[1][3]})")
    return scored[0][3], ""


# ---------------------------------------------------------------------------
# The resume kit (section 9 of the master profile): loading and the fact audit
# ---------------------------------------------------------------------------
# The kit is the career owner's curated wording, written from the [V]/[R] facts
# above it. The engine renders kit text verbatim and audits every piece of it:
# a hard problem (a [NO] or unevidenced tool, a [NO] echo, a number found in no
# line of the entry and not a sum of its verified counts) keeps the item off the
# page; a soft note (a paraphrase, a tool evidenced in another entry, a number
# from an untagged table row or a sum) is rendered and reported for review.
KIT_ENTRY_KINDS = {"experience": "experience", "project": "project", "research": "research",
                   "leadership": "leadership"}
DERIVED_SUM_MAX_TERMS = 4


@dataclass
class KitAudit:
    entries: dict[str, str | None]             # kit group key -> master entry id
    blocked: dict[str, list[str]]              # bullet id -> hard problems (never rendered)
    notes: dict[str, list[str]]                # bullet id -> soft notes (rendered, reported)
    summary_notes: dict[str, list[str]]        # family -> notes on its 9.3 summary
    summary_blocked: dict[str, list[str]]
    row_notes: dict[str, list[str]]            # skills item -> notes
    row_blocked: dict[str, list[str]]
    synonym_ids: list[tuple[str, ...]]         # bullets each 9.6 row speaks for

    def as_dict(self) -> dict[str, Any]:
        return {"blocked_bullets": self.blocked, "bullet_notes": self.notes,
                "summary_notes": self.summary_notes, "blocked_summaries": self.summary_blocked,
                "skills_notes": self.row_notes, "blocked_skills": self.row_blocked}


def load_kit(master: MasterProfile) -> resume_kit.ResumeKit:
    try:
        return resume_kit.parse_kit([line.raw for line in master.lines])
    except resume_kit.KitError as exc:
        raise MissingPrerequisiteError(
            f"The resume kit in {master.path} is invalid ({len(exc.problems)} problem(s)): "
            + "; ".join(exc.problems[:8])) from exc


def map_kit_group(group: resume_kit.KitGroup, master: MasterProfile) -> str | None:
    """The master entry a kit role or project describes (word overlap within its kind)."""
    kind = KIT_ENTRY_KINDS[group.kind]
    stems = content_stems(group.header)
    scored = sorted(((len(stems & content_stems(e.title)), -i, e.id)
                     for i, e in enumerate(master.entries) if e.kind == kind), reverse=True)
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][2]


def _number_lines(master: MasterProfile, entry_id: str | None, value: str,
                  tags: frozenset[str | None]) -> list[int]:
    """Lines of the entry (or the whole profile) holding a number, by effective tag."""
    found = []
    for line in master.lines:
        if line.meta or line.kind in ("blank", "table_sep", "table_header", "heading") or \
                line.tag not in tags or (entry_id is not None and line.entry_id != entry_id):
            continue
        if value in {_normalize_number(m.group(0))
                     for m in STANDALONE_NUMBER.finditer(_line_text(line))}:
            found.append(line.number)
    return found


def _counted_noun(text: str, end: int) -> str | None:
    """Stem of the noun a number counts ("17 tests" -> "test"), within 4 words of it."""
    for word in re.findall(r"[A-Za-z]+", re.split(r"[;.]", text[end:], maxsplit=1)[0])[:4]:
        if word.lower() not in STOPWORDS and word.lower() not in ("and", "or", "of"):
            if re.fullmatch(r"(?:tests?|users?|runners?|subgroups?|groups?|skus?|files?|"
                            r"frames?|requests?|services?|teams?|events?)", word, re.I):
                return stem(word.lower())
    return None


def _derived_sum(value: str, at_least: bool, noun: str | None, master: MasterProfile,
                 entry_id: str | None) -> list[str] | None:
    """Verified counts of the same thing that add up to a stated number: "17 tests" =
    11 + 6 tests; "1,600+ tests" is at most the sum of the verified test counts."""
    target = int(value) if value.isdigit() else None
    if target is None or target < 2 or noun is None:
        return None
    counts: list[int] = []
    for line in master.lines:
        if line.meta or line.tag not in POSITIVE_TAGS or \
                (entry_id is not None and line.entry_id != entry_id):
            continue
        text = _line_text(line)
        for match in STANDALONE_NUMBER.finditer(text):
            raw = _normalize_number(match.group(0))
            if raw.isdigit() and 1 < int(raw) < target and \
                    _counted_noun(text, match.end()) == noun:
                counts.append(int(raw))
    counts = sorted(set(counts), reverse=True)[:12]
    if at_least:  # the largest verified counts, until they reach the stated floor
        picked: list[int] = []
        for count in counts:
            if sum(picked) >= target:
                break
            picked.append(count)
        return [f"{n:,}" for n in picked] if len(picked) >= 2 and sum(picked) >= target \
            else None
    for size in range(2, DERIVED_SUM_MAX_TERMS + 1):
        for combo in itertools.combinations(counts, size):
            if sum(combo) == target:
                return [f"{n:,}" for n in combo]
    return None


def audit_claim(plain: str, master: MasterProfile, entry_id: str | None
                ) -> tuple[list[str], list[str]]:
    """(hard problems, soft notes) for one piece of kit wording against its master entry."""
    scope = build_scope(master, entry_id)
    hard: list[str] = []
    soft: list[str] = []
    for key in sorted({m.key for m in SKILL_FINDER.find(plain)}):
        display = SKILL_INDEX[key].display
        occurrences = master.occurrences.get(key, [])
        if key in master.global_no:
            hard.append(f"names '{display}', which is marked [NO]")
        elif entry_id is not None and any(o.entry_id == entry_id
                                          for o in master.scoped_no.get(key, [])):
            hard.append(f"names '{display}', which is marked [NO] for this entry")
        elif key in scope.skills or (entry_id is None and _has_global_positive(master, key)):
            continue
        elif _has_global_positive(master, key):
            soft.append(f"'{display}' has [V]/[R] evidence in another entry, not this one")
        elif any(o.kind in ("heading", "untagged") for o in occurrences):
            soft.append(f"'{display}' appears only in an untagged heading or note")
        else:
            hard.append(f"names '{display}', which no [V] or [R] line supports")
    words = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[A-Za-z]+", plain)]
    untagged = frozenset({None, TAG_VERIFIED, TAG_REPO, TAG_TABLE})
    for match in STANDALONE_NUMBER.finditer(plain):
        value = _normalize_number(match.group(0))
        before = [t for t in words if t[1] <= match.start()][-4:]
        after = [t for t in words if t[0] >= match.end()][:4]
        window = content_stems(" ".join(t[2] for t in before + after))
        lines = scope.numbers.get(value, [])
        if lines:
            if window and not any(window & content_stems(scope.texts[n]) for n in lines):
                soft.append(f"'{match.group(0)}' appears in this entry in another context "
                            f"(line {lines[0]})")
            continue
        loose = _number_lines(master, entry_id, value, untagged)
        if loose:
            soft.append(f"'{match.group(0)}' comes from untagged line {loose[0]}; tag it [R] or "
                        f"[V] to make it traceable")
            continue
        at_least = plain[match.end():match.end() + 1] == "+"
        parts = _derived_sum(value, at_least, _counted_noun(plain, match.end()), master,
                             entry_id)
        if parts:
            soft.append(f"'{match.group(0)}{'+' if at_least else ''}' is not stated; verified "
                        f"counts add up to {'at least ' if at_least else ''}it "
                        f"({' + '.join(parts)})")
            continue
        hard.append(f"number '{match.group(0)}' is in no line of the master entry and is not a "
                    f"sum of its verified counts")
    for token in ALNUM_RUN.findall(plain):
        if any(c.isdigit() for c in token) and any(c.isalpha() for c in token) and \
                token.lower() not in scope.tokens:
            holders = [l.number for l in master.lines if not l.meta and l.tag in untagged and
                       (entry_id is None or l.entry_id == entry_id) and token.lower() in
                       {t.lower() for t in ALNUM_RUN.findall(_line_text(l))}]
            if holders:
                soft.append(f"'{token}' comes from untagged line {holders[0]}")
            else:
                hard.append(f"'{token}' is in no line of the master entry")
    stems = content_stems(plain)
    support = len(stems & scope.stems) / len(stems) if stems else 1.0
    if support < LEXICAL_SUPPORT_THRESHOLD:
        soft.append(f"paraphrase: {support:.0%} of its words appear in the entry")
    numbers = {_normalize_number(m.group(0)) for m in STANDALONE_NUMBER.finditer(plain)}
    for line_no, statement in _no_statements_for(master, entry_id):
        signature = content_stems(statement) | {
            _normalize_number(m.group(0)) for m in STANDALONE_NUMBER.finditer(statement)}
        if len(signature) >= 2 and len(signature & (stems | numbers)) / len(signature) >= \
                NO_CLAIM_OVERLAP_THRESHOLD:
            hard.append(f"echoes the [NO] statement on line {line_no}: "
                        f"'{excerpt(statement, 80)}'")
    return hard, soft


def audit_kit(kit: resume_kit.ResumeKit, master: MasterProfile) -> KitAudit:
    entries = {g.key: map_kit_group(g, master) for g in kit.groups}
    blocked: dict[str, list[str]] = {}
    notes: dict[str, list[str]] = {}
    for bullet in kit.bullets.values():
        entry_id = entries[bullet.group]
        if entry_id is None:
            blocked[bullet.id] = [f"its header '{kit.group(bullet.group).header}' matches no "
                                  f"master entry"]
            continue
        hard, soft = audit_claim(bullet.plain, master, entry_id)
        if hard:
            blocked[bullet.id] = hard
        if soft:
            notes[bullet.id] = soft
    for bullet in kit.bullets.values():  # a bullet kept with a blocked one goes with it
        if bullet.keep_with in blocked and bullet.id not in blocked:
            blocked[bullet.id] = [f"keeps with {bullet.keep_with}, which is blocked"]
    summary_notes: dict[str, list[str]] = {}
    summary_blocked: dict[str, list[str]] = {}
    for code, (_line, text) in kit.summaries.items():
        hard, soft = audit_claim(resume_kit.BOLD.sub(r"\1", text), master, None)
        if hard:
            summary_blocked[code] = hard
        if soft:
            summary_notes[code] = soft
    row_notes: dict[str, list[str]] = {}
    row_blocked: dict[str, list[str]] = {}
    for _row, (_line, items) in kit.rows.items():
        for item in items:
            hard, soft = audit_claim(item, master, None)
            hard += [] if hard else ([problem] if (problem := _kv_item_problem(
                item, master, False, _positive_text(master))) and "no positive" not in problem
                else [])
            if hard:
                row_blocked[item] = hard
            elif soft or _kv_item_problem(item, master, False, _positive_text(master)):
                row_notes[item] = soft or [f"'{item}' appears only in an untagged heading or "
                                           f"note"]
    synonym_ids = []
    for row in kit.synonyms:
        ids = row.bullet_ids
        if not ids:  # "CI failures cut 40%": the bullets carrying the fact's numbers and words
            fact_numbers = {_normalize_number(m.group(0))
                            for m in STANDALONE_NUMBER.finditer(row.fact)}
            ids = tuple(b.id for b in kit.bullets.values()
                        if fact_numbers <= {_normalize_number(m.group(0))
                                            for m in STANDALONE_NUMBER.finditer(b.plain)}
                        and len(content_stems(row.fact) & content_stems(b.plain)) >= 2)
        synonym_ids.append(ids)
    return KitAudit(entries, blocked, notes, summary_notes, summary_blocked, row_notes,
                    row_blocked, synonym_ids)


# ---------------------------------------------------------------------------
# Role family classification and job traits (rules only; no LLM)
# ---------------------------------------------------------------------------
# Title rules, first match wins. Several matches lower the confidence; no match
# falls back to scoring the requirement lines against each family's vocabulary.
FAMILY_TITLE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("ana", re.compile(r"\banalyst\b|\banalytics engineer\b", re.I), "'Analyst' in the title"),
    ("ml", re.compile(r"\bmachine learning\b|\bml engineer\b|\bapplied ml\b|\bmlops\b", re.I),
     "'Machine Learning' or 'ML Engineer' in the title"),
    ("fde", re.compile(r"\bforward[- ]deployed\b|\bsolutions?\b", re.I),
     "'Forward Deployed' or 'Solutions' in the title"),
    ("test", re.compile(r"\btest(?:ing)?\b|\bsdet\b|\bqa automation\b", re.I),
     "'Test', 'SDET' or 'QA Automation' in the title"),
    ("dev", re.compile(r"\bdevops\b|\bsre\b|\bsite reliability\b|\bplatform\b", re.I),
     "'DevOps', 'SRE' or 'Platform' in the title"),
    ("qa", re.compile(r"\bquality\b|\bcompliance\b", re.I),
     "'Quality' or 'Compliance' in the title, with regulated-industry terms"),
)
REGULATED_TERMS = re.compile(r"\b(?:gmp|gxp|fda|21 cfr|iso 13485|medical devices?|pharma\w*|"
                             r"biotech\w*|clinical|life sciences|regulated)\b", re.I)
FAMILY_BODY_TERMS: dict[str, re.Pattern[str]] = {
    "dev": re.compile(r"\b(?:ci/cd|kubernetes|terraform|infrastructure|devops|sre|reliability|"
                      r"observability|on[- ]call|cloud)\b", re.I),
    "swe": re.compile(r"\b(?:software engineering|backend|frontend|full[- ]stack|apis?|"
                      r"services|code review|distributed systems)\b", re.I),
    "ml": re.compile(r"\b(?:machine learning|models?|training|pytorch|ml|deep learning|"
                     r"recommendation|ranking)\b", re.I),
    "fde": re.compile(r"\b(?:customers?|clients?|llms?|rag|agents?|agentic|generative ai|"
                      r"deploy\w*|solutions)\b", re.I),
    "ana": re.compile(r"\b(?:sql|analytics|analysis|insights|dashboards?|reporting|"
                      r"statistic\w*|metrics|data visuali[sz]ation)\b", re.I),
    "test": re.compile(r"\b(?:test(?:ing|s)?|qa|quality assurance|playwright|pytest|selenium|"
                       r"e2e|automation frameworks?)\b", re.I),
    "qa": re.compile(r"\b(?:gmp|validation|compliance|regulated|sops?|audit\w*|capa|iso)\b",
                     re.I),
    "ops": re.compile(r"\b(?:operations|logistics|program management|scheduling|staffing|"
                      r"supply chain)\b", re.I),
}
JOB_TRAITS: dict[str, re.Pattern[str]] = {
    "hardware": re.compile(r"\b(?:hardware|embedded|firmware|pcbs?|electrical engineering|"
                           r"circuit)\b", re.I),
    "robotics": re.compile(r"\brobot\w*\b", re.I),
    "mechanical": re.compile(r"\b(?:mechanical|cad|solidworks)\b", re.I),
    "frontend": re.compile(r"\b(?:front[- ]?end|ui engineer|web developer|react developer)\b",
                           re.I),
    "healthcare": re.compile(r"\b(?:healthcare|health care|clinical|medical|hospitals?|"
                             r"patients?|pharma\w*|life sciences)\b", re.I),
    "quality": re.compile(r"\b(?:quality|compliance|gmp|validation)\b", re.I),
    "research": re.compile(r"\b(?:research(?:er)?|scientist)\b", re.I),
    "finance": re.compile(r"\b(?:financ\w*|fintech|banks?|banking|lending|credit|payments?|"
                          r"capital markets|insurance)\b", re.I),
    "quant": re.compile(r"\b(?:quant\w*|trading|derivatives|options pricing)\b", re.I),
    "leadership": re.compile(r"\b(?:leadership|lead(?:ing)? (?:a |the )?teams?|mentor\w*|"
                             r"people management|manag(?:e|ing) (?:a )?team)\b", re.I),
}
# Families whose code alone implies a trait the 9.7 rules ask about.
FAMILY_TRAITS = {"ana": {"analyst"}, "ml": {"ml"}, "qa": {"quality"}}


@dataclass
class FamilyDecision:
    code: str
    name: str
    confidence: float
    reasons: list[str]
    method: str                     # title rule | typical title | requirement lines | default
    traits: list[str]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _job_body(jd: JobDescription) -> str:
    return "\n".join(l.text for l in jd.lines if not l.is_heading and l.section_type != "info"
                     and not is_pay_text(l.text))


def job_traits(jd: JobDescription, family: str) -> list[str]:
    """Traits the 9.7 rules and family qualifiers ask about ("hardware roles", "frontend").

    Read from the title and the requirement and responsibility lines only (never benefits or
    the company blurb). Quality and research roles are named in the title or the family.
    """
    title = jd.role
    asks = "\n".join(l.text for l in jd.lines if not l.is_heading and l.section_type in (
        "mandatory", "required", "preferred", "responsibilities") and not is_pay_text(l.text))
    traits = set(FAMILY_TRAITS.get(family, set()))
    for trait, pattern in JOB_TRAITS.items():
        if pattern.search(title) or (trait not in ("quality", "research") and
                                     pattern.search(asks)):
            traits.add(trait)
    return sorted(traits)


def classify_family(jd: JobDescription, kit: resume_kit.ResumeKit) -> FamilyDecision:
    """One of the 8 role families: title rules first, then the requirement lines."""
    title = jd.role
    body = _job_body(jd)
    hits = []
    for code, pattern, why in FAMILY_TITLE_RULES:
        if pattern.search(title) and (code != "qa" or REGULATED_TERMS.search(f"{title}\n{body}")):
            hits.append((code, why))
    if hits:
        code, why = hits[0]
        reasons = [why + f" ('{title}')"]
        reasons += [f"also matched {c} ({w}); the rule order prefers {code}"
                    for c, w in hits[1:] if c != code]
        confidence = 0.9 if len({c for c, _w in hits}) == 1 else 0.7
        method = "title rule"
    else:
        typical = [(c, t) for c, (_name, titles) in kit.families.items() for t in titles
                   if t and re.search(rf"(?<![a-z0-9]){re.escape(t.lower())}(?![a-z0-9])",
                                      title.lower())]
        if typical:
            code, matched = max(typical, key=lambda ct: len(ct[1]))
            reasons, confidence, method = [f"title contains the 9.1 typical title '{matched}'"], \
                0.8, "typical title"
        else:
            scores = {c: len(p.findall(body)) for c, p in FAMILY_BODY_TERMS.items()}
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1], FAMILY_CODES_ORDER[kv[0]]))
            code, top = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else 0
            if top == 0:
                code, confidence, method = "swe", 0.3, "default"
                reasons = ["no title rule or family vocabulary matched; defaulted to swe"]
            else:
                confidence = round(min(0.75, 0.4 + 0.35 * (top - runner_up) / top), 2)
                method = "requirement lines"
                reasons = [f"title matched no rule; the requirement lines use {code} vocabulary "
                           f"most ({top} hits vs {ranked[1][0]} {runner_up})"]
    name = kit.families.get(code, (code, ()))[0]
    return FamilyDecision(code, name, confidence, reasons, method, job_traits(jd, code))


FAMILY_CODES_ORDER = {code: i for i, code in enumerate(resume_kit.FAMILY_CODES)}
HARD_REQUIREMENT_TYPES = ("years", "degree", "clearance", "location", "work_authorization",
                          "start_date")


def _term_phrases(term: CoverageTerm, text: str) -> list[str]:
    """The words of a JD line that asked for a term."""
    found: list[str] = []
    for key in (term.members or (term.key,)):
        kind, name = key.split(":", 1)
        if kind == "concept":
            concept = CONCEPT_INDEX[name]
            found += [m.group(0) for m in concept.jd.finditer(text)
                      if not _overlaps(concept.jd_exclude, text, m.start(), m.end())]
        else:
            found += [m.text for m in SKILL_FINDER.find(text) if m.key == name]
    return list(dict.fromkeys(found))


def phrase_terms(text: str) -> set[str]:
    """Coverage term keys a short phrase names ("triage" -> concept:debugging)."""
    keys = {f"concept:{c.key}" for c in _jd_concepts(text)}
    keys |= {f"skill:{m.key}" for m in SKILL_FINDER.find(text)}
    return keys


def term_synonyms(term: CoverageTerm, analysis: Analysis) -> list[str]:
    """Human-readable synonyms of a term, plus the 9.6 synonym-map words that name it."""
    keys = set(term.members or (term.key,))
    words: list[str] = []
    for key in keys:
        kind, name = key.split(":", 1)
        words += list(CONCEPT_INDEX[name].synonyms) if kind == "concept" else \
            [SKILL_INDEX[name].display]
    for row in (analysis.kit.synonyms if analysis.kit else []):
        words += [w for w in row.words if phrase_terms(w) & keys]
    return list(dict.fromkeys(words))


def build_job_document(analysis: Analysis) -> dict[str, Any]:
    """<base>_job.json: the parsed job, its role family, typed requirements and hard flags."""
    jd, model = analysis.jd, analysis.coverage
    requirements = []
    for req in (model.requirements if model else []):
        synonyms: list[str] = []
        for term in req.terms:
            synonyms += term_synonyms(term, analysis)
        requirements.append({
            "id": req.id, "text": req.text, "type": req.type, "weight": req.weight,
            "jd_line": req.jd_line, "mode": req.mode,
            "terms": [t.display for t in req.terms],
            "phrases": list(dict.fromkeys(p for t in req.terms
                                          for p in _term_phrases(t, req.text))),
            "synonyms": list(dict.fromkeys(synonyms))})
    hard: dict[str, Any] = {}
    for index, item in enumerate(model.hard if model else [], 1):
        requirements.append({"id": f"H{index:02d}", "text": item.text, "type": "hard",
                             "weight": 0, "jd_line": item.jd_line, "kind": item.type,
                             "status": item.status})
        hard.setdefault(item.type, {"status": item.status, "detail": item.detail,
                                    "jd_line": item.jd_line, "master_lines": item.lines,
                                    "affects_fit": item.affects_fit})
    for kind in HARD_REQUIREMENT_TYPES:
        hard.setdefault(kind, {"status": "not_stated", "detail": "The posting does not ask.",
                               "jd_line": None, "master_lines": [], "affects_fit": False})
    return {"title": jd.role, "company": jd.company,
            "location": _job_location_text(jd) or None,
            "region": model.region if model else job_region(jd),
            "source_job_description": jd.display_path,
            "family": analysis.family.as_dict() if analysis.family else None,
            "requirements": requirements, "hard_requirements": hard,
            "work_authorization_note": model.work_authorization if model else None}


# ---------------------------------------------------------------------------
# Resume content selection
# ---------------------------------------------------------------------------
EMPH_PATTERN = re.compile(r"\\emph(?=\{)")
# Practices and concepts: valid requirements, but never appended to a tools list.
PRACTICE_KEYS = frozenset({
    "incident", "troubleshooting", "documentation", "caching", "rate_limiting", "idempotency",
    "code_review", "agile", "on_call", "release_mgmt", "statistics", "linear_algebra", "dsa",
    "sentiment", "prompt_engineering", "model_registry", "kalman", "sensor_fusion", "gmp",
    "qa_practice", "unit_testing", "integration_testing", "regression_testing", "slo"})
HEADER_LEXICAL_SECTIONS = frozenset({"awards", "research", "leadership"})
TECH_LIST_SECTIONS = frozenset({"projects", "research"})
METRIC_PATTERN = re.compile(r"\d|\$|%")


def _positive_text(master: MasterProfile) -> str:
    return "\n".join(_line_text(l).lower() for l in master.lines
                     if not l.meta and l.tag in POSITIVE_TAGS)


def _literal_in(text_lower: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text_lower) is not None


def _requirement_hit_sets(analysis: Analysis) -> list[tuple[Requirement, set[str]]]:
    result = []
    for req in analysis.requirements:
        if req.status != STATUS_MATCHED or not (req.key or req.members):
            continue
        keys = {req.key, *SKILL_INDEX[req.key].members} if req.key else set(req.members)
        result.append((req, keys))
    return result


def _jd_stems(jd: JobDescription) -> set[str]:
    stems: set[str] = set()
    for line in jd.lines:
        if not line.is_heading and line.section_type != "info":
            stems |= content_stems(line.text)
    return stems


def _score_candidate(cand: Candidate, hits: list[tuple[Requirement, set[str]]],
                     jd_stems: set[str]) -> None:
    matched = [req for req, keys in hits if keys & set(cand.skills)]
    cand.req_hits = tuple(sorted({req.name for req in matched}))
    cand.req_weight = sum(req.weight for req in {id(r): r for r in matched}.values())
    cand.metric = bool(METRIC_PATTERN.search(cand.plain))
    cand.overlap = min(len(content_stems(cand.plain) & jd_stems), RELEVANCE_OVERLAP_CAP)


def _filter_tech_list(entry: TemplateEntry, master: MasterProfile, scope: EvidenceScope,
                      removed: list[dict[str, Any]], warnings: list[str],
                      section_title: str) -> str:
    """Drop unsupported items from a project's \\emph{...} stack list."""
    arg = entry.args[0]
    match = EMPH_PATTERN.search(arg)
    if not match:
        return entry.header_raw
    content, end = read_group(arg, match.end())
    items = _split_items(content)
    kept: list[str] = []
    for item in items:
        plain = latex_to_plain(item)
        keys = SKILL_FINDER.keys(plain)
        reason = ""
        if keys:
            for key in keys:
                display = SKILL_INDEX[key].display
                if key in master.global_no:
                    reason = f"'{display}' is marked [NO]"
                    break
                if key not in scope.skills:
                    reason = f"no positive evidence for '{display}' in this entry"
                    break
                if any(o.entry_id == scope.entry_id for o in master.scoped_no.get(key, [])):
                    warnings.append(
                        f"Kept '{display}' in the {latex_to_plain(entry.args[0])[:30]!r} stack "
                        f"line: the entry has positive evidence for it, while its [NO] marker "
                        f"blocks it from bullets")
        else:
            stems = content_stems(plain)
            if stems and len(stems & scope.stems) / len(stems) < LEXICAL_SUPPORT_THRESHOLD:
                reason = "wording not supported by this entry"
        if reason:
            removed.append({"section": section_title, "item": plain, "reason": reason,
                            "location": "entry stack line"})
        else:
            kept.append(item)
    if len(kept) == len(items):
        return entry.header_raw
    new_arg = arg[:match.end()] + "{" + ", ".join(kept) + "}" + arg[end:]
    args = [new_arg, *entry.args[1:]]
    return "\\" + entry.macro + "\n        " + "".join("{" + a + "}" for a in args)


def _verify_header(entry: TemplateEntry, section_kind: str, master: MasterProfile,
                   entry_id: str) -> ClaimCheck:
    scope = build_scope(master, entry_id, include_headings=True)
    args = list(entry.args)
    if section_kind in TECH_LIST_SECTIONS:
        match = EMPH_PATTERN.search(args[0])
        if match:
            _content, end = read_group(args[0], match.end())
            args[0] = args[0][:match.start()] + args[0][end:]
    plain = " | ".join(latex_to_plain(a) for a in args if latex_to_plain(a))
    check = verify_claim(plain, master, scope)
    if section_kind not in HEADER_LEXICAL_SECTIONS:
        pairs = [(c, r) for c, r in zip(check.codes, check.reasons) if c != "lexical"]
        check.codes = [c for c, _r in pairs]
        check.reasons = [r for _c, r in pairs]
        check.ok = not pairs
    return check


def _section_candidate_limit(kind: str, available: int) -> int:
    if kind in ("experience", "projects"):
        return min(CANDIDATE_BULLETS_PER_ENTRY, available)
    if kind in ("research", "leadership"):
        return min(CANDIDATE_BULLETS_MINOR, available)
    return available


def build_resume_plan(template: ParsedTemplate, master: MasterProfile, analysis: Analysis,
                      measure: Callable[[Sequence[tuple[str, str, str]]], dict[str, int]]
                      | None = None) -> ResumePlan:
    """Plan the resume: from the resume kit, or (legacy, --no-kit) from template bullets."""
    if analysis.kit is not None:
        return build_kit_plan(template, master, analysis, measure)
    hits = _requirement_hit_sets(analysis)
    jd_stems = _jd_stems(analysis.jd)
    plan = ResumePlan(template=template, entries={}, section_order=[], entry_order={},
                      summary_text=None, kv_items={})
    positive_lower = _positive_text(master)
    for si, section in enumerate(template.sections):
        kind = template_section_kind(section.title)
        plan.section_order.append(si)
        if section.kind == "paragraph":
            plan.summary_text = _verified_summary(section, master, plan)
            continue
        if section.kind == "keyvalue":
            _plan_kv_section(si, section, kind, master, plan, hits, positive_lower)
            continue
        for ei, entry in enumerate(section.entries):
            plan.entries[(si, ei)] = _plan_entry(si, ei, section, kind, entry, master, plan,
                                                 hits, jd_stems)
        order = [ei for ei in range(len(section.entries)) if plan.entries[(si, ei)].keep]
        if kind == "projects":
            order.sort(key=lambda ei: (-plan.entries[(si, ei)].relevance, ei))
        plan.entry_order[si] = order
    if analysis.coverage is not None and analysis.coverage.requirements:
        select_by_coverage(plan, analysis)
        order_skills(plan, analysis)
        generate_summary(plan, analysis)
    return plan


def _plan_entry(si: int, ei: int, section: TemplateSection, kind: str, entry: TemplateEntry,
                master: MasterProfile, plan: ResumePlan,
                hits: list[tuple[Requirement, set[str]]], jd_stems: set[str]) -> EntryPlan:
    optional = kind in ("projects", "research", "leadership")
    entry_id, why = map_template_entry(entry, kind, master)
    header_plain = " | ".join(latex_to_plain(a) for a in entry.args if latex_to_plain(a))
    if entry_id is None:
        plan.warnings.append(f"Dropped template entry {header_plain[:60]!r}: {why}")
        plan.template_items_removed.append({"section": section.title, "item": header_plain,
                                            "reason": why, "location": "entry"})
        return EntryPlan(si, ei, None, [], [], 0, optional, keep=False,
                         rejected=[{"bullet": b.plain, "reasons": [why]} for b in entry.bullets])
    header_check = _verify_header(entry, kind, master, entry_id)
    if not header_check.ok:
        plan.warnings.append(f"Dropped template entry {header_plain[:60]!r}: header not "
                             f"supported ({'; '.join(header_check.reasons)})")
        plan.template_items_removed.append({"section": section.title, "item": header_plain,
                                            "reason": "; ".join(header_check.reasons),
                                            "location": "entry header"})
        return EntryPlan(si, ei, entry_id, [], [], 0, optional, keep=False)
    scope = build_scope(master, entry_id)
    header_raw = entry.header_raw
    if kind in TECH_LIST_SECTIONS:
        header_raw = _filter_tech_list(entry, master, scope, plan.template_items_removed,
                                       plan.warnings, section.title)
    candidates: list[Candidate] = []
    rejected: list[dict[str, Any]] = []
    for bullet in entry.bullets:
        check = verify_claim(bullet.plain, master, scope)
        if not check.ok:
            rejected.append({"bullet": bullet.plain, "reasons": check.reasons,
                             "support": check.support, "index": bullet.index})
            continue
        cand = Candidate(section_index=si, entry_index=ei, bullet_index=bullet.index,
                         raw=bullet.raw, plain=bullet.plain, skills=tuple(check.skills),
                         support=check.support, source_lines=check.source_lines)
        if DEPENDENT_BULLET.match(bullet.plain) and kind not in ("projects", "research"):
            parent = next((c for c in candidates if c.bullet_index == bullet.index - 1), None)
            if parent is None:
                rejected.append({"bullet": bullet.plain, "support": check.support, "reasons": [
                    "depends on the bullet before it, which was not verified"],
                    "index": bullet.index})
                continue
            cand.parent_index = parent.bullet_index
        _score_candidate(cand, hits, jd_stems)
        candidates.append(cand)
    ranked = sorted(candidates, key=lambda c: (-c.relevance, c.bullet_index))
    selected = order_with_parents(ranked[:_section_candidate_limit(kind, len(ranked))],
                                  candidates)
    if kind == "experience":
        relevant = any(c.req_weight > 0 for c in selected)
        floor = min(FLOOR_RELEVANT_EXPERIENCE if relevant else FLOOR_OTHER_EXPERIENCE,
                    len(selected))
    elif optional:
        floor = min(FLOOR_KEPT_PROJECT, len(selected))
    else:
        floor = len(selected)
    keep = True
    if optional and entry.has_item_list and not selected:
        keep = False
        plan.warnings.append(f"Dropped {header_plain[:50]!r}: none of its bullets could be "
                             f"verified against the master profile")
    return EntryPlan(si, ei, entry_id, candidates, selected, floor, optional, keep=keep,
                     rejected=rejected, header_raw=header_raw)


# ---------------------------------------------------------------------------
# Coverage-driven generation: bullets, skills and summary
# ---------------------------------------------------------------------------
def order_with_parents(chosen: Sequence[Candidate], pool: Sequence[Candidate]
                       ) -> list[Candidate]:
    """Keep each dependent bullet directly after its parent; drop orphans."""
    chosen_ids = {c.bullet_index for c in chosen}
    by_index = {c.bullet_index: c for c in pool}

    def rooted(cand: Candidate) -> bool:
        while cand.parent_index is not None:
            if cand.parent_index not in chosen_ids:
                return False
            cand = by_index[cand.parent_index]
        return True

    kept = [c for c in chosen if rooted(c)]
    children: dict[int, list[Candidate]] = {}
    for cand in kept:
        if cand.parent_index is not None:
            children.setdefault(cand.parent_index, []).append(cand)
    ordered: list[Candidate] = []

    def place(cand: Candidate) -> None:
        ordered.append(cand)
        for child in sorted(children.get(cand.bullet_index, []), key=lambda c: c.bullet_index):
            place(child)

    for cand in kept:
        if cand.parent_index is None:
            place(cand)
    return ordered


def _entry_name(plan: ResumePlan, si: int, ei: int) -> str:
    plain = latex_to_plain(plan.template.sections[si].entries[ei].args[0])
    return re.sub(r"\s*\(.*?\)\s*$", "", plain.partition(" | ")[0]).strip()


def _ancestors(cand: Candidate, by_index: dict[int, Candidate]) -> list[Candidate]:
    chain = []
    while cand.parent_index is not None:
        cand = by_index[cand.parent_index]
        chain.append(cand)
    return chain[::-1]


def select_by_coverage(plan: ResumePlan, analysis: Analysis) -> None:
    """Choose bullets by greedy weighted set cover over the job's requirements.

    The objective is the scorer's own: requirement weight times evidence credit,
    with the same coherence rule (one entry proving several terms of a requirement
    beats pieces from unrelated entries). Each bullet proves terms at its entry's
    strength (job 3, project 2 or 1). Order of work:

    1. Greedy: add the bullet (with any bullets it depends on) that raises
       coverage most per bullet, while it raises coverage and the budget lasts.
    2. The best proof of every core term, every job that proves the job's focus
       (such as test automation), and every project that covers two or more
       high-weight requirements, are included.
    3. Floors: each included job is topped up to 3 bullets with mapped bullets.
       A job without enough mapped bullets is dropped unless it is the only proof
       of something or proves the focus; only then may unmapped bullets fill its
       floor, continuations of its kept bullets first.
    4. Depth: more mapped bullets, by standalone weight, up to the budget.

    A bullet that maps to no requirement is never chosen while a coverable
    requirement is still uncovered (``plan.selection_check`` records this).
    """
    model = analysis.coverage
    master = analysis.master
    weights = model.term_weights()
    projects = _project_strengths(master)
    kinds = {si: template_section_kind(sec.title) for si, sec in enumerate(plan.template.sections)}
    apply_rewrites(plan, analysis, kinds)
    term_keys = list(weights) + [m for r in model.requirements for t in r.terms for m in t.members]
    req_terms = {r.id: {t.key for t in r.terms} | {m for t in r.terms for m in t.members}
                 for r in model.requirements}
    high_reqs = {r.id for r in model.requirements if r.weight >= WEIGHT_REQUIRED}
    generic_terms = {t.key for r in model.requirements for t in r.terms if term_weight(t) < 1}
    pool: dict[tuple[int, int], list[Candidate]] = {}
    names: dict[tuple[int, int], str] = {}
    for (si, ei), ep in sorted(plan.entries.items()):
        if not ep.keep or kinds[si] not in ("experience", "projects", "research", "leadership"):
            continue
        names[(si, ei)] = _entry_name(plan, si, ei)
        strength = Fraction(_resume_strength(kinds[si], names[(si, ei)], projects), 3)
        for cand in ep.candidates:
            cand.proves = {k: strength for k in dict.fromkeys(term_keys)
                           if _text_proves(k, cand.plain)}
            specific = {k for k in cand.proves if k not in generic_terms}
            cand.requirement_ids = tuple(rid for rid, keys in req_terms.items()
                                         if keys & specific)
        pool[(si, ei)] = list(ep.candidates)
    seed: dict[str, dict[str, Proof]] = {}
    for (si, li), items in plan.kv_items.items():
        if kinds[si] == "skills":
            text = ", ".join(latex_to_plain(item) for item in items)
            for key in term_keys:
                if _text_proves(key, text):
                    seed.setdefault(key, {})["skills"] = Proof(SKILLS_ONLY_CREDIT, 0, text,
                                                               "skills", "skills")
    picked: dict[tuple[int, int], list[Candidate]] = {key: [] for key in pool}

    def table_for(chosen: dict[tuple[int, int], list[Candidate]]) -> dict[str, dict[str, Proof]]:
        table = {term: dict(entries) for term, entries in seed.items()}
        for key, cands in chosen.items():
            for cand in cands:
                for term, credit in cand.proves.items():
                    old = table.setdefault(term, {}).get(names[key])
                    if old is None or credit > old.credit:
                        table[term][names[key]] = Proof(credit, int(credit * 3), cand.plain,
                                                        names[key], "bullet")
        for req in model.requirements:
            for term in req.terms:
                if term.kind == "any_of":
                    table[term.key] = _merge_members(term, table)
        return table

    def coverage(table: dict[str, dict[str, Proof]]) -> Fraction:
        return sum((r.weight * _requirement_credit(r, table) for r in model.requirements),
                   Fraction(0))

    current = coverage(table_for(picked))

    def gain(key: tuple[int, int], unit: Sequence[Candidate]) -> Fraction:
        trial = {k: list(v) for k, v in picked.items()}
        trial[key] = trial[key] + list(unit)
        return coverage(table_for(trial)) - current

    def standalone(unit: Sequence[Candidate]) -> Fraction:
        return sum((weights.get(k, Fraction(0)) * v for c in unit for k, v in c.proves.items()),
                   Fraction(0))

    def units(keys: Sequence[tuple[int, int]], mapped_only: bool = True,
              room: int | None = None):
        for key in keys:
            taken = {c.bullet_index for c in picked[key]}
            by_index = {c.bullet_index: c for c in pool[key]}
            for cand in pool[key]:
                if cand.bullet_index in taken or (mapped_only and not cand.requirement_ids):
                    continue
                chain = [a for a in _ancestors(cand, by_index) if a.bullet_index not in taken]
                if room is None or len(chain) < room:
                    yield key, chain + [cand]

    def best_unit(keys, score, mapped_only: bool = True, room: int | None = None):
        best = None
        for key, unit in units(keys, mapped_only, room):
            rank = (score(key, unit) / len(unit), standalone(unit), max(c.relevance for c in unit),
                    -len(unit), -key[0], -key[1], -unit[-1].bullet_index)
            if best is None or rank > best[0]:
                best = (rank, key, unit)
        return best

    def take(key: tuple[int, int], unit: Sequence[Candidate], value: float) -> None:
        nonlocal current
        for cand in unit:
            cand.value = value
            picked[key].append(cand)
        current = coverage(table_for(picked))

    count = 0
    while count < COVERAGE_BULLET_BUDGET:  # 1. greedy coverage
        best = best_unit(list(pool), gain)
        if best is None or best[0][0] <= 0:
            break
        take(best[1], best[2], float(best[0][0]))
        count += len(best[2])
    shown = table_for(picked)
    for req in model.requirements:  # 2a. the best proof of every core term is shown
        if req.type != "core":
            continue
        for term in req.terms:
            if term_weight(term) < 1 or shown.get(term.key) or term.key not in weights:
                continue
            best = best_unit(list(pool), lambda _k, u, t=term.key: max(
                (c.proves.get(t, Fraction(0)) for c in u), default=Fraction(0)))
            if best is not None and best[0][0] > 0:
                take(best[1], best[2], float(max(gain(best[1], best[2]), Fraction(1, 100))))
                shown = table_for(picked)
    focus_key = model.focus[0] if model.focus else None
    focus_jobs: set[tuple[int, int]] = set()
    for key in pool:  # 2b. every job proving the job's focus shows it (years of it count)
        if kinds[key[0]] != "experience" or focus_key is None:
            continue
        if any(focus_key in c.proves for c in picked[key]):
            focus_jobs.add(key)
            continue
        best = best_unit([key], lambda _k, u: max(
            (c.proves.get(focus_key, Fraction(0)) for c in u), default=Fraction(0)) *
            (1 + standalone(u)))
        if best is not None and best[0][0] > 0:
            take(key, best[2], float(max(gain(key, best[2]), Fraction(1, 100))))
            focus_jobs.add(key)
    for key in pool:  # 2c. a project covering 2+ high-weight requirements shows its best
        if kinds[key[0]] != "projects":
            continue
        covered = {rid for c in pool[key] for rid in c.requirement_ids if rid in high_reqs}
        if len(covered) < 2:
            continue
        if not picked[key]:
            best = best_unit([key], lambda k, u: Fraction(len(
                {r for c in u for r in c.requirement_ids if r in high_reqs})))
            if best is not None:
                take(key, best[2], float(max(gain(key, best[2]), Fraction(1, 100))))
        mapped = sum(1 for c in pool[key] if c.requirement_ids)
        while len(picked[key]) < min(STRONG_PROJECT_BULLETS, mapped):
            best = best_unit([key], lambda _k, u: Fraction(len(
                {r for c in u for r in c.requirement_ids})))
            if best is None:
                break
            take(key, best[2], float(standalone(best[2])) / 100)  # above floor and depth fill

    def unique(key: tuple[int, int]) -> bool:
        without = {k: (v if k != key else []) for k, v in picked.items()}
        return coverage(table_for(without)) < current

    for key in list(pool):  # 3. floors: 3 bullets per included job, mapped bullets first
        if not picked[key] or kinds[key[0]] != "experience":
            continue
        floor = min(EXPERIENCE_MIN_BULLETS, len(pool[key]))
        while len(picked[key]) < floor:  # never past the floor with a chain
            best = best_unit([key], lambda _k, u: standalone(u),
                             room=floor - len(picked[key]))
            if best is None:
                break
            take(key, best[2], float(standalone(best[2])) / 1000)
        if len(picked[key]) < floor:
            if not unique(key) and key not in focus_jobs:
                for cand in picked[key]:
                    cand.value = None
                picked[key] = []
                current = coverage(table_for(picked))
                continue
            while len(picked[key]) < floor:  # needed for its proof: fill the floor,
                taken = {c.bullet_index for c in picked[key]}  # continuing a kept bullet first
                best = best_unit([key], lambda _k, u, t=taken: Fraction(
                    int(u[0].parent_index in t)), mapped_only=False,
                    room=floor - len(picked[key]))
                if best is None:
                    break
                take(key, best[2], 0.0)
    count = sum(len(v) for v in picked.values())
    while count < COVERAGE_BULLET_BUDGET:  # 4. depth: more mapped proof, fitting decides
        best = best_unit([k for k in pool if picked[k] or kinds[k[0]] != "experience"],
                         lambda _k, u: standalone(u))
        if best is None or best[0][0] <= 0:
            break
        if kinds[best[1][0]] == "experience" and not picked[best[1]]:
            break
        take(best[1], best[2], float(best[0][0]) / 1000)
        count += len(best[2])
    every = {key: list(cands) for key, cands in pool.items()}
    reachable, final = table_for(every), table_for(picked)
    plan.selection_check = {
        "unmapped_bullets": [c.plain for cands in picked.values() for c in cands
                             if not c.requirement_ids],
        "uncovered_coverable_requirements": [
            r.id for r in model.requirements
            if _requirement_credit(r, model.profile) > 0 and
            _requirement_credit(r, reachable) > 0 and _requirement_credit(r, final) == 0],
    }
    plan.selection_check["violation"] = bool(
        plan.selection_check["unmapped_bullets"] and
        plan.selection_check["uncovered_coverable_requirements"])
    for key, ep in plan.entries.items():
        if key not in pool:
            continue
        si, ei = key
        if not picked[key]:
            ep.keep = False
            ep.selected = []
            plan.template_items_removed.append({
                "section": plan.template.sections[si].title, "item": names[key],
                "reason": "no bullet proves a requirement of this job", "location": "entry"})
            continue
        ordered = sorted(picked[key], key=lambda c: (-(c.value or 0), c.bullet_index))
        ep.selected = order_with_parents(ordered, pool[key])
        ep.coverage_value = sum(c.value or 0 for c in ep.selected)
        if kinds[si] == "experience":
            ep.floor = min(EXPERIENCE_MIN_BULLETS, len(ep.selected))
            ep.optional = True  # a whole role may go before a strong bullet elsewhere
        else:
            ep.floor = min(FLOOR_KEPT_PROJECT, len(ep.selected))
    for si in list(plan.entry_order):
        order = [ei for ei in range(len(plan.template.sections[si].entries))
                 if plan.entries[(si, ei)].keep]
        if kinds[si] in ("projects", "research", "leadership"):
            order.sort(key=lambda ei: (-plan.entries[(si, ei)].worth(), ei))
        plan.entry_order[si] = order


# ---------------------------------------------------------------------------
# Kit-driven plan and selection: bullets come from the 9.5 bullet bank
# ---------------------------------------------------------------------------
KIT_SECTIONS = ("experience", "projects", "research", "leadership")
# A role or project is relevant through what it did, not the language it was written in.
GENERAL_LANGUAGES = frozenset({"python", "java", "javascript", "typescript", "bash", "cpp",
                               "go", "rust", "scala", "kotlin", "csharp", "ruby", "php"})
PROJECT_LIMIT = 3                  # 9.7: the top 3 projects by requirement coverage
TOP_PROJECTS = 2                   # ... the top 2 with 3 or 4 bullets
TOP_PROJECT_BULLETS = (3, 4)
PROJECT_FLOORS = (3, 2, 1)         # bullets kept per project by rank when the page is full
OTHER_PROJECT_BULLETS = 2
KIT_BULLET_BUDGET = 30             # bullets chosen before fitting trims to the page budget
DEPTH_WEIGHT = Fraction(1, 4)      # relevance a bullet keeps when its terms are covered
KEYWORD_WEIGHT = Fraction(1, 2)    # per asked term only this bullet's words show (ATS view)
PROJECT_DEPTH_WEIGHT = Fraction(1, 4)   # relevance of a project's best 3 bullets
LEADERSHIP_BULLETS = 2             # per leadership entry, included when space allows
RESEARCH_BULLETS = 2
# What a kit theme says a bullet demonstrates, as coverage terms. Themes that name
# no requirement vocabulary ("result", "context") prove nothing on their own.
THEME_TERMS: dict[str, tuple[str, ...]] = {
    "ci_cd": ("concept:ci_pipelines", "skill:cicd"), "reliability": ("concept:reliability",),
    "automation": ("concept:automation",),
    "test stability": ("concept:reliability", "concept:test_automation"),
    "monitoring": ("concept:monitoring",), "anomaly": ("concept:risk_flagging",),
    "collaboration": ("concept:collaboration",), "teamwork": ("concept:collaboration",),
    "stakeholder": ("concept:collaboration",), "communication": ("concept:collaboration",),
    "test_infra": ("concept:test_automation", "concept:ci_pipelines"),
    "deployment": ("concept:deployment",), "rollout": ("concept:deployment",),
    "safe_rollout": ("concept:deployment", "concept:ml_tooling"),
    "integration": ("concept:integration",), "integrations": ("concept:integration",),
    "multiple systems": ("concept:integration",), "apis": ("concept:integration",),
    "connectors": ("concept:integration", "concept:data_pipelines"),
    "access_control": ("concept:sensitive_data",), "security": ("concept:sensitive_data",),
    "compliance": ("concept:sensitive_data",), "audit": ("concept:sensitive_data",),
    "traceability": ("concept:sensitive_data",), "gmp": ("concept:sensitive_data",),
    "ownership": ("concept:end_to_end",), "incident": ("concept:on_call", "concept:debugging"),
    "safe_change": ("concept:reliability", "concept:sensitive_data"),
    "backend_api": ("concept:api_integration",),
    "on_call": ("concept:on_call",), "downtime": ("concept:reliability",),
    "banking": ("concept:financial_services",), "finance": ("concept:financial_services",),
    "triage": ("concept:debugging",),
    "debugging others' code": ("concept:code_outside", "concept:debugging"),
    "customer": ("concept:customers",), "documentation": ("concept:documentation",),
    "best practices": ("concept:documentation",), "leadership": ("concept:leadership",),
    "mentoring": ("concept:leadership",), "teaching": ("concept:leadership",),
    "data_pipeline": ("concept:data_pipelines",),
    "incremental_processing": ("concept:data_pipelines",),
    "data_freshness": ("concept:data_quality",), "data_quality": ("concept:data_quality",),
    "quality": ("concept:data_quality",), "testing": ("concept:software_quality",),
    "code_quality": ("concept:software_quality",), "verification": ("concept:software_quality",),
    "risk": ("concept:risk_flagging",), "root_cause": ("concept:debugging",),
    "analytics": ("concept:data_analysis",), "insights": ("concept:data_analysis",),
    "data_analysis": ("concept:data_analysis",), "measurement": ("concept:data_analysis",),
    "dashboards": ("skill:data_viz",), "reporting": ("skill:data_viz",),
    "kpis": ("concept:metrics_tracking",), "user_metrics": ("concept:metrics_tracking",),
    "computer_vision": ("concept:ml_modeling", "skill:computer_vision"),
    "ml": ("concept:ml_modeling",), "ml_training": ("concept:ml_modeling",),
    "generalization": ("concept:ml_modeling",), "overfitting": ("concept:ml_modeling",),
    "hyperparameters": ("concept:ml_modeling",), "evaluation": ("concept:model_evaluation",),
    "precision": ("concept:model_evaluation",),
    "test_framework": ("concept:test_automation", "concept:model_evaluation"),
    "production": ("concept:production",), "recommendation": ("concept:recommendation",),
    "llm_safety": ("concept:llm_grounding", "concept:llm_apps"),
    "grounding": ("concept:llm_grounding",), "llm": ("concept:llm_apps",),
    "agents_adjacent": ("concept:ai_agents",), "cost": ("concept:reliability",),
    "tradeoffs": ("concept:reliability",), "full_stack": ("concept:full_stack",),
    "policy_learning": ("concept:policy_learning",), "reward_design": ("concept:reward_design",),
    "mlops": ("concept:ml_tooling",),
    "experiment_tracking": ("concept:experimentation", "concept:ml_tooling"),
    "ai_tooling": ("concept:ai_tooling",), "scheduling": ("concept:automation",),
    "structured_extraction": ("concept:structured_extraction",),
    "structured_output": ("concept:structured_extraction",), "rag": ("skill:rag",),
    "embeddings": ("concept:embeddings",), "nlp": ("skill:nlp",),
}
TRAIT_WORDS = {"hardware": "hardware", "robotics": "robotics", "mechanical": "mechanical",
               "frontend": "frontend", "healthcare": "healthcare", "health": "healthcare",
               "quality": "quality", "research": "research", "analyst": "analyst",
               "finance": "finance", "quant": "quant", "ml": "ml", "leadership": "leadership"}


@dataclass(frozen=True)
class KitRestriction:
    bullet_ids: frozenset[str]
    families: frozenset[str]
    traits: frozenset[str]
    grants: bool                    # "or when the job asks for X": the rule alone decides
    subject: str
    line: int


def kit_restrictions(kit: resume_kit.ResumeKit) -> tuple[list[KitRestriction], list[str]]:
    """9.7 rules of the form '<subject>: only for <roles> roles' as eligibility checks."""
    found: list[KitRestriction] = []
    warnings: list[str] = []
    for rule in kit.rules:
        match = re.search(r"\b(?:only )?for (.+?) roles?\b", rule.text, re.IGNORECASE)
        if not match:
            continue
        families = set(re.findall(r"`([a-z]+)`", match.group(1)))
        traits = set()
        words = re.findall(r"[A-Za-z]+", re.sub(r"`[a-z]+`", " ", match.group(1)))
        asks = re.search(r"when the job asks for ([A-Za-z ]+)", rule.text, re.IGNORECASE)
        words += re.findall(r"[A-Za-z]+", asks.group(1)) if asks else []
        for word in words:
            if word.lower() in TRAIT_WORDS:
                traits.add(TRAIT_WORDS[word.lower()])
            elif word.lower() not in ("and", "or", "the", "roles", "role"):
                warnings.append(f"9.7 rule '{rule.subject}' (line {rule.line}): unknown role "
                                f"type '{word}'")
        ids = set(rule.ids)
        if not ids:
            subject = rule.subject.lower()
            for group in kit.groups:
                name = group.name.lower()
                if subject.startswith(name) or name.startswith(subject.split()[0]):
                    ids |= set(group.bullets)
        if not ids:
            warnings.append(f"9.7 rule '{rule.subject}' (line {rule.line}) matches no bullets")
            continue
        found.append(KitRestriction(frozenset(ids), frozenset(families), frozenset(traits),
                                    bool(asks), rule.subject, rule.line))
    return found, warnings


def _condition_met(condition: str, traits: set[str]) -> bool:
    words = {TRAIT_WORDS[w] for w in re.findall(r"[a-z]+", condition.lower()) if w in TRAIT_WORDS}
    return bool(words & traits)


def kit_eligibility(kit: resume_kit.ResumeKit, family: FamilyDecision,
                    restrictions: Sequence[KitRestriction]) -> dict[str, str]:
    """Bullet ID -> why it is left out for this job (eligible bullets are absent).

    A bullet must list the job's family (or `all`), meet its family qualifier ("ops
    (hardware roles)"), and pass every 9.7 rule that names it. A rule with "or when the
    job asks for X" decides on its own. Of two wordings of one fact (S01, S01b) the one
    written for fewer families, including this one, is used.
    """
    traits = set(family.traits)
    reasons: dict[str, str] = {}
    for bullet in kit.bullets.values():
        rules = [r for r in restrictions if bullet.id in r.bullet_ids]
        granting = [r for r in rules if r.grants]
        if granting:
            rule = granting[0]
            if not (family.code in rule.families or traits & rule.traits):
                reasons[bullet.id] = (f"9.7 '{rule.subject}' (line {rule.line}): not a "
                                      f"{'/'.join(sorted(rule.families))} role and the job "
                                      f"does not ask for {'/'.join(sorted(rule.traits))}")
            continue
        fits = [ref for ref in bullet.families if ref.code in (resume_kit.ALL_FAMILIES,
                                                               family.code)]
        if not fits:
            reasons[bullet.id] = (f"families {', '.join(r.code for r in bullet.families)} do "
                                  f"not include {family.code}")
            continue
        if all(ref.condition and not _condition_met(ref.condition, traits) for ref in fits):
            reasons[bullet.id] = f"only for {fits[0].condition} roles"
            continue
        for rule in rules:
            if not (family.code in rule.families or traits & rule.traits):
                reasons[bullet.id] = (f"9.7 '{rule.subject}' (line {rule.line}): only for "
                                      f"{'/'.join(sorted(rule.traits | rule.families))} roles")
                break
    for bullet in kit.bullets.values():
        other = kit.bullets.get(bullet.alternate_of or "")
        if other is None or bullet.id in reasons or other.id in reasons:
            continue
        def specificity(b: resume_kit.KitBullet) -> int:
            codes = [r.code for r in b.families]
            return 99 if resume_kit.ALL_FAMILIES in codes else len(codes)
        keep, drop = (bullet, other) if specificity(bullet) < specificity(other) else \
            (other, bullet)
        reasons[drop.id] = f"alternate wording of {keep.id}, which suits {family.code} better"
    return reasons


def kit_hooks(kit: resume_kit.ResumeKit, audit: KitAudit) -> dict[str, frozenset[str]]:
    """Coverage terms each bullet demonstrates through its themes and 9.6 synonyms."""
    hooks: dict[str, set[str]] = {bid: set() for bid in kit.bullets}
    for bid, bullet in kit.bullets.items():
        for theme in bullet.themes:
            hooks[bid] |= set(THEME_TERMS.get(theme, ()))
    themed = {bid: frozenset(terms) for bid, terms in hooks.items()}
    for row, ids in zip(kit.synonyms, audit.synonym_ids):
        for word in row.words:
            for term in phrase_terms(word):  # to the row's bullets that show it, else all
                owners = [bid for bid in ids if term in themed[bid] or
                          _text_proves(term, kit.bullets[bid].plain)]
                for bid in owners or ids:
                    hooks[bid].add(term)
    return {bid: frozenset(terms) for bid, terms in hooks.items()}


HOOK_CREDIT = Fraction(1, 2)  # a theme or synonym the wording does not show is inferred


def hook_credit(term_key: str, text: str, hooks: frozenset[str]) -> Fraction:
    """1 when the words prove the term, HOOK_CREDIT when only kit themes or synonyms do."""
    if _text_proves(term_key, text):
        return Fraction(1)
    return HOOK_CREDIT if proves_with_hooks(term_key, text, hooks) else Fraction(0)


def proves_with_hooks(term_key: str, text: str, hooks: frozenset[str]) -> bool:
    """A bullet proves a term by its words (judged) or by its kit themes and synonyms."""
    if _text_proves(term_key, text):
        return True
    kind, key = term_key.split(":", 1)
    if kind == "any":
        return any(member in hooks for member in key.split("|"))
    if term_key in hooks:
        return True
    return kind == "skill" and any(f"skill:{m}" in hooks for m in SKILL_INDEX[key].members)


def kit_latex(text: str) -> str:
    """Kit Markdown to LaTeX: **bold** (tech) becomes \\textbf; all text is escaped."""
    out, last = [], 0
    for match in resume_kit.BOLD.finditer(text):
        out.append(latex_escape(text[last:match.start()]))
        out.append("\\textbf{" + latex_escape(match.group(1)) + "}")
        last = match.end()
    out.append(latex_escape(text[last:]))
    return "".join(out)


def _assign_kit_bullets(template: ParsedTemplate, master: MasterProfile,
                        kit: resume_kit.ResumeKit, audit: KitAudit
                        ) -> tuple[dict[tuple[int, int], list[resume_kit.KitBullet]], list[str]]:
    """Template entry for every kit bullet: its group's master entry, then wording."""
    by_entry: dict[str, list[tuple[int, int]]] = {}
    for si, section in enumerate(template.sections):
        kind = template_section_kind(section.title)
        if kind not in KIT_SECTIONS:
            continue
        for ei, entry in enumerate(section.entries):
            entry_id, _why = map_template_entry(entry, kind, master)
            if entry_id is not None:
                by_entry.setdefault(entry_id, []).append((si, ei))
    assigned: dict[tuple[int, int], list[resume_kit.KitBullet]] = {}
    warnings = []
    for group in kit.groups:
        targets = by_entry.get(audit.entries.get(group.key) or "", [])
        if not targets:
            warnings.append(f"Kit group '{group.header}' has no entry in the template; its "
                            f"bullets are not used")
            continue
        for bid in group.bullets:
            bullet = kit.bullets[bid]
            stems = content_stems(bullet.plain)

            def overlap(key: tuple[int, int]) -> int:
                entry = template.sections[key[0]].entries[key[1]]
                words = content_stems(latex_to_plain(entry.header_raw))
                for item in entry.bullets:
                    words |= content_stems(item.plain)
                return len(stems & words)
            parent = kit.bullets.get(bullet.keep_with or "")
            home = next((k for k, items in assigned.items() if parent in items), None)
            target = home if home in targets else max(targets, key=lambda k: (overlap(k),
                                                                               -targets.index(k)))
            assigned.setdefault(target, []).append(bullet)
    return assigned, warnings


def _entry_dates(entry: TemplateEntry) -> tuple[int, int]:
    """(end, start) as year*12+month from a header like 'Sep 2025 -- May 2026' (0 if none)."""
    text = latex_to_plain(" ".join(entry.args[1:2])) if len(entry.args) > 1 else ""
    found = [int(y) * 12 + MONTHS.get(m[:3].lower(), 0) for m, y in
             re.findall(r"([A-Za-z]{3,9})\.? (\d{4})", text)]
    if not found:
        years = [int(y) * 12 for y in re.findall(r"\b(\d{4})\b", text)]
        found = years
    if not found:
        return 0, 0
    return max(found), min(found)


def _plan_kit_entry(si: int, ei: int, section: TemplateSection, kind: str, entry: TemplateEntry,
                    master: MasterProfile, plan: ResumePlan,
                    bullets: Sequence[resume_kit.KitBullet], analysis: Analysis,
                    excluded: dict[str, str], hooks: dict[str, frozenset[str]],
                    hits: list[tuple[Requirement, set[str]]], jd_stems: set[str]) -> EntryPlan:
    """An entry whose candidates are its kit bullets (eligible ones), verified and scored."""
    optional = True
    header_plain = " | ".join(latex_to_plain(a) for a in entry.args if latex_to_plain(a))
    entry_id, why = map_template_entry(entry, kind, master)
    if entry_id is None or not bullets:
        reason = why or "no kit bullets for this entry"
        plan.template_items_removed.append({"section": section.title, "item": header_plain,
                                            "reason": reason, "location": "entry"})
        return EntryPlan(si, ei, entry_id, [], [], 0, optional, keep=False)
    header_check = _verify_header(entry, kind, master, entry_id)
    if not header_check.ok:
        plan.template_items_removed.append({"section": section.title, "item": header_plain,
                                            "reason": "; ".join(header_check.reasons),
                                            "location": "entry header"})
        return EntryPlan(si, ei, entry_id, [], [], 0, optional, keep=False)
    scope = build_scope(master, entry_id)
    header_raw = entry.header_raw
    if kind in TECH_LIST_SECTIONS:
        header_raw = _filter_tech_list(entry, master, scope, plan.template_items_removed,
                                       plan.warnings, section.title)
    audit = analysis.kit_audit
    candidates: list[Candidate] = []
    rejected: list[dict[str, Any]] = []
    for position, bullet in enumerate(bullets):
        reason = (f"audit: {audit.blocked[bullet.id][0]}" if bullet.id in audit.blocked else
                  excluded.get(bullet.id))
        if reason:
            rejected.append({"bullet": bullet.plain, "kit_id": bullet.id, "reasons": [reason],
                             "stage": "eligibility", "index": position})
            continue
        check = verify_claim(bullet.plain, master, scope)
        cand = Candidate(section_index=si, entry_index=ei, bullet_index=position,
                         raw=kit_latex(bullet.text), plain=bullet.plain,
                         skills=tuple(check.skills), support=check.support,
                         source_lines=check.source_lines, kit_id=bullet.id,
                         impact=bullet.impact, kit_line=bullet.line,
                         hooks=hooks.get(bullet.id, frozenset()))
        _score_candidate(cand, hits, jd_stems)
        candidates.append(cand)
    by_id = {c.kit_id: c for c in candidates}
    changed = True
    while changed:  # a bullet kept with another needs that one eligible, in this entry
        changed = False
        for cand in list(candidates):
            parent_id = analysis.kit.bullets[cand.kit_id].keep_with
            if parent_id is None:
                continue
            parent = by_id.get(parent_id)
            if parent is None:
                candidates.remove(cand)
                del by_id[cand.kit_id]
                rejected.append({"bullet": cand.plain, "kit_id": cand.kit_id, "stage": "eligibility",
                                 "index": cand.bullet_index, "reasons": [
                                     f"keeps with {parent_id}, which is not used here"]})
                changed = True
            else:
                cand.parent_index = parent.bullet_index
    return EntryPlan(si, ei, entry_id, candidates, [], 0, optional, keep=bool(candidates),
                     rejected=rejected, header_raw=header_raw)


def build_kit_plan(template: ParsedTemplate, master: MasterProfile, analysis: Analysis,
                   measure: Callable[[Sequence[tuple[str, str, str]]], dict[str, int]] | None
                   = None) -> ResumePlan:
    """Plan a resume from the resume kit: eligible bank bullets, chosen by coverage.

    With a compiler, one wrap probe measures every eligible bullet (*new* ones first) and
    every skills-row variant before anything is chosen; a bullet that takes two lines is
    left out with that reason.
    """
    kit, family = analysis.kit, analysis.family
    assert kit is not None and family is not None and analysis.kit_audit is not None
    plan = ResumePlan(template=template, entries={}, section_order=[], entry_order={},
                      summary_text=None, kv_items={}, kit_mode=True)
    hits = _requirement_hit_sets(analysis)
    jd_stems = _jd_stems(analysis.jd)
    positive_lower = _positive_text(master)
    restrictions, rule_warnings = kit_restrictions(kit)
    plan.warnings += rule_warnings
    excluded = kit_eligibility(kit, family, restrictions)
    rows = kit_skill_rows(analysis) if analysis.coverage is not None else []
    eligible = [b for b in kit.bullets.values() if b.id not in excluded
                and b.id not in analysis.kit_audit.blocked]
    eligible.sort(key=lambda b: not b.new)  # *new* bullets are wrap-checked first
    probe_items = [(b.id, kit_latex(b.text), "bullet") for b in eligible]
    probe_items += [(ident, _row_latex(row.name, items), "row")
                    for row in rows for ident, items in row_variants(row)]
    measured = measure(probe_items) if measure is not None else {}
    plan.wrap_probe = {"measured": len(measured), "new_first": [b.id for b in eligible if b.new],
                       "wrapped": {b.id: measured[b.id] for b in eligible
                                   if measured.get(b.id, 1) > 1}}
    for bid, lines in plan.wrap_probe["wrapped"].items():
        excluded[bid] = f"wraps to {lines} lines in the template (wrap probe)"
    hooks = kit_hooks(kit, analysis.kit_audit)
    assigned, warnings = _assign_kit_bullets(template, master, kit, analysis.kit_audit)
    plan.warnings += warnings
    for si, section in enumerate(template.sections):
        kind = template_section_kind(section.title)
        plan.section_order.append(si)
        if section.kind == "paragraph":
            plan.summary_text = _verified_summary(section, master, plan)
            continue
        if section.kind == "keyvalue":
            _plan_kv_section(si, section, kind, master, plan, hits, positive_lower)
            continue
        for ei, entry in enumerate(section.entries):
            if kind in KIT_SECTIONS:
                plan.entries[(si, ei)] = _plan_kit_entry(
                    si, ei, section, kind, entry, master, plan, assigned.get((si, ei), []),
                    analysis, excluded, hooks, hits, jd_stems)
            else:
                plan.entries[(si, ei)] = _plan_entry(si, ei, section, kind, entry, master, plan,
                                                     hits, jd_stems)
        plan.entry_order[si] = [ei for ei in range(len(section.entries))
                                if plan.entries[(si, ei)].keep]
    for ep in plan.entries.values():
        for item in ep.rejected:
            if item.get("kit_id"):
                plan.considered.append({"kit_id": item["kit_id"], "bullet": item["bullet"],
                                        "entry": _entry_name(plan, ep.section_index,
                                                             ep.entry_index),
                                        "reason": item["reasons"][0]})
    if analysis.coverage is not None and analysis.coverage.requirements:
        kit_skills(plan, analysis, rows, measured)
        kit_familiar(plan, analysis)
        kit_gap_line(plan, analysis)
        kit_summary(plan, analysis)
        select_from_kit(plan, analysis)
    else:
        _select_without_requirements(plan)
    return plan


def _select_without_requirements(plan: ResumePlan) -> None:
    """No scorable requirement: the highest-impact eligible bullets, three per entry."""
    for key, ep in plan.entries.items():
        if not ep.keep or not ep.candidates or ep.candidates[0].kit_id is None:
            continue
        ranked = sorted(ep.candidates, key=lambda c: (-c.impact, c.bullet_index))
        ep.selected = order_with_parents(ranked[:EXPERIENCE_MIN_BULLETS], ep.candidates)
        ep.floor = min(1, len(ep.selected))


def select_from_kit(plan: ResumePlan, analysis: Analysis) -> None:
    """Weighted set cover over the job's requirements with the kit's structure rules.

    Value of a bullet = coverage it adds (requirement weight x evidence strength, the
    scorer's own objective) x its kit impact / 5, per bullet of a dependency chain.

    - Experience: every role with a bullet that maps to a requirement is kept, with at
      least 3 bullets; a role is dropped only when none of its bullets maps.
    - Projects: the top 3 by requirement coverage; the top 2 get 3 or 4 bullets, the
      third up to 2. A project needs at least one mapped bullet.
    - Research entries need a mapped bullet; leadership is added when space allows.
    - The best proof of every core term is shown; depth bullets fill the budget.
    """
    model = analysis.coverage
    master = analysis.master
    weights = model.term_weights()
    for req in model.requirements:  # any one member of "A, B, or C" meets the whole ask
        for term in req.terms:
            for member in term.members:
                weights[member] = max(weights.get(member, Fraction(0)), weights[term.key])
    projects = _project_strengths(master)
    kinds = {si: template_section_kind(sec.title) for si, sec in enumerate(plan.template.sections)}
    term_keys = list(dict.fromkeys(list(weights) + [m for r in model.requirements
                                                     for t in r.terms for m in t.members]))
    req_terms = {r.id: {t.key for t in r.terms} | {m for t in r.terms for m in t.members}
                 for r in model.requirements}
    generic_terms = {t.key for r in model.requirements for t in r.terms if term_weight(t) < 1}
    pool: dict[tuple[int, int], list[Candidate]] = {}
    names: dict[tuple[int, int], str] = {}
    specific_ids: dict[str | None, tuple[str, ...]] = {}  # maps through a telling term
    for key, ep in sorted(plan.entries.items()):
        if not ep.keep or kinds[key[0]] not in KIT_SECTIONS:
            continue
        names[key] = _entry_name(plan, *key)
        strength = Fraction(_resume_strength(kinds[key[0]], names[key], projects), 3)
        for cand in ep.candidates:
            cand.proves = {k: strength * credit for k in term_keys
                           if (credit := hook_credit(k, cand.plain, cand.hooks))}
            specific = {k for k in cand.proves if k not in generic_terms}
            cand.requirement_ids = tuple(rid for rid, keys in req_terms.items()  # any term
                                         if keys & set(cand.proves))
            specific -= {f"skill:{lang}" for lang in GENERAL_LANGUAGES}  # everyone writes these
            specific_ids[cand.kit_id] = tuple(rid for rid, keys in req_terms.items()
                                              if keys & specific)
        pool[key] = list(ep.candidates)
    picked: dict[tuple[int, int], list[Candidate]] = {key: [] for key in pool}
    seed: dict[str, dict[str, Proof]] = {}  # the summary and skills rows are on the page too
    summary = " ".join(latex_to_plain(s) for s in plan.summary_sentences)
    skills = [", ".join(latex_to_plain(i) for i in items) for (si, _li), items in
              plan.kv_items.items() if kinds.get(si) == "skills"]
    for term in term_keys:
        if summary and _text_proves(term, summary):  # a claim the bullets should back up
            seed.setdefault(term, {})["summary"] = Proof(Fraction(STRENGTH_MENTION, 3),
                                                         STRENGTH_MENTION, summary, "summary",
                                                         "summary")
        elif any(_text_proves(term, row) for row in skills):
            seed.setdefault(term, {})["skills"] = Proof(SKILLS_ONLY_CREDIT, 0, "skills",
                                                        "skills", "skills")

    def table_for(chosen: dict[tuple[int, int], list[Candidate]]) -> dict[str, dict[str, Proof]]:
        table: dict[str, dict[str, Proof]] = {term: dict(e) for term, e in seed.items()}
        for key, cands in chosen.items():
            for cand in cands:
                for term, credit in cand.proves.items():
                    old = table.setdefault(term, {}).get(names[key])
                    if old is None or credit > old.credit:
                        table[term][names[key]] = Proof(credit, int(credit * 3), cand.plain,
                                                        names[key], "bullet")
        for req in model.requirements:
            for term in req.terms:
                if term.kind == "any_of":
                    table[term.key] = _merge_members(term, table)
        return table

    def coverage(table: dict[str, dict[str, Proof]]) -> Fraction:
        return sum((r.weight * _requirement_credit(r, table) for r in model.requirements),
                   Fraction(0))

    current = coverage(table_for(picked))

    def gain(key: tuple[int, int], unit: Sequence[Candidate]) -> Fraction:
        trial = {k: list(v) for k, v in picked.items()}
        trial[key] = trial[key] + list(unit)
        return coverage(table_for(trial)) - current

    def standalone(unit: Sequence[Candidate]) -> Fraction:
        return sum((weights.get(k, Fraction(0)) * v for c in unit for k, v in c.proves.items()),
                   Fraction(0))

    def impact(unit: Sequence[Candidate]) -> Fraction:
        return Fraction(sum(c.impact for c in unit), 5 * len(unit))

    specific_keys = [k for k in term_keys if k not in generic_terms and not k.startswith("any:")]
    worded = {c.kit_id: {k for k in specific_keys if _text_proves(k, c.plain)}
              for cands in pool.values() for c in cands}

    on_page = {k for k in specific_keys if (summary and _text_proves(k, summary)) or
               any(_text_proves(k, row) for row in skills)}  # summary and skills words

    def new_words(cands: Sequence[Candidate], shown: Sequence[Candidate]) -> int:
        seen = set(on_page).union(*(worded[c.kit_id] for c in shown))
        return len(set().union(*(worded[c.kit_id] for c in cands)) - seen) if cands else 0

    cap: dict[tuple[int, int], int] = {}
    floor: dict[tuple[int, int], int] = {}
    experiences = [k for k in pool if kinds[k[0]] == "experience"
                   and any(specific_ids[c.kit_id] for c in pool[k])
                   and len(pool[k]) >= EXPERIENCE_MIN_BULLETS]
    for key in pool:
        if kinds[key[0]] == "experience" and key not in experiences:
            plan.template_items_removed.append({
                "section": plan.template.sections[key[0]].title, "item": names[key],
                "reason": "no eligible bullet maps to a requirement of this job"
                if not any(specific_ids[c.kit_id] for c in pool[key]) else
                f"only {len(pool[key])} bullet(s) suit the {analysis.family.code} family; an "
                f"included role needs {EXPERIENCE_MIN_BULLETS}", "location": "entry"})
    # Projects: the set of up to 3 that adds the most beyond the roles (coverage, the
    # relevance of each project's best bullets, asked words no role shows), then ordered by
    # what the set would lose without each one.
    base = {k: [c for c in pool[k] if c.requirement_ids] for k in experiences}
    candidates_by_project = {k: [c for c in pool[k] if c.requirement_ids] for k in pool
                             if kinds[k[0]] == "projects"
                             and any(specific_ids[c.kit_id] for c in pool[k])}
    shown_by_roles = [c for v in base.values() for c in v]
    base_coverage = coverage(table_for(base))

    telling = {k for k in term_keys if k not in generic_terms and k not in
               {f"skill:{lang}" for lang in GENERAL_LANGUAGES}}  # what a project is about

    def set_value(keys: Sequence[tuple[int, int]]) -> Fraction:
        chosen = {**base, **{k: candidates_by_project[k] for k in keys}}
        provers: dict[str, int] = {}  # depth has diminishing returns: a term proved by n
        for cands in chosen.values():  # entries gives each of them 1/n of its relevance
            for term in {t for c in cands for t in c.proves}:
                provers[term] = provers.get(term, 0) + 1
        depth = Fraction(0)
        for k in keys:
            for cand in sorted(candidates_by_project[k],
                               key=lambda c: -standalone([c]) * c.impact)[:3]:
                depth += sum((weights.get(t, Fraction(0)) * v / provers[t]
                              for t, v in cand.proves.items() if t in telling),
                             Fraction(0)) * impact([cand])
        mapped = [c for k in keys for c in candidates_by_project[k]]
        return coverage(table_for(chosen)) - base_coverage + PROJECT_DEPTH_WEIGHT * depth + \
            KEYWORD_WEIGHT * new_words(mapped, shown_by_roles)

    size = min(PROJECT_LIMIT, len(candidates_by_project))
    if os.environ.get("JOBAUTO_DEBUG_PROJECTS"):
        for combo in itertools.combinations(sorted(candidates_by_project), size):
            mapped = [c for k in combo for c in candidates_by_project[k]]
            chosen = {**base, **{k: candidates_by_project[k] for k in combo}}
            print([names[k][:10] for k in combo], round(float(set_value(combo)), 3),
                  "cov", round(float(coverage(table_for(chosen)) - base_coverage), 3),
                  "words", new_words(mapped, shown_by_roles))
    best_set = max(itertools.combinations(sorted(candidates_by_project), size),
                   key=lambda combo: (set_value(combo), [-k[1] for k in combo]), default=())
    total = set_value(best_set) if best_set else Fraction(0)
    project_value: dict[tuple[int, int], tuple] = {
        k: (total - set_value([x for x in best_set if x != k]),
            max(c.impact for c in candidates_by_project[k]), -k[1]) for k in best_set}
    ranked = sorted(best_set, key=lambda k: project_value[k], reverse=True)
    for key in sorted(candidates_by_project):
        if key not in best_set:
            project_value[key] = (set_value([key]) - 1000, 0, -key[1])  # reported, not chosen
            ranked.append(key)
    plan.project_scores = {names[k]: round(float(project_value[k][0]), 3) for k in ranked}
    chosen_projects = ranked[:PROJECT_LIMIT]
    for rank, key in enumerate(chosen_projects):
        plan.project_ranks[key] = rank
        cap[key] = TOP_PROJECT_BULLETS[1] if rank < TOP_PROJECTS else OTHER_PROJECT_BULLETS
        floor[key] = PROJECT_FLOORS[min(rank, len(PROJECT_FLOORS) - 1)]
    for key in ranked[PROJECT_LIMIT:]:
        plan.template_items_removed.append({
            "section": plan.template.sections[key[0]].title, "item": names[key],
            "reason": f"9.7: only the top {PROJECT_LIMIT} projects by requirement coverage",
            "location": "entry"})
        for cand in pool[key]:
            plan.considered.append({"kit_id": cand.kit_id, "bullet": cand.plain,
                                    "entry": names[key],
                                    "reason": f"project ranked below the top {PROJECT_LIMIT}"})
    for key in pool:
        if kinds[key[0]] == "projects" and key not in project_value:
            plan.template_items_removed.append({
                "section": plan.template.sections[key[0]].title, "item": names[key],
                "reason": "no eligible bullet maps to a requirement of this job",
                "location": "entry"})
    research = [k for k in pool if kinds[k[0]] == "research"
                and any(specific_ids[c.kit_id] for c in pool[k])]
    leadership = [k for k in pool if kinds[k[0]] == "leadership"]
    for key in experiences:
        floor[key] = min(EXPERIENCE_MIN_BULLETS, len(pool[key]))
    for key in research:
        cap[key] = RESEARCH_BULLETS
    for key in leadership:
        cap[key] = LEADERSHIP_BULLETS
    active = experiences + chosen_projects + research

    def room(key: tuple[int, int]) -> int:
        return cap.get(key, 99) - len(picked[key])

    def units(keys, mapped_only: bool = True):
        for key in keys:
            taken = {c.bullet_index for c in picked[key]}
            by_index = {c.bullet_index: c for c in pool[key]}
            for cand in pool[key]:
                if cand.bullet_index in taken or (mapped_only and not cand.requirement_ids):
                    continue
                chain = [a for a in _ancestors(cand, by_index) if a.bullet_index not in taken]
                if len(chain) + 1 <= room(key):
                    yield key, chain + [cand]

    def best_unit(keys, score, mapped_only: bool = True):
        best = None
        for key, unit in units(keys, mapped_only):
            rank = (score(key, unit), impact(unit), standalone(unit), -len(unit), -key[0],
                    -key[1], -unit[-1].bullet_index)
            if best is None or rank > best[0]:
                best = (rank, key, unit)
        return best

    def take(key: tuple[int, int], unit: Sequence[Candidate], value: Fraction) -> None:
        nonlocal current
        for cand in unit:
            cand.value = float(value)
            picked[key].append(cand)
        current = coverage(table_for(picked))

    def worth(key: tuple[int, int], unit: Sequence[Candidate]) -> Fraction:
        """New coverage plus repeated proof of what the job weighs, times impact, per bullet."""
        return (gain(key, unit) + DEPTH_WEIGHT * standalone(unit)) * impact(unit) / len(unit)

    count = 0
    while count < KIT_BULLET_BUDGET:  # 1. greedy: coverage and relevance x impact
        best = best_unit(active, lambda k, u: worth(k, u) if gain(k, u) > 0 else Fraction(0))
        if best is None or best[0][0] <= 0:
            break
        take(best[1], best[2], best[0][0])
        count += len(best[2])
    shown = table_for(picked)
    for req in model.requirements:  # 2. the best proof of every core term is shown
        if req.type != "core":
            continue
        for term in req.terms:
            if term_weight(term) < 1 or shown.get(term.key) or term.key not in weights:
                continue
            best = best_unit(active, lambda _k, u, t=term.key: max(
                (c.proves.get(t, Fraction(0)) for c in u), default=Fraction(0)) * impact(u))
            if best is not None and best[0][0] > 0:
                take(best[1], best[2], max(gain(best[1], best[2]), Fraction(1, 100)))
                shown = table_for(picked)
    target = {key: (TOP_PROJECT_BULLETS[0] if plan.project_ranks[key] < TOP_PROJECTS else 1)
              for key in chosen_projects}
    for key in experiences + chosen_projects:  # 3. floors, mapped bullets first
        while len(picked[key]) < target.get(key, floor.get(key, 1)):
            best = best_unit([key], worth)
            if best is None:
                break
            take(key, best[2], best[0][0])
        while kinds[key[0]] == "experience" and len(picked[key]) < floor[key]:
            taken = {c.bullet_index for c in picked[key]}  # then the highest-impact others,
            best = best_unit([key], lambda _k, u, t=taken: Fraction(  # continuations first
                int(u[0].parent_index in t)) + impact(u), mapped_only=False)
            if best is None:
                break
            take(key, best[2], Fraction(0))
    for key in research + leadership:  # 4. research and leadership when space allows
        while room(key) > 0:
            best = best_unit([key], lambda _k, u: standalone(u) + impact(u),
                             mapped_only=kinds[key[0]] == "research")
            if best is None:
                break
            take(key, best[2], Fraction(0))
    count = sum(len(v) for v in picked.values())
    while count < KIT_BULLET_BUDGET:  # 5. depth: more mapped proof; fitting decides
        best = best_unit(experiences + chosen_projects, worth)
        if best is None or best[0][0] <= 0:
            break
        take(best[1], best[2], best[0][0])
        count += len(best[2])
    provers: dict[str, int] = {}  # depth is shared: a term proved by n bullets gives each 1/n
    for cand in (c for v in picked.values() for c in v):
        for term in cand.proves:
            provers[term] = provers.get(term, 0) + 1
    for key, chosen in picked.items():  # revalue symmetrically: what the page loses without it
        by_index = {c.bullet_index: c for c in pool[key]}
        for cand in chosen:
            gone = {c.bullet_index for c in chosen if cand in _ancestors(c, by_index)}
            without = {k: ([c for c in v if c is not cand and c.bullet_index not in gone]
                           if k == key else v) for k, v in picked.items()}
            unique = current - coverage(table_for(without))
            others = [c for k, v in picked.items() for c in v if c is not cand]
            depth = sum((weights.get(t, Fraction(0)) * v / provers[t]
                         for t, v in cand.proves.items()), Fraction(0))
            cand.value = float((unique + DEPTH_WEIGHT * depth +
                                KEYWORD_WEIGHT * new_words([cand], others)) * impact([cand]))
    every = {key: list(cands) for key, cands in pool.items()}
    reachable, final = table_for(every), table_for(picked)
    plan.selection_check = {
        "unmapped_bullets": [c.plain for k, cands in picked.items() for c in cands
                             if not c.requirement_ids and kinds[k[0]] != "leadership"],
        "uncovered_coverable_requirements": [
            r.id for r in model.requirements
            if _requirement_credit(r, model.profile) > 0 and
            _requirement_credit(r, reachable) > 0 and _requirement_credit(r, final) == 0],
    }
    plan.selection_check["violation"] = bool(
        plan.selection_check["unmapped_bullets"] and
        plan.selection_check["uncovered_coverable_requirements"])
    for key, ep in plan.entries.items():
        if key not in pool:
            continue
        si, ei = key
        chosen = picked[key]
        for cand in ep.candidates:
            if cand not in chosen and not any(c["kit_id"] == cand.kit_id
                                              for c in plan.considered):
                plan.considered.append({
                    "kit_id": cand.kit_id, "bullet": cand.plain, "entry": names[key],
                    "reason": "no requirement it maps to was still uncovered" if
                    cand.requirement_ids else "maps to no requirement of this job"})
        if not chosen:
            ep.keep = False
            ep.selected = []
            continue
        if kinds[si] == "projects":  # a result or impact bullet beats a detail bullet
            ordered = sorted(chosen, key=lambda c: (-standalone([c]) * c.impact,
                                                    -(c.value or 0), c.bullet_index))
        else:
            ordered = sorted(chosen, key=lambda c: (-(c.value or 0), -c.impact, c.bullet_index))
        ep.selected = order_with_parents(ordered, pool[key])
        without = {k: (v if k != key else []) for k, v in picked.items()}
        others = [c for k, v in picked.items() if k != key for c in v]
        ep.coverage_value = float(current - coverage(table_for(without)) +  # what the page
                                  KEYWORD_WEIGHT * new_words(ep.selected, others)) + \
            float(standalone(ep.selected)) / 1000  # loses without this entry, words included
        ep.optional = True
        ep.floor = min(floor.get(key, 1), len(ep.selected))  # never a header without bullets
        plan.floors[key] = ep.floor
    for si in list(plan.entry_order):
        order = [ei for ei in range(len(plan.template.sections[si].entries))
                 if plan.entries[(si, ei)].keep]
        if kinds[si] == "experience":
            order.sort(key=lambda ei: (tuple(-d for d in _entry_dates(
                plan.template.sections[si].entries[ei])), ei))
        elif kinds[si] == "projects":
            order.sort(key=lambda ei: (plan.project_ranks.get((si, ei), 99), ei))
        elif kinds[si] in ("research", "leadership"):
            order.sort(key=lambda ei: (-plan.entries[(si, ei)].worth(), ei))
        plan.entry_order[si] = order


# ---------------------------------------------------------------------------
# Kit mode: summary, skills rows and the gap line (Step 5)
# ---------------------------------------------------------------------------
SKILLS_ROW_CHARS = 118       # one line of "Label: a, b, c" at the template's skills width
SKILLS_ROWS_MAX = 6
LIGHT_ROW_EXTRA = 3          # "AI and ML (light)": the job's items plus this many more
GAP_UNMET_CREDIT = Fraction(1, 3)  # coursework or a skills-list mention is not experience
FACT_SPLIT = re.compile(r",\s+(?:and\s+)?(?=(?:a|an|the)\b|[a-z]+(?:ed|s)\b)|;\s+")


def _summary_facts(sentence: str) -> tuple[str, list[str]]:
    """'Identity: fact, fact, and fact.' -> (identity, facts); other sentences are one fact."""
    head, colon, tail = sentence.partition(": ")
    if not colon:
        return "", [sentence]
    parts, depth, current = [], 0, ""
    for index, char in enumerate(tail):
        depth += {"(": 1, ")": -1}.get(char, 0)
        current += char
        rest = tail[index + 1:]
        if depth == 0 and char == "," and FACT_SPLIT.match(tail[index:]) and \
                not re.match(r"\s+(?:and\s+)?[A-Z]", rest):
            parts.append(current[:-1].strip())
            current = ""
    parts.append(current.strip().rstrip("."))
    parts = [re.sub(r"^and\s+", "", p) for p in parts if p]
    return head, parts


def _fact_terms(fact: str, analysis: Analysis) -> set[str]:
    """Terms a summary fact proves: its words, plus the hooks of the kit bullet it restates."""
    model = analysis.coverage
    keys = {k for r in model.requirements for t in r.terms for k in (t.key, *t.members)}
    found = {k for k in keys if _text_proves(k, fact)}
    stems = content_stems(fact)
    numbers = {_normalize_number(m.group(0)) for m in STANDALONE_NUMBER.finditer(fact)}
    best = max(((len(stems & content_stems(b.plain)) + 2 * len(numbers & {
        _normalize_number(m.group(0)) for m in STANDALONE_NUMBER.finditer(b.plain)}), b.id)
                for b in analysis.kit.bullets.values()), default=(0, ""))
    if best[0] >= 3:
        hooks = analysis.kit_hooks.get(best[1], frozenset())
        found |= {k for k in keys if proves_with_hooks(k, analysis.kit.bullets[best[1]].plain,
                                                       hooks)}
    return found


def kit_summary(plan: ResumePlan, analysis: Analysis) -> None:
    """The family's 9.3 summary, keeping only facts that answer a high-weight requirement.

    Text is the kit's own (verbatim facts, no new claims); a fact that maps to no
    requirement of weight 2 or more is dropped. A clearance requirement adds the 9.7
    clearance line. No awards unless the job asks for them; label openers never appear.
    """
    kit, family, model, master = analysis.kit, analysis.family, analysis.coverage, \
        analysis.master
    entry = kit.summaries.get(family.code)
    if entry is None:
        plan.warnings.append(f"No 9.3 summary for {family.code}; the template summary stays")
        return
    if family.code in analysis.kit_audit.summary_blocked:
        plan.warnings.append(f"9.3 summary for {family.code} not used: "
                             f"{analysis.kit_audit.summary_blocked[family.code][0]}")
        return
    high = {k for r in model.requirements if r.weight >= WEIGHT_REQUIRED
            for t in r.terms for k in (t.key, *t.members)}
    sentences = [s.strip() for s in re.split(r"(?<=\.)\s+(?=[A-Z*])", entry[1]) if s.strip()]
    kept: list[str] = []
    plan.summary_facts = []
    for sentence in sentences:
        plain = resume_kit.BOLD.sub(r"\1", sentence)
        identity, facts = _summary_facts(plain)
        mapped = []
        for fact in facts:
            terms = _fact_terms(fact, analysis) & high
            plan.summary_facts.append({"fact": fact, "maps_to": sorted(
                r.id for r in model.requirements if r.weight >= WEIGHT_REQUIRED and
                {k for t in r.terms for k in (t.key, *t.members)} & terms)})
            if terms:
                mapped.append(fact)
            else:
                plan.template_items_removed.append({
                    "section": "Summary", "item": fact, "location": "summary fact",
                    "reason": "maps to no requirement of weight 2 or more"})
        if not mapped:
            continue
        if len(mapped) == len(facts):  # nothing dropped: the kit's own sentence, verbatim
            kept.append(sentence if sentence.endswith(".") else sentence + ".")
            continue
        if identity:
            body = mapped[0] if len(mapped) == 1 else (
                ", ".join(mapped[:-1]) + (", and " if len(mapped) > 2 else " and ") + mapped[-1])
            text = f"{identity}: {body}."
        else:
            text = mapped[0] if mapped[0].endswith(".") else mapped[0] + "."
        kept.append(_bold_like_kit(text, sentence))
    statement = kit_required_statement(analysis)
    if statement:
        kept.append(statement)
    kept = [s for s in kept if not BANNED_SUMMARY_OPENERS.match(resume_kit.BOLD.sub(r"\1", s))]
    if not kept:
        return
    plan.summary_sentences = [kit_latex(s) for s in kept]
    plan.summary_text = "\n    " + " ".join(plan.summary_sentences) + "\n    "
    plan.summary_source = {"family": family.code, "kit_line": entry[0]}


def _bold_like_kit(text: str, source: str) -> str:
    """Re-apply the kit's **bold** (tech) markers to a shortened sentence."""
    for term in sorted(set(resume_kit.BOLD.findall(source)), key=len, reverse=True):
        text = re.sub(rf"(?<![\w*]){re.escape(term)}(?![\w*])", f"**{term}**", text, count=1)
    return text


def kit_required_statement(analysis: Analysis) -> str | None:
    """Clearance (9.7 wording) or citizenship, only when the job asks and it is true."""
    model, master = analysis.coverage, analysis.master
    needs = {h.type: h for h in model.hard}
    citizenship, _clearance, _city = _master_identity(master)
    canadian = citizenship is not None and re.search(r"\bcanadian\b", _line_text(citizenship),
                                                     re.IGNORECASE)
    if "clearance" in needs and needs["clearance"].status == "met":
        statement = kit_clearance_statement(analysis.kit)
        if statement:
            return statement[0].rstrip(".") + "."
    if "citizenship" in needs and needs["citizenship"].status == "met" and canadian:
        return "Canadian citizen."
    return None


def _row_items(spec: resume_kit.RowSpec, kit: resume_kit.ResumeKit) -> list[str]:
    library = list(kit.rows[spec.row][1])
    return [i for i in library if i in spec.only] if spec.only else library


@dataclass
class SkillRowPlan:
    name: str
    job: list[str]          # items the job asks for: never cut
    other: list[str]        # library items that may fill the rest of the line


def kit_skill_rows(analysis: Analysis) -> list[SkillRowPlan]:
    """Rows from 9.4 in the family's order (at most 6) with the job's terms first.

    The core-skill guard adds any core-requirement language or tool from the row library
    that no row shows. Audit-blocked items never appear; an item shows in one row only,
    unless the row order names it explicitly ("Backend (Python, SQL)").
    """
    kit, family, model = analysis.kit, analysis.family, analysis.coverage
    specs = list(kit.row_order.get(family.code, ()))[:SKILLS_ROWS_MAX]
    named: set[str] = set()      # skills the job names (or lists as alternatives)
    related: set[str] = set()    # members of a category the job names ("machine learning")
    concepts: list[Concept] = []
    core: list[str] = []
    for req in model.requirements:
        for term in req.terms:
            for key in (term.members or (term.key,)):
                kind, name = key.split(":", 1)
                if kind == "skill":
                    named.add(name)
                    related |= set(SKILL_INDEX[name].members)
                    if req.type == "core" and SKILL_INDEX[name].kind != "category":
                        core.append(name)
                elif CONCEPT_INDEX[name] not in concepts:
                    concepts.append(CONCEPT_INDEX[name])
    blocked = analysis.kit_audit.row_blocked

    def asked(item: str) -> bool:  # the job names this tool
        return bool(set(SKILL_FINDER.keys(item)) & named)

    def relevant(item: str) -> bool:  # it proves an asked practice, or belongs to a named group
        return bool(set(SKILL_FINDER.keys(item)) & related) or \
            any(concept_proven(c, item)[0] for c in concepts)

    rows: list[SkillRowPlan] = []
    shown: set[str] = set()
    for spec in specs:
        items = [i for i in _row_items(spec, kit) if i not in blocked]
        items = [i for i in items if i not in shown or i in spec.only]
        job = [i for i in items if asked(i)]
        other = sorted((i for i in items if i not in job), key=lambda i: not relevant(i))
        rows.append(SkillRowPlan(spec.row, job, other[:LIGHT_ROW_EXTRA] if spec.light else other))
        shown |= set(items)
    present = {k for row in rows for i in row.job + row.other for k in SKILL_FINDER.keys(i)}
    for name in dict.fromkeys(core):  # the core-skill guard
        if name in present or name in PRACTICE_KEYS:
            continue
        home = next(((row, i) for row, (_line, items) in kit.rows.items() for i in items
                     if name in SKILL_FINDER.keys(i) and i not in blocked), None)
        if home is None:
            continue
        row_name, item = home
        target = next((r for r in rows if r.name == row_name), None)
        if target is None and len(rows) < SKILLS_ROWS_MAX:
            target = SkillRowPlan(row_name, [], [])
            rows.append(target)
        target = target or rows[0]
        target.job.insert(0, item)
        present.add(name)
    return rows


def row_variants(row: SkillRowPlan) -> list[tuple[str, list[str]]]:
    """(probe id, items): the job's items plus 0..n library items, longest first."""
    return [(f"row-{slugify(row.name, 20)}-{k}", row.job + row.other[:k])
            for k in range(len(row.other), -1, -1)]


def _row_latex(name: str, items: Sequence[str]) -> str:
    return f"\\textbf{{{latex_escape(name)}}}{{: {', '.join(latex_escape(i) for i in items)}}}"


def kit_skills(plan: ResumePlan, analysis: Analysis, rows: Sequence[SkillRowPlan],
               lines: dict[str, int]) -> None:
    """Render the rows: each keeps every job item and as many others as fit on one line
    (measured by the wrap probe when a compiler is present, else by character count)."""
    section_index = next((si for si, sec in enumerate(plan.template.sections)
                          if sec.kind == "keyvalue" and template_section_kind(sec.title) ==
                          "skills"), None)
    if section_index is None or not rows:
        return
    chosen: list[tuple[str, list[str]]] = []
    for row in rows:
        pick = None
        for ident, items in row_variants(row):
            measured = lines.get(ident)
            fits = measured == 1 if measured is not None else \
                len(row.name) + 2 + len(", ".join(items)) <= SKILLS_ROW_CHARS
            if fits:
                pick = items
                break
        if pick is None:
            pick = row.job
            plan.warnings.append(f"Skills row '{row.name}': its job items alone take more than "
                                 f"one line")
        if pick:
            chosen.append((row.name, pick))
        for item in row.other:
            if item not in pick:
                plan.template_items_removed.append({
                    "section": "Technical Skills", "item": f"{row.name}: {item}",
                    "reason": "not asked by this job and no room on the row's line",
                    "location": "list item"})
    section = plan.template.sections[section_index]
    kv = [KVLine(latex_escape(name), name, [latex_escape(i) for i in items])
          for name, items in chosen]
    sections = list(plan.template.sections)
    sections[section_index] = dataclasses.replace(section, kv_lines=kv)
    plan.template = dataclasses.replace(plan.template, sections=sections)
    for key in [k for k in plan.kv_items if k[0] == section_index]:
        del plan.kv_items[key]
    for li, line in enumerate(kv):
        plan.kv_items[(section_index, li)] = line.items
        plan.kit_rows[(section_index, li)] = analysis.kit.rows[line.label][0]
    plan.kv_order[section_index] = list(range(len(kv)))


def kit_gap_line(plan: ResumePlan, analysis: Analysis) -> None:
    """Interests: the family's 9.8 gaps the job asks for and the profile does not meet."""
    kit, family, model, master = analysis.kit, analysis.family, analysis.coverage, \
        analysis.master
    jd_text = "\n".join(l.text for l in analysis.jd.lines if l.section_type != "info")
    asked_terms = {}
    for req in model.requirements:
        for term in req.terms:
            for key in (term.key, *term.members):
                asked_terms.setdefault(key, []).append(req)
    positive = _positive_text(master)
    gaps = []
    for item in kit.gaps.get(family.code, (0, ()))[1]:
        terms = {k for k in phrase_terms(item) if k in asked_terms}
        literal = [w for w in re.findall(r"[A-Za-z][\w.+-]*[A-Z][\w.+-]*|\b[A-Z][a-z]+-\w+(?:-\w+)*",
                                         item) if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])",
                                                            jd_text)]
        if not terms and not literal:
            continue
        unmet_terms = [k for k in terms if (model.best(k) is None or
                                            model.best(k).credit <= GAP_UNMET_CREDIT)]
        unmet_literal = [w for w in literal if not _literal_in(positive, w)]
        if terms and not unmet_terms and not unmet_literal:
            continue
        reqs = [r for k in (unmet_terms or terms) for r in asked_terms.get(k, [])]
        gaps.append({"item": item, "term": (unmet_terms or sorted(terms) or [None])[0],
                     "requirements": sorted({r.id for r in reqs}),
                     "jd_lines": sorted({r.jd_line for r in reqs}) or [
                         next((l.number for l in analysis.jd.lines if any(
                             w in l.text for w in literal)), 0)]})
    distinct = {t.key for r in model.requirements for t in r.terms if term_weight(t) >= 1}
    count = max(1, min(4, -(-len(distinct) // 10)))  # about 10% of the job's terms
    gaps = gaps[:count]
    for si, section in enumerate(plan.template.sections):
        if section.kind != "keyvalue":
            continue
        for li, kv in enumerate(section.kv_lines):
            if "interest" not in kv.label.lower():
                continue
            for item in plan.kv_items.get((si, li), []):
                plan.template_items_removed.append({
                    "section": section.title, "item": f"{kv.label}: {latex_to_plain(item)}",
                    "reason": "Interests lists this family's 9.8 gaps the job asks for",
                    "location": "list item"})
            plan.kv_items[(si, li)] = [latex_escape(g["item"]) for g in gaps]
            plan.gap_lines[(si, li)] = gaps


def kit_familiar(plan: ResumePlan, analysis: Analysis) -> None:
    """'Familiar with' keeps only exposure items the job names (as in legacy mode)."""
    named = {k.split(":", 1)[1] for r in analysis.coverage.requirements for t in r.terms
             for k in (t.key, *t.members) if k.startswith("skill:")}
    for si, section in enumerate(plan.template.sections):
        if section.kind != "keyvalue" or template_section_kind(section.title) == "skills":
            continue
        for li, kv in enumerate(section.kv_lines):
            if "familiar" not in kv.label.lower():
                continue
            items = plan.kv_items.get((si, li), [])
            kept = [i for i in items if set(SKILL_FINDER.keys(latex_to_plain(i))) & named]
            for item in items:
                if item not in kept:
                    plan.template_items_removed.append({
                        "section": section.title, "item": f"{kv.label}: {latex_to_plain(item)}",
                        "reason": "not named by this job", "location": "list item"})
            plan.kv_items[(si, li)] = kept


EXPERIENCE_MIN_BULLETS = FLOOR_RELEVANT_EXPERIENCE
STRONG_PROJECT_BULLETS = 3   # bullets for a project covering 2+ high-weight requirements
REWRITES_NAME = "rewrites.json"


def load_rewrites(directory: Path) -> list[dict[str, Any]]:
    """Truthful wording rules from <template dir>/rewrites.json (optional).

    Each rule: {"find": text, "replace": text, "when": [concept keys], "repair":
    bool, "note": text}. "when" rules move a verified bullet into the job's
    vocabulary when the job asks for one of those concepts; "repair" rules
    restate a rejected template bullet in the master profile's own words. Every
    result must pass claim verification again, so a rule can never add a fact.
    """
    path = directory / REWRITES_NAME
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TemplateStructureError(f"{path} is not valid JSON: {exc}") from exc
    rules = data.get("rewrites") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not all(
            isinstance(r, dict) and isinstance(r.get("find"), str) and
            isinstance(r.get("replace"), str) and r["find"] for r in rules):
        raise TemplateStructureError(f"{path} needs a 'rewrites' list of {{find, replace}} rules")
    for rule in rules:
        unknown = [k for k in rule.get("when", []) if k not in CONCEPT_INDEX]
        if unknown:
            raise TemplateStructureError(f"{path}: unknown concept(s) {unknown} in 'when'")
    return rules


def apply_rewrites(plan: ResumePlan, analysis: Analysis, kinds: dict[int, str]) -> None:
    """Apply rewrites.json rules to candidates (vocabulary) and rejected bullets (repair)."""
    rules = load_rewrites(plan.template.path.parent)
    if not rules:
        return
    master = analysis.master
    asked = {t.key.split(":", 1)[1] for r in analysis.coverage.requirements for t in r.terms
             if t.kind == "concept"}
    limit = max((len(b.plain) for sec in plan.template.sections for e in sec.entries
                 for b in e.bullets), default=140)
    hits = _requirement_hit_sets(analysis)
    jd_stems = _jd_stems(analysis.jd)

    def rewrite(text: str, rule: dict[str, Any]) -> str | None:
        if text.count(rule["find"]) != 1:
            return None
        new = text.replace(rule["find"], rule["replace"])
        return new if len(new) <= max(limit, len(text)) else None

    for (si, ei), ep in sorted(plan.entries.items()):
        if not ep.keep or ep.master_entry_id is None or kinds[si] not in (
                "experience", "projects", "research", "leadership"):
            continue
        scope = build_scope(master, ep.master_entry_id)
        name = _entry_name(plan, si, ei)
        for cand in ep.candidates:
            for rule in rules:
                if rule.get("repair") or not set(rule.get("when", [])) & asked:
                    continue
                new = rewrite(cand.plain, rule)
                check = verify_claim(new, master, scope) if new else None
                if check is None or not check.ok:
                    continue
                plan.rewrites.append({"entry": name, "kind": "vocabulary", "original": cand.plain,
                                      "rewritten": new, "note": rule.get("note", "")})
                cand.plain, cand.raw = new, _latex_with_bold_tech(new)
                cand.skills, cand.source_lines = tuple(check.skills), check.source_lines
        for item in list(ep.rejected):
            for rule in rules:
                if not rule.get("repair") or "index" not in item:
                    continue
                new = rewrite(item["bullet"], rule)
                check = verify_claim(new, master, scope) if new else None
                if check is None or not check.ok or any(  # restates a kept bullet
                        content_stems(new) <= content_stems(c.plain) for c in ep.candidates):
                    continue
                cand = Candidate(section_index=si, entry_index=ei, bullet_index=item["index"],
                                 raw=_latex_with_bold_tech(new), plain=new,
                                 skills=tuple(check.skills), support=check.support,
                                 source_lines=check.source_lines)
                if DEPENDENT_BULLET.match(new) and kinds[si] not in ("projects", "research"):
                    parent = next((c for c in ep.candidates
                                   if c.bullet_index == item["index"] - 1), None)
                    if parent is None:
                        continue
                    cand.parent_index = parent.bullet_index
                _score_candidate(cand, hits, jd_stems)
                ep.candidates = sorted([*ep.candidates, cand], key=lambda c: c.bullet_index)
                ep.rejected.remove(item)
                plan.rewrites.append({"entry": name, "kind": "repair", "original": item["bullet"],
                                      "rewritten": new, "rejected_because": item["reasons"][0],
                                      "note": rule.get("note", "")})
                break


def _item_relevance(item: str, skill_keys: set[str], concepts: list[Concept],
                    groups: set[str]) -> int:
    """2 when the job asks for the item (or a concept it proves), 1 when related, else 0."""
    plain = latex_to_plain(item)
    keys = set(SKILL_FINDER.keys(plain))
    if keys & skill_keys or any(concept_proven(c, plain)[0] for c in concepts):
        return 2
    if any(SKILL_INDEX[k].group in groups for k in keys):
        return 1
    return 0


def job_gaps(model: CoverageModel, master: MasterProfile) -> list[dict[str, Any]]:
    """The job's top unmet asks (about 10% of its terms) for the Interests line.

    Only specific asks with no evidence at all qualify; generic process words,
    skills marked [NO] and alternatives already satisfied never do.
    """
    weights = model.term_weights()
    found: dict[str, dict[str, Any]] = {}
    for req in model.requirements:
        for term in req.terms:
            kind, key = term.key.split(":", 1)
            if term.kind == "any_of" or term_weight(term) < 1 or model.best(term.key):
                continue
            if kind == "skill" and (key in master.global_no or key in master.scoped_no):
                continue
            label = (CONCEPT_INDEX[key].gap_label or CONCEPT_INDEX[key].display.lower()) \
                if kind == "concept" else SKILL_INDEX[key].display
            gap = found.setdefault(term.key, {"item": label, "term": term.key,
                                              "weight": weights[term.key],
                                              "requirements": [], "jd_lines": []})
            gap["requirements"].append(req.id)
            gap["jd_lines"].append(req.jd_line)
    distinct = {t.key for r in model.requirements for t in r.terms if term_weight(t) >= 1}
    count = max(2, min(4, -(-len(distinct) // 10)))
    ranked = sorted(found.values(), key=lambda g: (-g["weight"], g["jd_lines"][0], g["item"]))
    return [{k: v for k, v in g.items() if k != "weight"} for g in ranked[:count]]


def order_skills(plan: ResumePlan, analysis: Analysis) -> None:
    """Rows by job relevance, core skills guaranteed, leftovers out, gaps in Interests.

    - Items the job asks for (or that prove a concept it asks for) score 2,
      items in a related group 1; other items are dropped.
    - Every core skill the career bank evidences appears in the skills section.
    - "Familiar with" keeps only items the job names itself; "Interests" lists the
      job's top unmet asks (``job_gaps``), never leftover tools. Languages stays.
    """
    model = analysis.coverage
    master = analysis.master
    skill_keys: set[str] = set()
    named: set[str] = set()
    concepts: list[Concept] = []
    for req in model.requirements:
        for term in req.terms:
            for key in (term.members or (term.key,)):
                kind, name = key.split(":", 1)
                if kind == "skill":
                    named.add(name)
                    skill_keys |= {name, *SKILL_INDEX[name].members}
                elif kind == "concept" and CONCEPT_INDEX[name] not in concepts:
                    concepts.append(CONCEPT_INDEX[name])
    groups = {SKILL_INDEX[k].group for k in named} | {g for c in concepts for g in c.groups}
    skill_rows: list[tuple[int, int]] = []
    for si, section in enumerate(plan.template.sections):
        if section.kind != "keyvalue":
            continue
        kind = template_section_kind(section.title)
        scores: dict[int, int] = {}
        for li, kv in enumerate(section.kv_lines):
            label = kv.label.lower()
            if "interest" in label:
                gaps = job_gaps(model, master)
                for item in plan.kv_items.get((si, li), []):
                    plan.template_items_removed.append({
                        "section": section.title, "item": f"{kv.label}: {latex_to_plain(item)}",
                        "reason": "Interests lists this job's unmet asks", "location": "list item"})
                plan.kv_items[(si, li)] = [latex_escape(g["item"]) for g in gaps]
                plan.gap_lines[(si, li)] = gaps
                continue
            familiar = "familiar" in label
            if kind != "skills" and not familiar:
                continue
            items = plan.kv_items.get((si, li), [])
            if familiar:
                rated = [(2 if set(SKILL_FINDER.keys(latex_to_plain(i))) & named else 0, pos, i)
                         for pos, i in enumerate(items)]
            else:
                rated = [(_item_relevance(i, skill_keys, concepts, groups), pos, i)
                         for pos, i in enumerate(items)]
            needed = 2 if familiar else 1
            for score, _pos, item in rated:
                if score < needed:
                    plan.template_items_removed.append({
                        "section": section.title, "item": f"{kv.label}: {latex_to_plain(item)}",
                        "reason": "not relevant to this job", "location": "list item"})
            kept = sorted((r for r in rated if r[0] >= needed), key=lambda r: (-r[0], r[1]))
            plan.kv_items[(si, li)] = [item for _s, _p, item in kept]
            scores[li] = sum(score for score, _p, _i in kept)
            if kind == "skills":
                skill_rows.append((si, li))
        if kind == "skills":
            plan.kv_order[si] = sorted(range(len(section.kv_lines)),
                                       key=lambda li: (-scores.get(li, 0), li))
    ensure_core_skills(plan, model, master, skill_rows)


def ensure_core_skills(plan: ResumePlan, model: CoverageModel, master: MasterProfile,
                       rows: Sequence[tuple[int, int]]) -> None:
    """Every core skill the career bank evidences ([V]/[R]) appears in the skills section."""
    if not rows:
        return
    shown = {k for row in rows for item in plan.kv_items.get(row, [])
             for k in SKILL_FINDER.keys(latex_to_plain(item))}
    for req in model.requirements:
        if req.type != "core":
            continue
        for term in req.terms:
            for key in (term.members or (term.key,)):
                kind, name = key.split(":", 1)
                spec = SKILL_INDEX.get(name) if kind == "skill" else None
                if spec is None or spec.kind == "category" or name in shown or \
                        name in PRACTICE_KEYS or name in master.global_no or \
                        not _has_global_positive(master, name):
                    continue
                labels = {row: plan.template.sections[row[0]].kv_lines[row[1]].label
                          for row in rows}
                target = next((row for row in rows if spec.group in _label_groups(labels[row])),
                              None) or next((row for row in rows if re.search(
                                  r"backend|programming|language|software", labels[row], re.I)),
                                  rows[0])
                plan.kv_items[target] = [latex_escape(spec.display), *plan.kv_items[target]]
                shown.add(name)
                plan.warnings.append(f"Added core skill '{spec.display}' to "
                                     f"'{labels[target]}' (evidenced in the master profile)")


def _cut_points(text: str) -> list[int]:
    """Clause boundaries outside parentheses; never inside a list ("A, B, and C")."""
    depth = 0
    cuts = []
    for index, char in enumerate(text):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth:
            continue
        for marker in (", ", "; ", " that ", " so ", " after ", " which ", " while ", " by ",
                       " checking ", " cutting ", " fixing ", " routing ", " pulling ",
                       " feeding ", " using ", " with "):
            if not text.startswith(marker, index):
                continue
            following = text[index + len(marker):]
            if marker == ", " and (following[:1].isupper() or following[:1].isdigit()
                                   or re.match(r"(?:and|or)\b", following)
                                   or re.match(r"(?:[\w./+-]+(?: [\w./+-]+){0,2}, )+(?:and|or) ",
                                               following)):
                continue  # "Quality, Supply Chain, and Warehouse" is one list
            if marker == " that " and re.search(r"\w+ing$", text[:index]):
                continue  # "checking that bad output falls back" is one clause
            cuts.append(index)
    return cuts


def _summary_clause(text: str, must_prove: Sequence[str] = (),
                    worth: Sequence[str] = ()) -> str | None:
    """A verbatim prefix of a verified bullet, cut at a clause boundary.

    Among the boundaries within the length limits, the cut must prove one of
    ``must_prove`` and keeps as many ``worth`` terms as possible (shorter wins a
    tie). A cut containing ";" is never used: the summary joins clauses with it.
    """
    low, preferred, hard = SUMMARY_CLAUSE_CHARS
    cuts = _cut_points(text)
    options = [c for c in cuts if low <= c <= preferred] + \
        [c for c in cuts if preferred < c <= hard] + ([len(text)] if len(text) <= hard else [])
    best: tuple[int, int, str] | None = None
    for order, cut in enumerate(options):
        clause = text[:cut].rstrip(" ,;")
        if ";" in clause or (must_prove and not any(_text_proves(k, clause) for k in must_prove)):
            continue
        kept = sum(1 for k in worth if _text_proves(k, clause))
        if best is None or (kept, -order) > (best[0], best[1]):
            best = (kept, -order, clause)
    if best is None:
        return None
    clause = best[2]
    first, _sep, rest = clause.partition(" ")
    if re.fullmatch(r"[A-Z][a-z]+(?:-[a-z]+)?", first):
        clause = first.lower() + (" " + rest if rest else "")
    return clause

def _short_name(name: str, kind: str) -> str:
    acronym = re.search(r"\(([A-Z]{2,})\)", name)
    if acronym:
        return acronym.group(1)
    name = re.sub(r",? (?:Inc|Ltd|LLC|Corp|Co)\.?$", "", name.split(",")[0]).strip()
    return name


def _latex_with_bold_tech(text: str) -> str:
    """latex_escape the sentence and bold vocabulary tech terms only."""
    out, last = [], 0
    for mention in SKILL_FINDER.find(text):
        if SKILL_INDEX[mention.key].kind == "category":
            continue
        out.append(latex_escape(text[last:mention.start]))
        out.append("\\textbf{" + latex_escape(text[mention.start:mention.end]) + "}")
        last = mention.end
    out.append(latex_escape(text[last:]))
    return "".join(out)


SUMMARY_MAX_CHARS = 400   # three lines at the template's summary width
# Identity openers by job focus: (focus term, opener, the evidence each part needs).
# A part is only said when the master profile proves it at project strength or more.
IDENTITIES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("concept:ml_modeling", "Engineer who {verbs} ML systems",
     (("trains", "concept:ml_modeling"), ("evaluates", "concept:model_evaluation"),
      ("ships", "concept:deployment"))),
    ("concept:test_automation", "Engineer who builds CI pipelines and the test gates that keep "
                                "them reliable",
     (("", "concept:ci_pipelines"), ("", "concept:test_automation"))),
    ("concept:deployment", "Engineer who deploys and supports software for client teams",
     (("", "concept:deployment"), ("", "concept:customers"))),
    ("concept:llm_apps", "Engineer who builds LLM applications with evaluations and safe "
                         "fallbacks",
     (("", "concept:llm_apps"), ("", "concept:model_evaluation"))),
)
BANNED_SUMMARY_OPENERS = re.compile(r"^(?:\w+(?: \w+)? experience:|also\b)", re.IGNORECASE)


def _proven(model: CoverageModel, master: MasterProfile, key: str) -> bool:
    proofs = model.profile.get(key)
    if proofs is None:
        proofs, _rejected = profile_proofs(key, master)
    best = _best(proofs)
    return best is not None and best.strength >= STRENGTH_PROJECT


def _identity(model: CoverageModel, master: MasterProfile) -> str:
    focus = model.focus[0] if model.focus else None
    for key, opener, parts in IDENTITIES:
        if key != focus:
            continue
        verbs = [verb for verb, need in parts if _proven(model, master, need)]
        if "{verbs}" in opener and len(verbs) >= 2:
            joined = verbs[0] if len(verbs) == 1 else \
                ", ".join(verbs[:-1]) + (", and " if len(verbs) > 2 else " and ") + verbs[-1]
            return opener.format(verbs=joined)
        if "{verbs}" not in opener and len(verbs) == len(parts):
            return opener
    weights = model.term_weights()
    labels = []
    for key in sorted(weights, key=lambda k: (-weights[k], k)):
        kind, name = key.split(":", 1)
        if kind == "concept" and not CONCEPT_INDEX[name].generic and model.best(key):
            labels.append(CONCEPT_INDEX[name].gap_label or CONCEPT_INDEX[name].display.lower())
        if len(labels) == 3:
            break
    if not labels:
        return "Engineer"
    return "Engineer with hands-on " + (labels[0] if len(labels) == 1 else
                                       ", ".join(labels[:-1]) + " and " + labels[-1]) + \
        " experience"


def _differentiator(plan: ResumePlan) -> str | None:
    """The template's first award, restated as one sentence (verified like any claim)."""
    for si, section in enumerate(plan.template.sections):
        if template_section_kind(section.title) != "awards":
            continue
        for ei, entry in enumerate(section.entries):
            ep = plan.entries.get((si, ei))
            if ep is None or not ep.keep:
                continue
            name, _sep, detail = latex_to_plain(entry.args[0]).partition(" | ")
            parts = [part.strip() for part in detail.split(", ") if part.strip()]
            best = next((part for part in parts if re.search(r"\d", part)), parts[0]
                        if parts else "")  # the most concrete part ("top 5% of ...")
            return f"{name.strip()}: {best}." if best else f"{name.strip()}."
    return None


def generate_summary(plan: ResumePlan, analysis: Analysis) -> None:
    """A 2 to 3 line summary: identity, the strongest mapped proofs, one differentiator.

    Proof clauses are verbatim prefixes of selected, verified bullets, and each
    must prove a term of a high-weight requirement. Every sentence is verified
    against the master profile; anything that fails is dropped, and with nothing
    left the verified template summary stays.
    """
    model = analysis.coverage
    master = analysis.master
    scope = build_scope(master, None)
    weights = model.term_weights()
    high = {t.key for r in model.requirements if r.weight >= WEIGHT_REQUIRED for t in r.terms}
    high |= {m for r in model.requirements if r.weight >= WEIGHT_REQUIRED
             for t in r.terms for m in t.members}
    focus_key = model.focus[0] if model.focus else None
    identity_keys = {focus_key} | {need for key, _o, parts in IDENTITIES if key == focus_key
                                   for _verb, need in parts}
    ranked = []
    for key, ep in plan.entries.items():
        if not ep.keep:
            continue
        for cand in ep.selected:
            targets = sorted((k for k in cand.proves if k in high),
                             key=lambda k: (k not in identity_keys, -weights.get(k, 0), k))
            if cand.parent_index is None and targets and not DEPENDENT_BULLET.match(cand.plain):
                own = [k for k in targets if k in identity_keys]
                score = (sum((weights.get(k, Fraction(0)) * cand.proves[k] for k in own),
                             Fraction(0)),
                         sum((weights.get(k, Fraction(0)) * cand.proves[k] for k in targets),
                             Fraction(0)))
                ranked.append((own, score, key, cand, targets))
    clauses: list[str] = []
    used: dict[tuple[int, int], int] = {}
    shown: set[str] = set()
    while len(clauses) < SUMMARY_PROOFS:  # each proof should add something not yet said
        best = None
        for own, score, key, cand, targets in ranked:
            if used.get(key, 0) >= 2 or id(cand) in used:
                continue
            fresh = [k for k in own if k not in shown]
            rank = (sum((weights.get(k, Fraction(0)) for k in fresh), Fraction(0)),
                    score[0], cand.metric, score[1], -key[0], -key[1], -cand.bullet_index)
            if best is None or rank > best[0]:
                best = (rank, own, key, cand, targets)
        if best is None:
            break
        _rank, own, key, cand, targets = best
        used[id(cand)] = 1
        clause = _summary_clause(cand.plain, own or targets[:2], targets)
        if clause is None or clause in clauses or not verify_claim(clause, master, scope).ok:
            continue
        clauses.append(clause)
        used[key] = used.get(key, 0) + 1
        shown |= {k for k in targets if _text_proves(k, clause)}
    sentences: list[str] = []
    if clauses:
        separator = "; " if any("," in c or ";" in c for c in clauses) else ", "
        body = clauses[0] if len(clauses) == 1 else (
            separator.join(clauses[:-1]) + (separator if len(clauses) > 2 else " ") + "and " +
            clauses[-1])
        identity = _identity(model, master)
        lead = f"{identity}: {body}."
        while len(lead) > SUMMARY_MAX_CHARS - 60 and len(clauses) > 1:
            clauses.pop()
            body = clauses[0] if len(clauses) == 1 else \
                separator.join(clauses[:-1]) + " and " + clauses[-1]
            lead = f"{identity}: {body}."
        if verify_claim(lead, master, scope).ok:
            sentences.append(lead)
    statement = required_statement(model, master)
    if statement and verify_claim(statement, master, scope).ok:
        sentences.append(statement)
    extra = _differentiator(plan)
    if sentences and extra and verify_claim(extra, master, scope).ok and \
            len(" ".join(sentences)) + len(extra) + 1 <= SUMMARY_MAX_CHARS:
        sentences.insert(1, extra)
    sentences = [s for s in sentences if not BANNED_SUMMARY_OPENERS.match(s)]
    if not sentences:
        return  # keep the verified template summary
    plan.summary_sentences = [_latex_with_bold_tech(s) for s in sentences]
    plan.summary_text = "\n    " + " ".join(plan.summary_sentences) + "\n    "
    plan.warnings.append("Summary generated for this job from verified bullets"
                         + (" plus a required statement" if statement else ""))


def required_statement(model: CoverageModel, master: MasterProfile) -> str | None:
    """A true one-line statement for clearance or citizenship requirements the job sets."""
    needs = {h.type for h in model.hard} & {"clearance", "citizenship"}
    if not needs:
        return None
    citizenship, clearance, _city = _master_identity(master)
    parts = []
    if citizenship is not None and model.region.startswith("Canada") and \
            re.search(r"\bcanadian\b", _line_text(citizenship), re.I):
        parts.append("Canadian citizen; no sponsorship needed in Canada")
    if clearance is not None and "clearance" in needs:
        parts.append(excerpt(_line_text(clearance).rstrip("."), 120))
    return ". ".join(parts) + "." if parts else None


def _verified_summary(section: TemplateSection, master: MasterProfile,
                      plan: ResumePlan) -> str | None:
    scope = build_scope(master, None)
    text = section.paragraph_text.strip()
    sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\\])", text) if s.strip()]
    kept: list[str] = []
    for sentence in sentences:
        check = verify_claim(latex_to_plain(sentence), master, scope)
        if check.ok:
            kept.append(sentence.strip())
        else:
            plan.template_items_removed.append({"section": section.title,
                                                "item": latex_to_plain(sentence),
                                                "reason": "; ".join(check.reasons),
                                                "location": "summary sentence"})
    if not kept:
        plan.warnings.append(f"Section '{section.title}' dropped: no sentence could be verified")
        return None
    plan.summary_sentences = kept
    return "\n    " + " ".join(kept) + "\n    "


def _label_groups(label: str) -> set[str]:
    lowered = label.lower()
    groups: set[str] = set()
    if re.search(r"devops|cloud|infra", lowered):
        groups |= {"cloud_devops", "os"}
    if re.search(r"observ|secur|monitor", lowered):
        groups |= {"observability_security"}
    if re.search(r"backend|back-end|programming", lowered):
        groups |= {"backend", "testing"}
    if re.search(r"database|data store", lowered):
        groups |= {"databases"}
    if re.search(r"\bai\b|\bml\b|machine learning", lowered):
        groups |= {"ai_ml"}
    if re.search(r"\bdata\b|analytics", lowered) and "database" not in lowered:
        groups |= {"data"}
    if re.search(r"front", lowered):
        groups |= {"frontend"}
    return groups


def _plan_kv_section(si: int, section: TemplateSection, kind: str, master: MasterProfile,
                     plan: ResumePlan, hits: list[tuple[Requirement, set[str]]],
                     positive_lower: str) -> None:
    hit_keys: set[str] = set()
    for _req, keys in hits:
        hit_keys |= keys
    present: set[str] = set()
    for li, kv in enumerate(section.kv_lines):
        label = kv.label.lower()
        exposure_ok = "familiar" in label or "interest" in label
        kept: list[str] = []
        for item in kv.items:
            plain = latex_to_plain(item)
            reason = _kv_item_problem(plain, master, exposure_ok, positive_lower)
            if reason:
                plan.template_items_removed.append({"section": section.title,
                                                    "item": f"{kv.label}: {plain}",
                                                    "reason": reason, "location": "list item"})
                continue
            kept.append(item)
            present |= set(SKILL_FINDER.keys(plain))
        if kind == "skills":
            kept = [item for _rank, _pos, item in sorted(
                (0 if set(SKILL_FINDER.keys(latex_to_plain(item))) & hit_keys else 1, pos, item)
                for pos, item in enumerate(kept))]
        plan.kv_items[(si, li)] = kept
    if kind != "skills":
        return
    for req, _keys in hits:
        if not req.key:
            continue  # either-or groups are covered by their members
        spec = SKILL_INDEX[req.key]
        if spec.kind != "skill" or spec.key in present or spec.key in PRACTICE_KEYS:
            continue
        for li, kv in enumerate(section.kv_lines):
            if spec.group in _label_groups(kv.label):
                plan.kv_items[(si, li)].append(latex_escape(spec.display))
                present.add(spec.key)
                plan.warnings.append(f"Added matched skill '{spec.display}' to "
                                     f"'{kv.label}' (positive evidence: "
                                     f"{req.evidence[0]['line'] if req.evidence else '?'})")
                break


def _kv_item_problem(plain: str, master: MasterProfile, exposure_ok: bool,
                     positive_lower: str) -> str:
    mentions = SKILL_FINDER.find(plain)
    for mention in mentions:
        key = mention.key
        display = SKILL_INDEX[key].display
        if key in master.global_no:
            return f"'{display}' is marked [NO]"
        if _has_global_positive(master, key):
            continue
        if exposure_ok and any(o.kind == "exposure" for o in master.occurrences.get(key, [])):
            continue
        return (f"'{display}' has no positive evidence" if not exposure_ok
                else f"'{display}' has no positive or exposure-level evidence")
    covered = [(m.start, m.end) for m in mentions]
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+#.-]*", plain):
        if any(s <= match.start() < e for s, e in covered):
            continue
        word = match.group(0).rstrip(".")
        if word.lower() in STOPWORDS or len(word) < 2 or not re.search(r"[A-Z]", word):
            continue
        if not _literal_in(positive_lower, word):
            return f"'{word}' does not appear in positive master evidence"
    if not mentions and not re.search(r"[A-Z]", plain):
        stems = content_stems(plain)
        if stems and not any(_literal_in(positive_lower, w) for w in word_tokens(plain)):
            return "wording does not appear in positive master evidence"
    return ""


# ---------------------------------------------------------------------------
# LaTeX escaping and rendering
# ---------------------------------------------------------------------------
LATEX_SPECIALS = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                  "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                  "^": r"\textasciicircum{}"}
LATEX_UNICODE = {"\u2013": "--", "\u2014": "---", "\u2018": "`", "\u2019": "'", "\u201c": "``",
                 "\u201d": "''", "\u2026": r"\ldots{}", "\u00b5": r"\ensuremath{\mu}",
                 "\u03bc": r"\ensuremath{\mu}", "\u00d7": r"\ensuremath{\times}",
                 "\u2192": r"\ensuremath{\rightarrow}", "\u2265": r"\ensuremath{\geq}",
                 "\u2264": r"\ensuremath{\leq}", "\u00b1": r"\ensuremath{\pm}",
                 "\u00b0": r"\ensuremath{^\circ}", "\u2212": "-", "\u00a0": "~"}


def latex_escape(text: str, engine: str = "pdflatex") -> str:
    """Escape text inserted into the template (never applied to template LaTeX)."""
    out: list[str] = []
    for char in unicodedata.normalize("NFC", text):
        if char in LATEX_SPECIALS:
            out.append(LATEX_SPECIALS[char])
        elif char in LATEX_UNICODE:
            out.append(LATEX_UNICODE[char])
        elif ord(char) < 128 or engine != "pdflatex":
            out.append(char)
        elif ord(char) < 256 and unicodedata.category(char).startswith("L"):
            out.append(char)  # Latin-1 letters are handled by inputenc utf8
        else:
            ascii_form = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode()
            out.append(latex_escape(ascii_form, engine) if ascii_form else "?")
    return "".join(out)


def spacing_block(level: int) -> str:
    if level <= 0:
        return ""
    settings = SPACING_LEVELS[level]
    return ("% --- jobauto: bounded spacing level " f"{level}" " (fonts and margins unchanged) ---\n"
            f"\\titlespacing*{{\\section}}{settings['section']}\n"
            f"\\setlist[itemize,2]{{{settings['list']}}}\n")


def follows_its_parent(cand: Candidate, before: Sequence[Candidate],
                       by_index: dict[int, Candidate]) -> bool:
    """A dependent bullet sits right after its parent, or after another bullet of the
    parent's block (S09 after S08, both kept with S07)."""
    if not before:
        return False
    previous = before[-1]
    if previous.bullet_index == cand.parent_index:
        return True
    chain = _ancestors(previous, by_index)
    return any(a.bullet_index == cand.parent_index for a in chain)


def active_model(plan: ResumePlan, removed: set[tuple]) -> list[tuple[int, list[tuple[int, list[Candidate]]]]]:
    """Sections (in template order) with their kept entries and bullets."""
    model = []
    for si in plan.section_order:
        section = plan.template.sections[si]
        if section.kind != "list":
            model.append((si, []))
            continue
        entries = []
        for ei in plan.entry_order.get(si, []):
            if ("entry", si, ei) in removed:
                continue
            ep = plan.entries[(si, ei)]
            bullets: list[Candidate] = []
            by_index = {c.bullet_index: c for c in ep.candidates}
            for c in ep.selected:
                if ("bullet", si, ei, c.bullet_index) in removed:
                    continue
                if c.parent_index is not None and not follows_its_parent(c, bullets, by_index):
                    continue  # its parent was trimmed: "Drove it ..." would read wrong
                bullets.append(c)
            entries.append((ei, bullets))
        if entries:
            model.append((si, entries))
    return model


def render_resume(plan: ResumePlan, removed: set[tuple], spacing_level: int) -> str:
    template = plan.template
    parts = [template.preamble.rstrip("\n") + "\n", spacing_block(spacing_level),
             "\n\\begin{document}", template.header]
    for si, entries in active_model(plan, removed):
        section = template.sections[si]
        if section.kind == "paragraph":
            if plan.summary_text is None:
                continue
            parts.append(section.paragraph_wrapper[0] + plan.summary_text
                         + section.paragraph_wrapper[1])
        elif section.kind == "keyvalue":
            lines = []
            for li in plan.kv_order.get(si, range(len(section.kv_lines))):
                kv = section.kv_lines[li]
                items = plan.kv_items.get((si, li), [])
                if items:
                    lines.append(f"\\textbf{{{kv.label_raw}}}{{: {', '.join(items)}}}")
            if not lines:
                continue
            parts.append(section.kv_wrapper[0] + section.prefix.join(lines)
                         + section.kv_wrapper[1])
        else:
            body = [section.prefix]
            for ei, bullets in entries:
                entry = section.entries[ei]
                header = plan.entries[(si, ei)].header_raw or entry.header_raw
                body.append("\n\n    " + header.strip())
                if bullets:
                    body.append("\n        \\resumeItemListStart\n")
                    body.extend(f"            \\resumeItem{{{c.raw}}}\n" for c in bullets)
                    body.append("        \\resumeItemListEnd")
            body.append("\n\n" + section.suffix)
            parts.append("".join(body))
    parts.append(template.trailer)
    return "".join(parts)


def expected_content(plan: ResumePlan, removed: set[tuple]) -> list[str]:
    """Strings that must survive into the PDF text (checked alnum-normalized)."""
    expected: list[str] = []
    name = re.search(r"\\scshape\s+([^}\\]+)", plan.template.header)
    if name:
        expected.append(collapse_ws(name.group(1)))
    for si, entries in active_model(plan, removed):
        section = plan.template.sections[si]
        if section.kind == "paragraph" and plan.summary_text is None:
            continue
        expected.append(section.title)
        for ei, bullets in entries:
            entry = section.entries[ei]
            expected.append(latex_to_plain(entry.args[0])[:40])
            for cand in bullets:
                expected.append(" ".join(cand.plain.split()[:5]))
    return [e for e in expected if e]


# ---------------------------------------------------------------------------
# Compilation and single-page verification
# ---------------------------------------------------------------------------
@dataclass
class ToolSet:
    engine_name: str | None
    engine_path: str | None
    pdftotext: str | None
    pdfinfo: str | None
    required_engine: str
    engine_reason: str

    def describe(self) -> dict[str, Any]:
        return {"required_engine": self.required_engine, "engine_reason": self.engine_reason,
                "engine": self.engine_name, "engine_path": self.engine_path,
                "pdftotext": self.pdftotext, "pdfinfo": self.pdfinfo}


@dataclass
class CompileResult:
    ok: bool
    returncode: int | None
    pdf_path: Path
    log_path: Path
    log_text: str
    error: str


@dataclass
class PdfCheck:
    ok: bool
    pages: int | None
    pdfinfo_pages: int | None
    pdftotext_pages: int | None
    method: str
    reasons: list[str]
    missing_content: list[str]
    limitations: list[str]


def detect_tools(template: ParsedTemplate,
                 which: Callable[[str], str | None] = shutil.which) -> ToolSet:
    """Find a compiler compatible with the template, plus PDF inspection tools."""
    required = template.engine_requirement
    candidates = ("pdflatex",) if required == "pdflatex" else ("xelatex", "lualatex")
    engine_name = engine_path = None
    for name in candidates:
        found = which(name)
        if found:
            engine_name, engine_path = name, found
            break
    return ToolSet(engine_name=engine_name, engine_path=engine_path,
                   pdftotext=which("pdftotext"), pdfinfo=which("pdfinfo"),
                   required_engine=required, engine_reason=template.engine_reason)


def compile_tex(tex: str, build_dir: Path, jobname: str, tools: ToolSet, texinputs: Path,
                runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                timeout: int = COMPILE_TIMEOUT_SECONDS) -> CompileResult:
    """Compile in an isolated build directory; argument lists only, no shell."""
    if not tools.engine_path:
        raise MissingPrerequisiteError(f"No compatible LaTeX engine ({tools.required_engine}) "
                                       f"is installed")
    build_dir.mkdir(parents=True, exist_ok=True)
    tex_path = build_dir / f"{jobname}.tex"
    pdf_path = build_dir / f"{jobname}.pdf"
    log_path = build_dir / f"{jobname}.log"
    for stale in (pdf_path, log_path):
        if stale.exists():
            stale.unlink()
    tex_path.write_text(tex, encoding="utf-8")
    env = dict(os.environ)
    env["TEXINPUTS"] = f"{texinputs}{os.pathsep}"
    env["openout_any"] = "p"
    command = [tools.engine_path, "-interaction=nonstopmode", "-halt-on-error",
               "-file-line-error", "-no-shell-escape", f"-output-directory={build_dir}",
               f"-jobname={jobname}", str(tex_path)]
    try:
        completed = runner(command, cwd=str(build_dir), env=env, capture_output=True,
                           text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        return CompileResult(False, None, pdf_path, log_path, log,
                             f"{tools.engine_name} timed out after {timeout}s")
    except OSError as exc:
        return CompileResult(False, None, pdf_path, log_path, "",
                             f"could not run {tools.engine_name}: {exc}")
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    if completed.returncode != 0:
        errors = [l for l in log.splitlines() if l.startswith("!") or ":error" in l.lower()
                  or re.match(r"^\S+\.tex:\d+:", l)]
        detail = errors[0] if errors else (completed.stdout or "")[-300:]
        return CompileResult(False, completed.returncode, pdf_path, log_path, log,
                             f"{tools.engine_name} exited with {completed.returncode}: "
                             f"{collapse_ws(detail)[:300]}")
    fatal = fatal_log_lines(log)
    if fatal:
        return CompileResult(False, completed.returncode, pdf_path, log_path, log,
                             f"{tools.engine_name} exited 0 but logged a fatal error: "
                             f"{collapse_ws(fatal[0])[:300]}")
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        return CompileResult(False, completed.returncode, pdf_path, log_path, log,
                             "compiler reported success but produced no PDF")
    return CompileResult(True, completed.returncode, pdf_path, log_path, log, "")


FATAL_LOG_LINE = re.compile(r"^(?:! |\S+\.tex:\d+: )|Emergency stop|Fatal error occurred|"
                            r"LaTeX Error:")


def fatal_log_lines(log: str) -> list[str]:
    """Error lines of a TeX log; any one fails Gate 1 even when the exit code is 0."""
    return [line for line in log.splitlines() if FATAL_LOG_LINE.search(line)]


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", text).lower())


def verify_pdf(pdf_path: Path, tools: ToolSet, expected: Sequence[str],
               runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> PdfCheck:
    """Page count via pdfinfo, text and page boundaries via pdftotext."""
    reasons: list[str] = []
    limitations: list[str] = []
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        return PdfCheck(False, None, None, None, "none", ["PDF is missing or empty"], [], [])
    info_pages: int | None = None
    if tools.pdfinfo:
        try:
            result = runner([tools.pdfinfo, str(pdf_path)], capture_output=True, text=True,
                            timeout=TOOL_TIMEOUT_SECONDS, check=False)
            match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout or "", re.MULTILINE)
            if result.returncode == 0 and match:
                info_pages = int(match.group(1))
            else:
                reasons.append(f"pdfinfo could not read the page count (exit {result.returncode})")
        except (subprocess.TimeoutExpired, OSError) as exc:
            reasons.append(f"pdfinfo failed: {exc}")
    else:
        limitations.append("pdfinfo unavailable: page count relies on pdftotext page breaks only")
    text_pages: int | None = None
    missing: list[str] = []
    if tools.pdftotext:
        try:
            result = runner([tools.pdftotext, "-enc", "UTF-8", str(pdf_path), "-"],
                            capture_output=True, text=True, timeout=TOOL_TIMEOUT_SECONDS,
                            check=False)
            if result.returncode != 0:
                reasons.append(f"pdftotext failed (exit {result.returncode})")
            else:
                text = result.stdout or ""
                pages = text.split("\f")
                if pages and not pages[-1].strip():
                    pages = pages[:-1]  # pdftotext ends every page with a form feed
                text_pages = len(pages)
                if not text.strip():
                    reasons.append("pdftotext extracted no text")
                normalized = _alnum(text)
                missing = [item for item in expected if _alnum(item)
                           and _alnum(item) not in normalized]
                if missing:
                    reasons.append(f"{len(missing)} expected text fragments are missing from "
                                   f"the PDF")
        except (subprocess.TimeoutExpired, OSError) as exc:
            reasons.append(f"pdftotext failed: {exc}")
    else:
        reasons.append("pdftotext unavailable: text extraction and page-boundary checks "
                       "were not performed")
    if info_pages is not None and text_pages is not None and info_pages != text_pages:
        reasons.append(f"inconsistent page counts: pdfinfo={info_pages}, "
                       f"pdftotext={text_pages}")
    pages = info_pages if info_pages is not None else text_pages
    method = "+".join(name for name, value in (("pdfinfo", info_pages),
                                               ("pdftotext", text_pages)) if value is not None)
    if pages is None:
        reasons.append("page count could not be determined")
    elif pages != 1:
        reasons.append(f"PDF has {pages} pages, not exactly 1")
    return PdfCheck(ok=not reasons, pages=pages, pdfinfo_pages=info_pages,
                    pdftotext_pages=text_pages, method=method or "none", reasons=reasons,
                    missing_content=missing[:20], limitations=limitations)


# ---------------------------------------------------------------------------
# Gate 3: ATS machine readability of the text extracted from the PDF
# ---------------------------------------------------------------------------
CORE_ATS_SECTIONS = ("education", "experience", "skills", "projects")
LATEX_ARTIFACT = re.compile(r"\\[A-Za-z]+|\\[&%$#_{}~^]|\$\|\$|textbackslash|textasciitilde|"
                            r"�|[\x00-\x08\x0b\x0e-\x1f]")
METRIC_TOKEN = re.compile(r"(?<![A-Za-z0-9.,])[$~]?\d+(?:[,.:]\d+)*"
                          r"(?:%|\+|[KMBkx]\b|(?:am|pm|ms|s)\b|(?![A-Za-z0-9]))")


@dataclass
class AtsCheck:
    ok: bool
    section_order: list[str]
    out_of_order_sections: list[str]
    missing_sections: list[str]
    core_sections: dict[str, bool]
    terms_checked: int
    fragmented_terms: list[str]
    latex_artifacts: list[str]
    metrics_checked: int
    missing_metrics: list[str]
    reasons: list[str]


def _ats_normalize(text: str) -> str:
    return collapse_ws(normalize_text(text)).casefold()


def rendered_section_titles(plan: ResumePlan, removed: set[tuple]) -> list[str]:
    """Section titles in the order render_resume emits them."""
    titles = []
    for si, _entries in active_model(plan, removed):
        section = plan.template.sections[si]
        if section.kind == "paragraph" and plan.summary_text is None:
            continue
        if section.kind == "keyvalue" and not any(plan.kv_items.get((si, li))
                                                   for li in range(len(section.kv_lines))):
            continue
        titles.append(section.title)
    return titles


def ats_terms_and_metrics(plan: ResumePlan, removed: set[tuple]) -> tuple[list[str], list[str]]:
    """Vocabulary terms in the rendered body and the metric tokens of kept bullets."""
    body = render_resume(plan, removed, 0).split("\\begin{document}", 1)[1]
    body = body.replace(plan.template.header, "", 1)
    terms: list[str] = []
    for mention in SKILL_FINDER.find(latex_to_plain(body)):
        if mention.text not in terms:
            terms.append(mention.text)
    metrics: list[str] = []
    for _si, entries in active_model(plan, removed):
        for _ei, bullets in entries:
            for cand in bullets:
                metrics += [t for t in _metric_tokens(cand.plain) if t not in metrics]
    return terms, metrics


def check_ats_text(text: str, titles: Sequence[str], terms: Sequence[str],
                   metrics: Sequence[str]) -> AtsCheck:
    """Reading order of section headers, intact technical terms, no LaTeX residue.

    Headers are matched as standalone lines first (real pdftotext output), then
    as text after the previous header (single-line extractions).
    """
    reasons: list[str] = []
    normalized_lines = [_ats_normalize(line) for line in text.split("\n")]
    joined = "\n".join(normalized_lines)
    cursor = 0
    found: list[str] = []
    out_of_order: list[str] = []
    missing_sections: list[str] = []
    for title in titles:
        wanted = _ats_normalize(title)
        match = re.compile(rf"(?m)^{re.escape(wanted)}$").search(joined, cursor)
        position = match.start() if match else joined.find(wanted, cursor)
        if position < 0:
            (out_of_order if wanted in joined else missing_sections).append(title)
            continue
        found.append(title)
        cursor = position + len(wanted)
    if out_of_order:
        reasons.append(f"section headers out of reading order: {', '.join(out_of_order)}")
    if missing_sections:
        reasons.append(f"section headers missing from the text layer: "
                       f"{', '.join(missing_sections)}")
    flat = _ats_normalize(text)
    fragmented = [term for term in terms if _ats_normalize(term) not in flat]
    if fragmented:
        reasons.append(f"technical terms fragmented or corrupted in the text layer: "
                       f"{', '.join(fragmented[:10])}")
    artifacts = sorted({m.group(0) for m in LATEX_ARTIFACT.finditer(text)})
    if artifacts:
        reasons.append(f"LaTeX residue in the text layer: {', '.join(map(repr, artifacts[:10]))}")
    lost = [metric for metric in metrics if _ats_normalize(metric) not in flat]
    if lost:
        reasons.append(f"metrics lost or altered in the text layer: {', '.join(lost[:10])}")
    kinds = {template_section_kind(title) for title in titles}
    return AtsCheck(ok=not reasons, section_order=found, out_of_order_sections=out_of_order,
                    missing_sections=missing_sections,
                    core_sections={kind: kind in kinds for kind in CORE_ATS_SECTIONS},
                    terms_checked=len(terms), fragmented_terms=fragmented[:20],
                    latex_artifacts=artifacts[:20], metrics_checked=len(metrics),
                    missing_metrics=lost[:20], reasons=reasons)


def ats_readability_check(pdf_path: Path, tools: ToolSet, plan: ResumePlan, removed: set[tuple],
                          runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
                          ) -> AtsCheck:
    titles = rendered_section_titles(plan, removed)
    terms, metrics = ats_terms_and_metrics(plan, removed)
    failure = AtsCheck(False, [], [], list(titles), {}, len(terms), [], [], len(metrics), [], [])
    if not tools.pdftotext:
        failure.reasons.append("pdftotext unavailable: the text layer could not be checked")
        return failure
    try:
        result = runner([tools.pdftotext, "-enc", "UTF-8", str(pdf_path), "-"],
                        capture_output=True, text=True, timeout=TOOL_TIMEOUT_SECONDS,
                        check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        failure.reasons.append(f"pdftotext failed: {exc}")
        return failure
    if result.returncode != 0:
        failure.reasons.append(f"pdftotext failed (exit {result.returncode})")
        return failure
    return check_ats_text(result.stdout or "", titles, terms, metrics)


def kit_trim_sequence(plan: ResumePlan) -> list[tuple[tuple, str]]:
    """Kit mode: what goes first when the page overflows (9.7 keeps roles and top projects).

    1. leadership and research entries; 2. bullets above each entry's floor, lowest value
    first; 3. projects ranked below the top 2; 4. bullets of the top projects down to one;
    5. as a last resort, roles from the oldest, never the most recent one.
    """
    units: list[tuple[tuple, str]] = []
    kinds = {si: template_section_kind(sec.title) for si, sec in enumerate(plan.template.sections)}
    live = [(si, ei) for si, order in plan.entry_order.items() for ei in order
            if plan.entries[(si, ei)].keep and kinds[si] in KIT_SECTIONS]
    for kind in ("leadership", "research"):
        for key in sorted((k for k in live if kinds[k[0]] == kind),
                          key=lambda k: (plan.entries[k].worth(), -k[1])):
            units.append((("entry", *key), f"{kind} is included only when space allows"))
    above = []
    protected: dict[tuple[int, int], set[int]] = {}
    for key in live:
        ep = plan.entries[key]
        if kinds[key[0]] in ("experience", "projects"):
            protected[key] = _protected_bullets(ep, kinds[key[0]])
            for rank, cand in enumerate(ep.selected):
                if cand.bullet_index not in protected[key]:
                    above.append(((cand.worth(), cand.impact, kinds[key[0]] == "experience",
                                   -rank), ("bullet", *key, cand.bullet_index)))
    units += [(unit, f"{SPARE_REASON}: bullet above its entry floor, the least page coverage "
                     f"lost per line") for _r, unit in sorted(above)]
    projects = sorted((k for k in live if kinds[k[0]] == "projects"),
                      key=lambda k: -plan.project_ranks.get(k, 99))
    roles = sorted((k for k in live if kinds[k[0]] == "experience"),
                   key=lambda k: _entry_dates(plan.template.sections[k[0]].entries[k[1]]))
    spare = [k for k in projects if plan.project_ranks.get(k, 99) >= TOP_PROJECTS] + roles[:-1]

    def per_line(key: tuple[int, int]) -> float:  # a role header takes 2 lines, a project 1
        lines = (2 if kinds[key[0]] == "experience" else 1) + len(plan.entries[key].selected)
        return plan.entries[key].worth() / lines

    for key in sorted(spare, key=lambda k: (per_line(k), _entry_dates(
            plan.template.sections[k[0]].entries[k[1]]))):
        units.append((("entry", *key), f"{SPARE_REASON}: "
                      + ("project ranked below the top 2" if kinds[key[0]] == "projects" else
                         "older role") + ", the least page coverage lost per line"))
    for key in projects:
        ep = plan.entries[key]
        if plan.project_ranks.get(key, 99) < TOP_PROJECTS:
            keep = sorted((c for c in ep.selected if c.bullet_index in protected[key]),
                          key=lambda c: (c.impact, c.worth()))
            for cand in keep[:-1]:
                units.append((("bullet", *key, cand.bullet_index),
                              "top project trimmed toward one bullet"))
    return units


def _protected_bullets(ep: EntryPlan, kind: str) -> set[int]:
    """The floor: an entry's most valuable bullets (in a project, the highest-impact ones),
    with every bullet they depend on."""
    by_index = {c.bullet_index: c for c in ep.candidates}
    rank = (lambda c: (c.impact, c.worth())) if kind == "projects" else \
        (lambda c: (c.worth(), c.impact))
    kept: set[int] = set()
    for cand in sorted(ep.selected, key=rank, reverse=True):
        if len(kept) >= ep.floor:
            break
        kept |= {a.bullet_index for a in _ancestors(cand, by_index)} | {cand.bullet_index}
    return kept


# ---------------------------------------------------------------------------
# Step 6: wrap probe (before selection) and layout checks (after compiling)
# ---------------------------------------------------------------------------
PROBE_MARK = re.compile(r"JOBAUTO-W:([A-Za-z0-9_.-]+):([\d.]+)pt:([\d.]+)pt:(\d+)")
PROBE_MACRO = ("\\newcommand{\\jobautomeasure}[2]{\\begingroup\\hbadness=10000"
               "\\hfuzz=\\maxdimen\\setbox0\\hbox{\\small #2}\\setbox2\\hbox to\\linewidth"
               "{\\small #2}\\typeout{JOBAUTO-W:#1:\\the\\wd0:\\the\\linewidth:"
               "\\the\\badness}\\endgroup}")
# Ragged right: one line shrunk with badness b costs (10 + b)^2 demerits against about
# 200 for breaking early into two lines, so TeX keeps a too-wide line only when b <= 4.
MAX_SHRINK_BADNESS = 4
MM = 72 / 25.4                       # points per millimetre
BIG_GAP_MM = 8.0                     # a blank band taller than this inside the page is a gap
SECTION_GAP_MM = 6.0                 # gap above a section title: "a couple of millimetres"
PAGE_FILL_MM = 25.0                  # unused space at the bottom of a page budgeted as full
OVERLAP_SHARE = 0.35                 # boxes of adjacent lines touch; real overlap goes deeper


def make_wrap_probe(template: ParsedTemplate, tools: ToolSet, build_dir: Path, jobname: str,
                    runner: Callable[..., subprocess.CompletedProcess]
                    ) -> Callable[[Sequence[tuple[str, str, str]]], dict[str, int]]:
    """A measuring function: (id, LaTeX, kind) items -> lines each takes in the template.

    One compile sets every bullet in the template's own nested item list and every skills
    row in the skills list and logs its natural width, the line width there and the badness
    of squeezing it into that width. The template is ragged right, where TeX would rather
    break a line than shrink its spaces much, so a wider line stays on one line only when
    the squeeze is tiny (MAX_SHRINK_BADNESS). Kit bullets marked *new* go first. Without a
    compiler, or when the log has no readings, nothing is known.
    """
    skills = next((s for s in template.sections if s.kind == "keyvalue" and
                   template_section_kind(s.title) == "skills"), None)
    opening = re.search(r"\\begin\{itemize\}(?:\[[^\]]*\])?", skills.kv_wrapper[0]) \
        if skills is not None else None
    row_list = opening.group(0) if opening else "\\begin{itemize}[leftmargin=0.15in, label={}]"

    def measure(items: Sequence[tuple[str, str, str]]) -> dict[str, int]:
        if not tools.engine_path or not items:
            return {}
        body = ["\\section{Probe}", "\\resumeSubHeadingListStart",
                "\\resumeProjectHeading{\\textbf{Probe}}{}", "\\resumeItemListStart"]
        body += [f"\\item \\jobautomeasure{{{ident}}}{{{latex}}}"
                 for ident, latex, kind in items if kind == "bullet"] or ["\\item probe"]
        body += ["\\resumeItemListEnd", "\\resumeSubHeadingListEnd", row_list]
        body += [f"\\item \\jobautomeasure{{{ident}}}{{{latex}}}"
                 for ident, latex, kind in items if kind == "row"] or ["\\item probe"]
        body.append("\\end{itemize}")
        tex = template.preamble.rstrip("\n") + "\n" + PROBE_MACRO + "\n\\begin{document}\n" + \
            "\n".join(body) + "\n\\end{document}\n"
        result = compile_tex(tex, build_dir / "probe", f"{jobname}_probe", tools,
                             template.path.parent.resolve(), runner)
        return {m.group(1): 1 if float(m.group(2)) <= float(m.group(3)) or
                int(m.group(4)) <= MAX_SHRINK_BADNESS else 2
                for m in PROBE_MARK.finditer(result.log_text or "")}

    return measure


@dataclass
class LayoutCheck:
    ok: bool
    wrapped: list[str]
    overlaps: list[str]
    big_gaps: list[str]
    section_gaps_mm: dict[str, float]
    bottom_space_mm: float | None
    lines: int
    reasons: list[str]
    warnings: list[str]
    limitations: list[str]


def _layout_lines(xhtml: str) -> list[tuple[int, float, float, float, float, str]]:
    """(page, yMin, yMax, xMin, xMax, text) for every line of `pdftotext -bbox-layout`."""
    import xml.etree.ElementTree as ElementTree
    cleaned = re.sub(r"<!DOCTYPE[^>]*>", "", xhtml)
    root = ElementTree.fromstring(cleaned)
    found = []
    for page_no, page in enumerate(root.iter("{http://www.w3.org/1999/xhtml}page"), 1):
        for line in page.iter("{http://www.w3.org/1999/xhtml}line"):
            words = [w.text or "" for w in line.iter("{http://www.w3.org/1999/xhtml}word")]
            found.append((page_no, float(line.get("yMin")), float(line.get("yMax")),
                          float(line.get("xMin")), float(line.get("xMax")), " ".join(words)))
    return found


def layout_check(pdf_path: Path, tools: ToolSet, plan: ResumePlan, removed: set[tuple],
                 runner: Callable[..., subprocess.CompletedProcess], page_budget: int = 1
                 ) -> LayoutCheck:
    """One line per bullet and skills row, no overlapping text, no big blank bands.

    Wraps come from `pdftotext -layout` (each physical line is one text line); overlaps and
    gaps from the line boxes of `pdftotext -bbox-layout`.
    """
    check = LayoutCheck(True, [], [], [], {}, None, 0, [], [], [])
    if not tools.pdftotext:
        check.limitations.append("pdftotext unavailable: layout not checked")
        return check
    try:
        text = runner([tools.pdftotext, "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
                      capture_output=True, text=True, timeout=TOOL_TIMEOUT_SECONDS,
                      check=False).stdout or ""
        boxes = runner([tools.pdftotext, "-bbox-layout", "-enc", "UTF-8", str(pdf_path), "-"],
                       capture_output=True, text=True, timeout=TOOL_TIMEOUT_SECONDS,
                       check=False).stdout or ""
    except (subprocess.TimeoutExpired, OSError) as exc:
        check.limitations.append(f"pdftotext failed: {exc}")
        return check
    physical = [_alnum(line) for line in text.replace("\f", "\n").split("\n")]

    def one_line(item: str) -> bool:
        wanted = _alnum(item)
        return not wanted or any(wanted in line for line in physical)

    for si, entries in active_model(plan, removed):
        section = plan.template.sections[si]
        if section.kind == "keyvalue" and template_section_kind(section.title) == "skills":
            for li in plan.kv_order.get(si, range(len(section.kv_lines))):
                items = plan.kv_items.get((si, li), [])
                row = f"{section.kv_lines[li].label}: " + ", ".join(latex_to_plain(i)
                                                                     for i in items)
                if items and not one_line(row):  # only its job items left, which never shrink
                    check.warnings.append(f"skills row wraps: {excerpt(row, 60)}")
        for _ei, bullets in entries:
            check.wrapped += [c.plain for c in bullets if not one_line(c.plain)]
    try:
        lines = _layout_lines(boxes) if boxes.strip() else []
    except Exception as exc:  # malformed XHTML from an unusual PDF: report, never crash
        lines = []
        check.limitations.append(f"line boxes unreadable: {type(exc).__name__}")
    check.lines = len(lines)
    titles = {_alnum(t) for t in rendered_section_titles(plan, removed)}
    for page in sorted({l[0] for l in lines}):
        on_page = sorted((l for l in lines if l[0] == page), key=lambda l: (l[1], l[3]))
        for i, a in enumerate(on_page):
            for b in on_page[i + 1:]:
                if b[1] >= a[2]:
                    break
                y_overlap = min(a[2], b[2]) - max(a[1], b[1])
                x_overlap = min(a[4], b[4]) - max(a[3], b[3])
                if y_overlap > OVERLAP_SHARE * min(a[2] - a[1], b[2] - b[1]) and x_overlap > 1:
                    check.overlaps.append(f"'{excerpt(a[5], 40)}' / '{excerpt(b[5], 40)}'")
        bottom = None
        for line in on_page:
            if bottom is not None:
                gap = (line[1] - bottom) / MM
                if gap > BIG_GAP_MM:
                    check.big_gaps.append(f"{gap:.1f} mm before '{excerpt(line[5], 40)}'")
                if _alnum(line[5]) in titles:
                    check.section_gaps_mm[line[5]] = round(max(gap, 0.0), 1)
            bottom = line[2] if bottom is None else max(bottom, line[2])
        if bottom is not None and page == page_budget:
            height = 792.0
            match = re.search(r'<page width="[\d.]+" height="([\d.]+)"', boxes)
            if match:
                height = float(match.group(1))
            check.bottom_space_mm = round((height - bottom) / MM, 1)
    wide = {t: g for t, g in check.section_gaps_mm.items() if g > SECTION_GAP_MM}
    if check.wrapped:
        check.reasons.append(f"{len(check.wrapped)} line(s) wrap: "
                             + "; ".join(excerpt(w, 60) for w in check.wrapped[:3]))
    if check.overlaps:
        check.reasons.append(f"overlapping text: {check.overlaps[0]}")
    if check.big_gaps:
        check.reasons.append(f"blank gap: {check.big_gaps[0]}")
    if wide:
        check.warnings.append("section gaps above " + f"{SECTION_GAP_MM:.0f} mm: " +
                              ", ".join(f"{t} {g} mm" for t, g in wide.items()))
    if check.bottom_space_mm is not None and check.bottom_space_mm > PAGE_FILL_MM:
        check.warnings.append(f"{check.bottom_space_mm} mm unused at the bottom of page "
                              f"{page_budget}")
    check.ok = not check.reasons
    return check


def trim_sequence(plan: ResumePlan) -> list[tuple[tuple, str]]:
    """Deterministic order in which content may be removed to fit one page.

    Optional entries that evidence no matched JD requirement go first. After
    that, bullets above their entry floor and whole optional entries share one
    queue ordered by value (bullet relevance, or an entry's best bullet), so a
    weak project is dropped before a strong bullet of a relevant job. The most
    relevant entry of each optional section is never removed.
    """
    units: list[tuple[tuple, str]] = []
    position = {(si, ei): pos for si, order in plan.entry_order.items()
                for pos, ei in enumerate(order)}

    def priority(si: int) -> int:
        return SECTION_PRIORITY.get(template_section_kind(plan.template.sections[si].title), 9)

    optional = [(si, ei) for (si, ei), ep in plan.entries.items()
                if ep.keep and ep.optional and (si, ei) in position]
    irrelevant = sorted((k for k in optional if plan.entries[k].worth() == 0),
                        key=lambda k: (priority(k[0]), -position[k]))
    for si, ei in irrelevant:
        units.append((("entry", si, ei), "optional entry with no matched JD requirement"))
    scheduled = set(irrelevant)
    protected: set[tuple[int, int]] = set()
    for si in sorted({k[0] for k in optional}):
        remaining = [k for k in optional if k[0] == si and k not in scheduled]
        if remaining:
            protected.add(min(remaining, key=lambda k: (-plan.entries[k].worth(),
                                                         position[k])))
    queue: list[tuple[tuple, tuple, str]] = []
    for (si, ei), ep in plan.entries.items():
        if not ep.keep or (si, ei) in scheduled or (si, ei) not in position:
            continue
        kind = template_section_kind(plan.template.sections[si].title)
        if kind in ("education", "awards"):
            continue
        for rank, cand in enumerate(ep.selected):
            if rank >= ep.floor:
                queue.append(((cand.worth(), 0, priority(si), -position[(si, ei)], -rank),
                              ("bullet", si, ei, cand.bullet_index),
                              "lowest-value bullet above its entry floor"))
        if ep.optional and (si, ei) not in protected and ep.selected:
            best = max(c.worth() for c in ep.selected)
            queue.append(((best, 1, priority(si), -position[(si, ei)], 0), ("entry", si, ei),
                          "lowest-value optional entry"))
    for _key, unit, why in sorted(queue):
        units.append((unit, why))
    return units


@dataclass
class FitResult:
    ok: bool
    tex: str
    removed: list[dict[str, Any]]
    spacing_level: int
    iterations: int
    check: PdfCheck | None
    compile_result: CompileResult | None
    diagnostics: list[str]
    final_removed: set[tuple] = field(default_factory=set)
    restored: list[str] = field(default_factory=list)
    swapped: list[str] = field(default_factory=list)   # bullets removed because they wrapped


def _unit_is_noop(unit: tuple, removed: set[tuple], plan: ResumePlan | None = None) -> bool:
    """Removing it changes nothing: its entry, or the bullet it depends on, is gone."""
    if unit[0] != "bullet":
        return False
    if ("entry", unit[1], unit[2]) in removed:
        return True
    if plan is not None:
        ep = plan.entries[(unit[1], unit[2])]
        cand = next((c for c in ep.selected if c.bullet_index == unit[3]), None)
        while cand is not None and cand.parent_index is not None:
            if ("bullet", unit[1], unit[2], cand.parent_index) in removed:
                return True
            cand = next((c for c in ep.selected if c.bullet_index == cand.parent_index), None)
    return False


def _attempt(plan: ResumePlan, removed: set[tuple], spacing: int, tools: ToolSet,
             build_dir: Path, jobname: str, texinputs: Path,
             runner: Callable[..., subprocess.CompletedProcess]
             ) -> tuple[str, CompileResult, PdfCheck | None]:
    tex = render_resume(plan, removed, spacing)
    result = compile_tex(tex, build_dir, jobname, tools, texinputs, runner)
    if not result.ok:
        return tex, result, None
    return tex, result, verify_pdf(result.pdf_path, tools, expected_content(plan, removed),
                                   runner)


SPARE_REASON = "spare entry"


def fit_to_one_page(plan: ResumePlan, tools: ToolSet, build_dir: Path, jobname: str,
                    texinputs: Path,
                    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                    loss: Callable[[set[tuple], tuple], float] | None = None) -> FitResult:
    """Compile; trim (then tighten spacing) until one page; then refill.

    Every decision uses the real compiled page count. Trimming removes up to
    BULLETS_REMOVED_PER_ITERATION units per compile. Because that can
    overshoot, the refill pass tries to restore the most valuable removed units
    one at a time (at most REFILL_ATTEMPTS compiles) and keeps each restore that
    still verifies as one page.
    """
    sequence = kit_trim_sequence(plan) if plan.kit_mode else trim_sequence(plan)
    removed: set[tuple] = set()
    swapped: list[str] = []
    order: list[tuple] = []
    log: list[dict[str, Any]] = []
    applied = 0
    spacing = 0
    iterations = 0
    check: PdfCheck | None = None
    result: CompileResult | None = None
    tex = ""
    while iterations < MAX_FIT_ITERATIONS:
        iterations += 1
        tex, result, check = _attempt(plan, removed, spacing, tools, build_dir, jobname,
                                      texinputs, runner)
        if check is None:
            return FitResult(False, tex, log, spacing, iterations, None, result,
                             [f"compilation failed: {result.error}"], set(removed))
        if check.ok:
            wrapped = _wrapped_units(plan, removed, layout_check(
                result.pdf_path, tools, plan, removed, runner).wrapped)
            if not wrapped:
                break
            for unit in wrapped:  # swap out a bullet that takes two lines, then recompile
                removed.add(unit)
                swapped.append(_describe_unit(plan, unit))
                log.append({"iteration": iterations, "unit": _describe_unit(plan, unit),
                            "reason": "wraps to a second line in the PDF"})
            continue
        if check.pages is None or check.pages <= 1:
            return FitResult(False, tex, log, spacing, iterations, check, result,
                             ["PDF verification failed for reasons other than length: "
                              + "; ".join(check.reasons)], set(removed))
        taken = 0
        while applied < len(sequence) and taken < BULLETS_REMOVED_PER_ITERATION:
            unit, why = sequence[applied]
            if why.startswith(SPARE_REASON) and loss is not None:
                # bullets above their floors and spare entries share one greedy queue: drop
                # what loses the least page coverage per line (static value breaks ties)
                spare = [i for i in range(applied, len(sequence))
                         if sequence[i][1].startswith(SPARE_REASON)
                         and sequence[i][0] not in removed
                         and not _unit_is_noop(sequence[i][0], removed, plan)]
                if not spare:
                    applied += 1
                    continue
                pick = min(spare, key=lambda i: (loss(removed, sequence[i][0]), i))
                sequence[applied], sequence[pick] = sequence[pick], sequence[applied]
                unit, why = sequence[applied]
            applied += 1
            if _unit_is_noop(unit, removed, plan):
                continue
            removed.add(unit)
            order.append(unit)
            log.append({"iteration": iterations, "unit": _describe_unit(plan, unit),
                        "reason": why, "key": list(unit)})
            taken += 1
        if taken:
            continue
        if spacing + 1 < len(SPACING_LEVELS):
            spacing += 1
            log.append({"iteration": iterations, "unit": f"spacing level {spacing}",
                        "reason": "content floor reached; bounded spacing tightening"})
            continue
        break
    if check is None or not check.ok:
        diagnostics = [
            f"Still {check.pages if check else '?'} pages after removing {len(order)} of "
            f"{len(sequence)} removable units and applying the tightest spacing level "
            f"({spacing}); remaining content is at the evidence floor (experience floors "
            f"{FLOOR_RELEVANT_EXPERIENCE}/{FLOOR_OTHER_EXPERIENCE} bullets, education and "
            f"awards untouched, fonts and margins unchanged).",
            "A human decision is needed: shorten template bullets, lower the trimming floor, "
            "or approve smaller margins/fonts.",
        ]
        return FitResult(False, tex, log, spacing, iterations, check, result, diagnostics,
                         set(removed))
    best = (tex, result, check)
    restored: list[str] = []
    restored_units: list[list] = []
    build_holds_best = True  # every trial overwrites the same PDF in build_dir
    candidates = [u for u in reversed(order)
                  if not (u[0] == "bullet" and ("entry", u[1], u[2]) in removed)]
    for unit in candidates[:REFILL_ATTEMPTS]:
        if iterations >= MAX_FIT_ITERATIONS:
            break
        if unit[0] == "bullet" and ("entry", unit[1], unit[2]) in removed:
            continue
        trial = removed - {unit}
        iterations += 1
        t_tex, t_result, t_check = _attempt(plan, trial, spacing, tools, build_dir, jobname,
                                            texinputs, runner)
        if t_check is not None and t_check.ok:
            removed = trial
            best = (t_tex, t_result, t_check)
            restored.append(_describe_unit(plan, unit))
            restored_units.append(list(unit))
            build_holds_best = True
        else:
            build_holds_best = False
    tex, result, check = best
    if not build_holds_best:
        # A failed trial overwrote the build PDF: recompile the kept version so the
        # file that gets published is exactly the one that was verified.
        iterations += 1
        tex, result, check = _attempt(plan, removed, spacing, tools, build_dir, jobname,
                                      texinputs, runner)
        if check is None or not check.ok:
            return FitResult(False, tex, log, spacing, iterations, check, result,
                             ["final recompile after refill did not verify"], set(removed),
                             restored)
    log = [item for item in log if item.get("key") not in restored_units]
    return FitResult(True, tex, log, spacing, iterations, check, result, [], set(removed),
                     restored, swapped)


MAX_SWAPS = 12


def _page_coverage(model: CoverageModel, lines: Sequence[ResumeLine]) -> Fraction:
    """The scorer's objective for the rendered page: requirement weight x credit."""
    table: dict[str, dict[str, Proof]] = {}
    for req in model.requirements:
        for term in req.terms:
            for key in (term.members or (term.key,)):
                if key not in table:
                    table[key], _why = resume_proofs(key, lines)
            if term.kind == "any_of":
                table[term.key] = _merge_members(term, table)
    return sum((r.weight * _requirement_credit(r, table) for r in model.requirements),
               Fraction(0))


def improve_by_swaps(plan: ResumePlan, removed: set[tuple], analysis: Analysis) -> list[str]:
    """After fitting, swap a shown kit bullet for an unshown one of the same entry while
    that raises the page's coverage. Every kit bullet is one line (wrap probe), so a swap
    keeps the page length; the caller recompiles and verifies, and undoes on failure."""
    model = analysis.coverage
    if model is None or not model.requirements:
        return []
    done: list[str] = []
    current = _page_coverage(model, resume_lines_from_plan(plan, removed, analysis.master))
    for _ in range(MAX_SWAPS):
        best: tuple[Fraction, EntryPlan, int, Candidate] | None = None
        for si, entries in active_model(plan, removed):
            for ei, bullets in entries:
                ep = plan.entries[(si, ei)]
                shown = {c.bullet_index for c in bullets}
                parents = {c.parent_index for c in bullets if c.parent_index is not None}
                for position, old in enumerate(ep.selected):
                    if old.kit_id is None or old.bullet_index not in shown or \
                            old.bullet_index in parents:
                        continue
                    for new in ep.candidates:
                        if new in ep.selected or new.kit_id is None or new.impact < old.impact \
                                or (new.parent_index is not None and
                                    new.parent_index not in shown):
                            continue  # never trade a result bullet for a detail bullet
                        saved = list(ep.selected)
                        ep.selected[position] = new
                        ep.selected[:] = order_with_parents(ep.selected, ep.candidates)
                        trial = _page_coverage(model, resume_lines_from_plan(
                            plan, removed, analysis.master))
                        ep.selected[:] = saved
                        if trial > current and (best is None or trial > best[0]):
                            best = (trial, ep, position, new)
        if best is None:
            break
        trial, ep, position, new = best
        old = ep.selected[position]
        ep.selected[position] = new
        ep.selected[:] = order_with_parents(ep.selected, ep.candidates)
        new.value = old.value
        current = trial
        done.append(f"{old.kit_id} -> {new.kit_id} ({_entry_name(plan, ep.section_index, ep.entry_index)})")
    return done


def _wrapped_units(plan: ResumePlan, removed: set[tuple], texts: Sequence[str]) -> list[tuple]:
    """Bullet units whose text wrapped (skills rows are fixed by the probe, not here)."""
    wanted = {_alnum(t) for t in texts}
    units = []
    for si, entries in active_model(plan, removed):
        for ei, bullets in entries:
            units += [("bullet", si, ei, c.bullet_index) for c in bullets
                      if _alnum(c.plain) in wanted]
    return units


def _describe_unit(plan: ResumePlan, unit: tuple) -> str:
    section = plan.template.sections[unit[1]]
    entry = section.entries[unit[2]]
    name = latex_to_plain(entry.args[0])[:50]
    if unit[0] == "entry":
        return f"entry '{name}' ({section.title})"
    cand = next((c for c in plan.entries[(unit[1], unit[2])].candidates
                 if c.bullet_index == unit[3]), None)
    bullet = cand.plain if cand is not None else entry.bullets[unit[3]].plain
    tag = f"{cand.kit_id} " if cand is not None and cand.kit_id else ""
    return f"bullet {tag}'{bullet[:60]}' from '{name}'"


# ---------------------------------------------------------------------------
# Output naming and atomic writes
# ---------------------------------------------------------------------------
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,150}$")


def output_base(jd: JobDescription, outputs_dir: Path) -> tuple[str, list[str]]:
    """'<company>_<role>' slug; a deterministic suffix avoids silent overwrites."""
    notes: list[str] = []
    company = slugify(jd.company) or "unknown-company"
    role = slugify(jd.role) or "unknown-role"
    base = f"{company}_{role}"
    existing = outputs_dir / f"{base}_analysis.json"
    if existing.exists():
        owner = None
        try:
            owner = json.loads(existing.read_text(encoding="utf-8")).get("source_job_description")
        except (OSError, ValueError, AttributeError):
            owner = None
        if owner != jd.display_path:
            suffixed = f"{base}-{sha256_text(jd.display_path)[:8]}"
            notes.append(f"'{existing.name}' belongs to another job description ({owner!r}); "
                         f"using '{suffixed}' to avoid overwriting it")
            base = suffixed
            existing = outputs_dir / f"{base}_analysis.json"
            if existing.exists():
                try:
                    owner = json.loads(existing.read_text(encoding="utf-8")).get(
                        "source_job_description")
                except (OSError, ValueError, AttributeError):
                    owner = None
                if owner != jd.display_path:
                    raise OutputError(f"Output name collision for {base}; remove or rename "
                                      f"{existing} and retry")
    return base, notes


def safe_output_path(outputs_dir: Path, name: str) -> Path:
    if not SAFE_NAME.match(name):
        raise OutputError(f"Refusing unsafe output file name: {name!r}")
    root = outputs_dir.resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise OutputError(f"Refusing to write outside {root}: {name!r}")
    return path


def _default_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, _default_mode())
        os.replace(tmp, path)
    except OSError as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise OutputError(f"Could not write {path}: {exc}") from exc


def atomic_copy(src: Path, dst: Path) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, _default_mode())
        os.replace(tmp, dst)
    except OSError as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise OutputError(f"Could not write {dst}: {exc}") from exc


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def ensure_outputs_dir(outputs_dir: Path) -> None:
    try:
        outputs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"Cannot create output directory {outputs_dir}: {exc}") from exc
    if not os.access(outputs_dir, os.W_OK):
        raise OutputError(f"Output directory is not writable: {outputs_dir}")


def previous_valid_pdf(outputs_dir: Path, base: str) -> bool:
    """True when the existing PDF is the verified output recorded in the last report."""
    pdf = outputs_dir / f"{base}_resume.pdf"
    report = outputs_dir / f"{base}_analysis.json"
    if not pdf.exists() or not report.exists():
        return False
    try:
        resume = json.loads(report.read_text(encoding="utf-8")).get("resume", {})
    except (OSError, ValueError, AttributeError):
        return False
    return bool(resume.get("single_page_verified")) and resume.get("pdf_sha256") == \
        file_sha256(pdf)


def clear_previous_outputs(outputs_dir: Path, base: str) -> list[str]:
    """--no-cache: delete this job's earlier outputs (the analysis JSON is rewritten anyway).

    Without it, a verified PDF survives a failed rebuild by design, which can look as if
    the run changed nothing; the summary then says so (``last_valid_pdf_kept``).
    """
    removed = []
    for path in sorted(outputs_dir.glob(f"{base}_*")):
        if path.is_file() and path.name != f"{base}_analysis.json":
            path.unlink()
            removed.append(path.name)
    shutil.rmtree(outputs_dir / ".build" / base, ignore_errors=True)
    return removed


# ---------------------------------------------------------------------------
# Provenance: every rendered claim traced to positive master evidence
# ---------------------------------------------------------------------------
REPO_LINK = re.compile(r"(?:https?://)?(?:github\.com|gitlab\.com|bitbucket\.org)/[A-Za-z0-9_.-]+"
                       r"(?:/[A-Za-z0-9_.-]+)?|(?:https?://)?[A-Za-z0-9-]+\."
                       r"(?:vercel\.app|github\.io|netlify\.app)", re.IGNORECASE)
TAG_LABELS = {TAG_VERIFIED: "[V]", TAG_REPO: "[R]", TAG_TABLE: "skills table"}


def _entry_links(master: MasterProfile, entry: MasterEntry | None, header_raw: str = "") -> list[str]:
    links: list[str] = []
    found = [u.split("://", 1)[-1] for u in re.findall(r"\\href\{([^{}]*)\}", header_raw)
             if not u.startswith("mailto:")]
    if entry is not None:
        for n in range(entry.start_line, min(entry.end_line, len(master.lines)) + 1):
            found.extend(m.group(0) for m in REPO_LINK.finditer(master.line(n).raw))
    for link in found:
        link = link.split("://", 1)[-1].rstrip("./")
        if link and link not in links:
            links.append(link)
    return links


def _proof_line(master: MasterProfile, n: int) -> dict[str, Any]:
    line = master.line(n)
    entry = master.entry(line.entry_id)
    tag = TAG_LABELS.get(line.tag or "", f"[{line.tag}]" if line.tag else line.kind)
    return {"line": n, "tag": tag, "section": line.section,
            "entry": entry.title if entry else None, "text": excerpt(_line_text(line), 200)}


def _skill_lines(master: MasterProfile, key: str, lines: set[int] | None,
                 kinds: frozenset[str] = frozenset({"positive"})) -> list[int]:
    """Lines evidencing a skill (or, for a category, one of its members)."""
    found: set[int] = set()
    for skill in (key, *SKILL_INDEX[key].members):
        if skill in master.global_no:
            continue
        found |= {o.line for o in master.occurrences.get(skill, [])
                  if o.kind in kinds and (lines is None or o.line in lines)}
    return sorted(found)


def _wording_lines(plain: str, scope: EvidenceScope, cited: Sequence[int]) -> list[int]:
    lines = [n for n in cited if n in scope.texts]
    if not lines:
        claim = content_stems(plain)
        ranked = sorted(scope.line_numbers,
                        key=lambda n: (-len(claim & content_stems(scope.texts.get(n, ""))), n))
        lines = [n for n in ranked[:3] if claim & content_stems(scope.texts.get(n, ""))]
    return lines


def _trace_claim(kind: str, plain: str, master: MasterProfile, scope: EvidenceScope,
                 check: ClaimCheck, problems: list[str]) -> dict[str, Any]:
    """Evidence for one checked claim: wording lines, skill lines and metric lines."""
    if not check.ok:
        problems.extend(check.reasons)
    lines = _wording_lines(plain, scope, check.source_lines)
    positive_scope = {n for n in scope.texts if master.line(n).tag in POSITIVE_TAGS}
    skills = []
    for key in check.skills:
        skill_lines = _skill_lines(master, key, positive_scope if scope.entry_id else None)
        if not skill_lines:
            problems.append(f"no positive line for '{SKILL_INDEX[key].display}'")
        skills.append({"skill": SKILL_INDEX[key].display, "lines": skill_lines[:5]})
    metrics = []
    for match in STANDALONE_NUMBER.finditer(plain):
        value = _normalize_number(match.group(0))
        sources = [n for n in scope.numbers.get(value, []) if n in lines] or \
            scope.numbers.get(value, [])[:1]
        if not sources:
            problems.append(f"number '{match.group(0)}' has no source line")
            continue
        metrics.append({"value": match.group(0), "line": sources[0]})
        if sources[0] not in lines:
            lines.append(sources[0])
    for token in ALNUM_RUN.findall(plain):  # verbatim codes such as 8am, 180K, P99
        if not (any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)):
            continue
        holders = [n for n in sorted(scope.texts)
                   if token.lower() in {t.lower() for t in ALNUM_RUN.findall(scope.texts[n])}]
        sources = [n for n in holders if n in lines] or holders[:1]
        if not sources:
            problems.append(f"token '{token}' has no source line")
            continue
        metrics.append({"value": token, "line": sources[0]})
        if sources[0] not in lines:
            lines.append(sources[0])
    if not lines:
        problems.append("no positive master line supports this wording")
    return {"evidence": [_proof_line(master, n) for n in sorted(lines)], "skills": skills,
            "metrics": metrics, "wording_support": check.support}


def _trace_kit_claim(plain: str, master: MasterProfile, entry_id: str | None,
                     kit_id: str | None, kit_line: int) -> tuple[dict[str, Any], list[str]]:
    """Kit wording: its kit row, the master lines behind it, and the audit's findings.

    Only hard findings (a [NO] or unevidenced tool, a [NO] echo, a number in no line of the
    entry that is not a sum of its counts) stop the build; soft ones are listed as notes.
    """
    hard, soft = audit_claim(plain, master, entry_id)
    scope = build_scope(master, entry_id)
    check = verify_claim(plain, master, scope)
    ignored: list[str] = []
    trace = _trace_claim("bullet", plain, master, scope,
                         dataclasses.replace(check, ok=True, reasons=[], codes=[]), ignored)
    trace["evidence"] = [e for e in trace["evidence"]
                         if master.line(e["line"]).tag in POSITIVE_TAGS]
    trace["kit"] = {"id": kit_id, "line": kit_line,
                    "text": excerpt(_line_text(master.line(kit_line)), 200) if kit_line else ""}
    trace["audit_notes"] = soft
    if not trace["evidence"] and not hard:
        soft.append("no single [V]/[R] line restates it; it rests on the kit row")
    return trace, hard


def _evidence_problems(master: MasterProfile, evidence: Sequence[dict[str, Any]],
                       entry_id: str | None, allow_headings: bool = False) -> list[str]:
    problems = []
    entry = master.entry(entry_id)
    for item in evidence:
        line = master.line(item["line"])
        if allow_headings and line.kind in ("heading", "bold") and line.tag in (
                None, TAG_VERIFIED, TAG_REPO) and (line.entry_id == entry_id or (
                    entry is not None and item["line"] == entry.start_line)):
            continue  # entry headers may cite their own heading and label lines
        if line.meta or line.tag not in POSITIVE_TAGS:
            problems.append(f"cites line {item['line']}, which is not tagged [V]/[R]")
        elif entry_id is not None and line.entry_id != entry_id and entry_id not in line.refs:
            problems.append(f"cites line {item['line']}, which belongs to another entry")
    return problems


def _kv_item_lines(plain: str, master: MasterProfile, exposure_ok: bool) -> list[int]:
    kinds = frozenset({"positive", "exposure"}) if exposure_ok else frozenset({"positive"})
    lines: set[int] = set()
    mentions = SKILL_FINDER.find(plain)
    for mention in mentions:
        lines |= set(_skill_lines(master, mention.key, None, kinds)[:3])
    covered = [(m.start, m.end) for m in mentions]
    words = [m.group(0).rstrip(".") for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#.-]*", plain)
             if not any(s <= m.start() < e for s, e in covered)]
    words = [w for w in words if len(w) >= 2 and w.lower() not in STOPWORDS
             and (re.search(r"[A-Z]", w) or not mentions)]
    for word in words:
        for line in master.lines:
            if not line.meta and line.tag in POSITIVE_TAGS and \
                    _literal_in(_line_text(line).lower(), word):
                lines.add(line.number)
                break
    return sorted(lines)


def build_provenance(plan: ResumePlan, master: MasterProfile, removed: set[tuple]
                     ) -> dict[str, Any]:
    """Map every claim that will be rendered to master lines; raise on any gap.

    This is a hard gate that runs before anything is written or compiled. It
    re-verifies each claim independently of the selection step, so a bug there
    cannot put an untraceable claim on the page.
    """
    claims: list[dict[str, Any]] = []
    untraced: list[str] = []

    def add(record: dict[str, Any], problems: list[str]) -> None:
        record["traced"] = not problems
        if problems:
            record["problems"] = problems
            untraced.append(f"{record['type']} {record['text'][:70]!r}: {problems[0]}")
        claims.append(record)

    for si, entries in active_model(plan, removed):
        section = plan.template.sections[si]
        if section.kind == "paragraph":
            if plan.summary_text is None:
                continue
            scope = build_scope(master, None)
            for sentence in plan.summary_sentences:
                plain = latex_to_plain(sentence)
                problems: list[str] = []
                if plan.kit_mode and plan.summary_source:
                    trace, problems = _trace_kit_claim(plain, master, None, None,
                                                       plan.summary_source["kit_line"])
                else:
                    trace = _trace_claim("summary_sentence", plain, master, scope,
                                         verify_claim(plain, master, scope), problems)
                problems += _evidence_problems(master, trace["evidence"], None)
                add({"id": f"s{si}.summary.{len(claims)}", "type": "summary_sentence",
                     "section": section.title, "entry": None, "latex": sentence, "text": plain,
                     "master_entry": None, **trace}, problems)
            continue
        if section.kind == "keyvalue":
            for li, kv in enumerate(section.kv_lines):
                label = kv.label.lower()
                exposure_ok = "familiar" in label or "interest" in label
                if (si, li) in plan.gap_lines:  # unmet asks: traced to the JD, claims nothing
                    for gap in plan.gap_lines[(si, li)]:
                        add({"id": f"s{si}.l{li}.gap.{slugify(gap['item'], 30)}",
                             "type": "interest_gap", "section": section.title,
                             "entry": kv.label, "latex": latex_escape(gap["item"]),
                             "text": gap["item"], "master_entry": None, "evidence": [],
                             "jd_requirements": gap["requirements"],
                             "jd_lines": gap["jd_lines"]}, [])
                    continue
                for item in plan.kv_items.get((si, li), []):
                    plain = latex_to_plain(item)
                    lines = _kv_item_lines(plain, master, exposure_ok)
                    record = {"id": f"s{si}.l{li}.{slugify(plain, 30)}", "type": "skills_item",
                              "section": section.title, "entry": kv.label, "latex": item,
                              "text": plain, "master_entry": None,
                              "evidence": [_proof_line(master, n) for n in lines]}
                    if (si, li) in plan.kit_rows:  # a 9.4 row item: hard audit problems only
                        hard, soft = audit_claim(plain, master, None)
                        record["kit"] = {"line": plan.kit_rows[(si, li)]}
                        record["audit_notes"] = soft if lines else soft + [
                            "named only in a heading or note of the master profile"]
                        add(record, hard)
                        continue
                    problems = [] if lines else ["no master line names this item"]
                    reason = _kv_item_problem(plain, master, exposure_ok, _positive_text(master))
                    if reason:
                        problems.append(reason)
                    add(record, problems)
            continue
        for ei, bullets in entries:
            ep = plan.entries[(si, ei)]
            entry = section.entries[ei]
            kind = template_section_kind(section.title)
            master_entry = master.entry(ep.master_entry_id)
            entry_info = None if master_entry is None else {
                "id": master_entry.id, "title": master_entry.title,
                "section": master_entry.section, "heading_line": master_entry.start_line,
                "links": _entry_links(master, master_entry, ep.header_raw or entry.header_raw)}
            entry_name = latex_to_plain(entry.args[0])
            header_check = _verify_header(entry, kind, master, ep.master_entry_id) \
                if master_entry else None
            problems = [] if master_entry else ["entry is not mapped to a master entry"]
            header_lines: list[int] = []
            if master_entry is not None and header_check is not None:
                if not header_check.ok:
                    problems.extend(header_check.reasons)
                header_lines = sorted({master_entry.start_line, *header_check.source_lines})
            header_plain = " | ".join(latex_to_plain(a) for a in entry.args if latex_to_plain(a))
            evidence = [_proof_line(master, n) for n in header_lines]
            problems += _evidence_problems(master, evidence, ep.master_entry_id,
                                           allow_headings=True)
            add({"id": f"s{si}.e{ei}.header", "type": "entry_header", "section": section.title,
                 "entry": entry_name, "latex": (ep.header_raw or entry.header_raw).strip(),
                 "text": header_plain, "master_entry": entry_info, "evidence": evidence},
                problems)
            if master_entry is None:
                continue
            scope = build_scope(master, ep.master_entry_id)
            positive_scope = {n for n in scope.texts if master.line(n).tag in POSITIVE_TAGS}
            emph = EMPH_PATTERN.search((ep.header_raw or entry.header_raw)) \
                if kind in TECH_LIST_SECTIONS else None
            if emph:
                content, _end = read_group(ep.header_raw or entry.header_raw, emph.end())
                for item in _split_items(content):
                    plain = latex_to_plain(item)
                    keys = SKILL_FINDER.keys(plain)
                    lines: list[int] = []
                    for key in keys:
                        lines += [n for n in _skill_lines(master, key, positive_scope)[:3]
                                  if n not in lines]
                    if not keys:
                        lines = _wording_lines(plain, scope, [])
                    item_problems = [] if lines else ["no positive line in this entry names it"]
                    if any(key in master.global_no for key in keys):
                        item_problems.append("names a [NO] skill")
                    add({"id": f"s{si}.e{ei}.stack.{slugify(plain, 30)}", "type": "stack_item",
                         "section": section.title, "entry": entry_name, "latex": item,
                         "text": plain, "master_entry": entry_info,
                         "evidence": [_proof_line(master, n) for n in sorted(lines)]},
                        item_problems)
            for cand in bullets:
                problems = []
                if cand.kit_id:
                    trace, problems = _trace_kit_claim(cand.plain, master, ep.master_entry_id,
                                                       cand.kit_id, cand.kit_line)
                else:
                    trace = _trace_claim("bullet", cand.plain, master, scope,
                                         verify_claim(cand.plain, master, scope), problems)
                problems += _evidence_problems(master, trace["evidence"], ep.master_entry_id)
                add({"id": f"s{si}.e{ei}.b{cand.bullet_index}", "type": "bullet",
                     "section": section.title, "entry": entry_name, "latex": cand.raw,
                     "text": cand.plain, "master_entry": entry_info,
                     "matched_requirements": list(cand.req_hits),
                     "requirement_ids": list(cand.requirement_ids),
                     "relevance": cand.relevance, **trace}, problems)
    if untraced:
        raise ProvenanceValidationError(
            f"{len(untraced)} claim(s) cannot be traced to [V]/[R] evidence in "
            f"{master.path.name}; nothing was written. First: " + "; ".join(untraced[:3]))
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim["type"]] = counts.get(claim["type"], 0) + 1
    return {"master_profile": master.path.name,
            "master_profile_sha256": sha256_text("\n".join(l.raw for l in master.lines)),
            "rule": "Every rendered claim cites [V], [R] or skills-table lines of the master "
                    "profile. Entry headers may also cite their own heading line, and "
                    "'Familiar with' items may cite exposure-table rows. The Interests line "
                    "lists the job's unmet asks and is traced to the JD lines that ask for "
                    "them; it claims no experience. Any other untraced item raises "
                    "ProvenanceValidationError before anything is written.",
            "summary": {"claims": len(claims), "traced": len(claims), "by_type": counts},
            "claims": claims}


# ---------------------------------------------------------------------------
# Interview briefing (written next to every verified resume)
# ---------------------------------------------------------------------------
# Every sentence is either verbatim JD text, verbatim resume text with its master
# lines, or fixed template wording. Nothing is paraphrased or invented.
BRIEFING_CONTEXT_LINES = 5
BRIEFING_FOCUS_REQUIREMENTS = 8
BRIEFING_STAR_PROMPTS = 3
CONTEXT_SECTIONS = frozenset({"responsibilities", "mandatory", "required", "intro",
                              "unclassified", "preferred"})


def _technical_context(analysis: Analysis) -> list[dict[str, Any]]:
    """JD lines carrying the most requirement weight, quoted verbatim."""
    weight: dict[int, int] = {}
    names: dict[int, list[tuple[str, str]]] = {}
    for req in analysis.requirements:
        for n in req.jd_lines:
            weight[n] = weight.get(n, 0) + req.weight
            if (req.name, req.status) not in names.setdefault(n, []):
                names[n].append((req.name, req.status))
    lines = [l for l in analysis.jd.lines
             if not l.is_heading and l.section_type in CONTEXT_SECTIONS
             and weight.get(l.number) and not is_pay_text(l.text)]
    ranked = sorted(lines, key=lambda l: (-weight[l.number], l.number))[:BRIEFING_CONTEXT_LINES]
    return [{"jd_line": l.number, "section": l.section_title or l.section_type,
             "text": collapse_ws(l.text.lstrip("-*• ")), "requirement_weight": weight[l.number],
             "requirements": [{"name": n, "status": s} for n, s in names[l.number]]}
            for l in ranked]


def _metric_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in METRIC_TOKEN.finditer(text):
        if match.group(0) not in tokens:
            tokens.append(match.group(0))
    return tokens


def build_briefing(analysis: Analysis, provenance: dict[str, Any], pdf_name: str
                   ) -> dict[str, Any]:
    bullets = [{**c, "entry": c["entry"].split(" | ")[0].strip()}  # drop project stack lists
               for c in provenance["claims"] if c["type"] == "bullet"]
    metrics = []
    for claim in bullets:
        tokens = _metric_tokens(claim["text"])
        if tokens:
            metrics.append({"metrics": tokens, "entry": claim["entry"], "bullet": claim["text"],
                            "master_lines": sorted({m["line"] for m in claim["metrics"]})})
    order = {id(req): i for i, req in enumerate(analysis.requirements)}
    matched = sorted((r for r in analysis.requirements if r.status == STATUS_MATCHED),
                     key=lambda r: (-r.weight, order[id(r)]))
    prompts: list[dict[str, Any]] = []
    used_bullets: set[str] = set()
    used_entries: set[str] = set()

    def pick(pool: list[dict[str, Any]]) -> dict[str, Any] | None:
        pool = [c for c in pool if c["id"] not in used_bullets]
        if not pool:
            return None
        return min(pool, key=lambda c: (c["entry"] in used_entries,
                                        not _metric_tokens(c["text"]), -c["relevance"],
                                        bullets.index(c)))

    def prompt(claim: dict[str, Any], req: Requirement | None) -> dict[str, Any]:
        used_bullets.add(claim["id"])
        used_entries.add(claim["entry"])
        tokens = _metric_tokens(claim["text"])
        lines = [e["line"] for e in claim["evidence"]]
        master_entry = (claim.get("master_entry") or {}).get("title") or claim["entry"]
        return {
            "requirement": req.name if req else None,
            "category": req.category if req else None,
            "jd_excerpt": req.jd_excerpt if req else None,
            "likely_question": (f"Tell me about a time you used {req.name}." if req else
                                f"Walk me through your work at {claim['entry']}."),
            "situation": f"{claim['entry']} ({master_entry})",
            "task": (f"The posting asks for {req.name}: \"{req.jd_excerpt}\"" if req else
                     "Strongest remaining evidence on this resume."),
            "action": claim["text"],
            "result": (", ".join(tokens) if tokens else
                       f"No metric in this bullet; describe the outcome only as master lines "
                       f"{', '.join(map(str, lines))} state it."),
            "evidence_lines": claim["evidence"],
        }

    for req in matched:
        if len(prompts) == BRIEFING_STAR_PROMPTS:
            break
        claim = pick([c for c in bullets if req.name in c["matched_requirements"]])
        if claim is not None:
            prompts.append(prompt(claim, req))
    while len(prompts) < BRIEFING_STAR_PROMPTS:
        claim = pick(bullets)
        if claim is None:
            break
        prompts.append(prompt(claim, None))
    focus = sorted(analysis.requirements, key=lambda r: (-r.weight, order[id(r)]))
    return {
        "company": analysis.jd.company,
        "role": analysis.jd.role,
        "source_job_description": analysis.jd.display_path,
        "resume_pdf": pdf_name,
        "fit_score": analysis.score,
        "primary_domain": analysis.domain["primary"],
        "technical_context": _technical_context(analysis),
        "requirements_to_prepare": [
            {"name": r.name, "category": r.category, "weight": r.weight, "status": r.status}
            for r in focus[:BRIEFING_FOCUS_REQUIREMENTS]],
        "highlighted_metrics": metrics,
        "star_prompts": prompts,
        "note": ("Technical context is quoted verbatim from the job description; actions, "
                 "results and metrics are verbatim resume bullets with their master lines."),
    }


def render_briefing_markdown(briefing: dict[str, Any]) -> str:
    out = [f"# Interview briefing: {briefing['role']} at {briefing['company']}", "",
           f"- Job description: `{briefing['source_job_description']}`",
           f"- Resume: `{briefing['resume_pdf']}` (fit score {briefing['fit_score']}, "
           f"{briefing['primary_domain']})", "",
           "## Technical context (verbatim from the job description)", ""]
    for item in briefing["technical_context"]:
        reqs = ", ".join(f"{r['name']} ({r['status'].replace('_', ' ')})"
                         for r in item["requirements"])
        out.append(f"- **JD line {item['jd_line']}** ({item['section']}): {item['text']}  ")
        out.append(f"  _Requirements:_ {reqs}")
    if not briefing["technical_context"]:
        out.append("- No weighted requirement lines were found in the job description.")
    out += ["", "## Requirements to prepare for", "", "| Requirement | Category | Status |",
            "|---|---|---|"]
    for req in briefing["requirements_to_prepare"]:
        out.append(f"| {req['name']} | {req['category']} | {req['status'].replace('_', ' ')} |")
    out += ["", "## Highlighted metrics in this resume", ""]
    for item in briefing["highlighted_metrics"]:
        lines = ", ".join(map(str, item["master_lines"])) or "-"
        out.append(f"- **{', '.join(item['metrics'])}** ({item['entry']}): {item['bullet']} "
                   f"_(master lines {lines})_")
    if not briefing["highlighted_metrics"]:
        out.append("- This resume build contains no metrics.")
    out += ["", "## STAR talking points", ""]
    for number, item in enumerate(briefing["star_prompts"], 1):
        heading = item["requirement"] or "Additional evidence"
        out += [f"### {number}. {heading}", "",
                f"- **Likely question:** {item['likely_question']}",
                f"- **Situation:** {item['situation']}",
                f"- **Task:** {item['task']}",
                f"- **Action:** {item['action']}",
                f"- **Result:** {item['result']}",
                f"- **Evidence:** master lines "
                f"{', '.join(str(e['line']) for e in item['evidence_lines'])}", ""]
    out += ["---", "", f"_{briefing['note']}_", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Resume stage
# ---------------------------------------------------------------------------
def _selection_report(plan: ResumePlan, removed_units: set[tuple]) -> list[dict[str, Any]]:
    report = []
    for si, entries in active_model(plan, removed_units):
        section = plan.template.sections[si]
        for ei, bullets in entries:
            ep = plan.entries[(si, ei)]
            report.append({
                "section": section.title,
                "entry": latex_to_plain(section.entries[ei].args[0]),
                "master_entry": ep.master_entry_id,
                "bullets": [{"kit_id": c.kit_id, "text": c.plain, "relevance": c.relevance,
                             "matched_requirements": list(c.req_hits),
                             "wording_support": c.support, "source_lines": c.source_lines}
                            for c in bullets],
            })
    return report


def _rejections(plan: ResumePlan) -> list[dict[str, Any]]:
    result = []
    for (si, ei), ep in sorted(plan.entries.items()):
        for item in ep.rejected:
            result.append({"section": plan.template.sections[si].title,
                           "entry": latex_to_plain(plan.template.sections[si].entries[ei].args[0]),
                           **item})
    return result


def build_gates(fit: FitResult | None, ats: AtsCheck | None,
                layout: LayoutCheck | None = None) -> dict[str, Any]:
    """The proof-of-build gates; a later gate runs only after the earlier ones pass."""
    def state(passed: bool | None) -> str:
        return "not_run" if passed is None else ("passed" if passed else "failed")

    result = fit.compile_result if fit else None
    log = result.log_text if result else ""
    syntax = None if result is None else result.ok
    check = fit.check if fit else None
    single = None if not syntax or check is None else (check.ok and check.pages == 1)
    return {
        "gate_1_syntax": {
            "status": state(syntax),
            "returncode": result.returncode if result else None,
            "fatal_errors": [collapse_ws(l)[:200] for l in fatal_log_lines(log)][:5],
            "overfull_boxes": len(re.findall(r"^Overfull \\[hv]box", log, re.MULTILINE)),
            "detail": result.error if result is not None and not result.ok else "",
        },
        "gate_2_single_page": {
            "status": state(single),
            "pdfinfo_pages": check.pdfinfo_pages if check else None,
            "form_feed_pages": check.pdftotext_pages if check else None,
            "compile_iterations": fit.iterations if fit else 0,
            "units_trimmed": len(fit.removed) if fit else 0,
            "spacing_level": fit.spacing_level if fit else 0,
        },
        "gate_3_ats_readability": ({"status": "not_run"} if ats is None else
                                   {"status": state(ats.ok), **dataclasses.asdict(ats)}),
        "gate_4_layout": ({"status": "not_run"} if layout is None else
                          {"status": state(layout.ok), **dataclasses.asdict(layout)}),
    }


def _write_companion(outputs_dir: Path, name: str, text: str) -> str:
    path = safe_output_path(outputs_dir, name)
    atomic_write_text(path, text)
    return path.name


def produce_resume(analysis: Analysis, template: ParsedTemplate, outputs_dir: Path, base: str,
                   tools: ToolSet, dry_run: bool,
                   runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                   debug_proof: bool = False) -> tuple[dict[str, Any], int]:
    """Plan, render, compile and verify the resume; returns (resume block, exit code)."""
    measure = None
    if analysis.kit is not None and not dry_run and tools.engine_path:
        measure = make_wrap_probe(template, tools, outputs_dir / ".build" / base, base, runner)
    plan = build_resume_plan(template, analysis.master, analysis, measure=measure)
    # Hard gate: raises before anything is written when a claim is untraceable.
    provenance = build_provenance(plan, analysis.master, set())
    names = {kind: f"{base}_resume{suffix}" for kind, suffix in (
        ("tex", ".tex"), ("pdf", ".pdf"), ("log", ".log"), ("failed_tex", ".failed.tex"),
        ("failed_log", ".failed.log"), ("unverified_tex", ".unverified.tex"))}
    proof_name = f"{base}_provenance.json"

    def write_proof(removed: set[tuple]) -> dict[str, Any]:
        """Provenance of exactly what was rendered; written with --debug-proof."""
        proof = build_provenance(plan, analysis.master, removed)
        block["provenance"].update(proof["summary"], scope="rendered resume")
        if debug_proof:
            block["provenance"]["file"] = _write_companion(outputs_dir, proof_name,
                                                           dump_json(proof))
            block["files"]["provenance"] = proof_name
        return proof
    block: dict[str, Any] = {
        "generated": False,
        "status": "planned",
        "tools": tools.describe(),
        "compiled": False,
        "single_page_verified": False,
        "page_count": None,
        "verification": None,
        "fitting": None,
        "selected_content": _selection_report(plan, set()),
        "project_ranking": plan.project_scores,
        "considered": plan.considered,
        "summary_facts": plan.summary_facts,
        "summary_source": plan.summary_source,
        "rejected_template_bullets": _rejections(plan),
        "template_items_removed": plan.template_items_removed,
        "warnings": plan.warnings,
        "provenance": {**provenance["summary"], "scope": "all candidate content (pre-fit)",
                       "file": None},
        "build_gates": build_gates(None, None),
        "files": {},
    }
    if dry_run:
        block["status"] = "dry_run"
        block["reason"] = "--dry-run: nothing compiled or written"
        return block, EXIT_OK
    keep_valid = previous_valid_pdf(outputs_dir, base)
    if not tools.engine_path:
        tex = render_resume(plan, set(), 0)
        target = names["unverified_tex"] if keep_valid else names["tex"]
        path = safe_output_path(outputs_dir, target)
        atomic_write_text(path, tex)
        write_proof(set())
        block.update(generated=True, status="not_compiled", tex_sha256=sha256_text(tex))
        block["files"]["tex"] = path.name
        block["reason"] = (f"No compatible LaTeX engine: the template needs "
                           f"{tools.required_engine} ({tools.engine_reason}). The .tex was "
                           f"written from the full candidate set (before fitting); no PDF was "
                           f"compiled and no page count was verified.")
        return block, EXIT_MISSING_PREREQ
    if not tools.pdftotext:
        raise MissingPrerequisiteError("pdftotext is required to verify the PDF text and page "
                                       "boundaries; install poppler-utils")
    build_dir = outputs_dir / ".build" / base
    if build_dir.exists():
        shutil.rmtree(build_dir)
    jobname = f"{base}_resume"
    loss = None
    if plan.kit_mode and analysis.coverage is not None and analysis.coverage.requirements:
        def loss(removed: set[tuple], unit: tuple) -> float:
            """Page coverage lost per line by removing one whole entry."""
            before = _page_coverage(analysis.coverage, resume_lines_from_plan(
                plan, removed, analysis.master))
            after = _page_coverage(analysis.coverage, resume_lines_from_plan(
                plan, removed | {unit}, analysis.master))
            if unit[0] == "bullet":
                lines = len(resume_lines_from_plan(plan, removed, analysis.master)) - \
                    len(resume_lines_from_plan(plan, removed | {unit}, analysis.master))
                return float(before - after) / max(1, lines)
            ep = plan.entries[(unit[1], unit[2])]
            header = 2 if template_section_kind(
                plan.template.sections[unit[1]].title) == "experience" else 1
            shown = [c for c in ep.selected if ("bullet", *unit[1:], c.bullet_index)
                     not in removed]
            return float(before - after) / (header + len(shown))
    fit = fit_to_one_page(plan, tools, build_dir, jobname, template.path.parent.resolve(),
                          runner, loss)
    removed_units = fit.final_removed
    swaps: list[str] = []
    if fit.ok and plan.kit_mode and fit.compile_result is not None:
        saved = {key: list(ep.selected) for key, ep in plan.entries.items()}
        swaps = improve_by_swaps(plan, removed_units, analysis)
        if swaps:  # same number of one-line bullets: recompile and verify the page again
            tex, result, check = _attempt(plan, removed_units, fit.spacing_level, tools,
                                          build_dir, jobname, template.path.parent.resolve(),
                                          runner)
            if check is not None and check.ok:
                fit.tex, fit.compile_result, fit.check = tex, result, check
                fit.iterations += 1
            else:
                for key, selected in saved.items():
                    plan.entries[key].selected = selected
                swaps = []
                fit.tex, fit.compile_result, fit.check = _attempt(
                    plan, removed_units, fit.spacing_level, tools, build_dir, jobname,
                    template.path.parent.resolve(), runner)
    block["selected_content"] = _selection_report(plan, removed_units)
    if plan.kit_mode and analysis.kit_audit is not None:
        shown = {b["kit_id"] for e in block["selected_content"] for b in e["bullets"]}
        block["kit_notes"] = {bid: notes for bid, notes in analysis.kit_audit.notes.items()
                              if bid in shown}
    block["fitting"] = {"iterations": fit.iterations, "removed": fit.removed,
                        "restored_after_overshoot": fit.restored,
                        "swaps_after_fitting": swaps,
                        "spacing_level": fit.spacing_level,
                        "final_bullets": sum(len(b) for _si, entries in
                                             active_model(plan, removed_units)
                                             for _ei, b in entries),
                        "diagnostics": fit.diagnostics}
    if fit.check is not None:
        block["verification"] = dataclasses.asdict(fit.check)
        block["page_count"] = fit.check.pages
    block["compiled"] = bool(fit.compile_result and fit.compile_result.ok)
    block["generated"] = True
    block["tex_sha256"] = sha256_text(fit.tex)
    if fit.ok and fit.compile_result is not None:
        # Independent re-check of the exact file about to be published.
        final_check = verify_pdf(fit.compile_result.pdf_path, tools,
                                 expected_content(plan, fit.final_removed), runner)
        if not final_check.ok or final_check.pages != 1:
            fit.ok = False
            fit.check = final_check
            fit.diagnostics = ["the PDF about to be published failed its final one-page "
                               "re-check: " + "; ".join(final_check.reasons)]
            block["verification"] = dataclasses.asdict(final_check)
            block["page_count"] = final_check.pages
    ats: AtsCheck | None = None
    if fit.ok and fit.compile_result is not None:
        ats = ats_readability_check(fit.compile_result.pdf_path, tools, plan, removed_units,
                                    runner)
    layout: LayoutCheck | None = None
    if fit.ok and ats is not None and ats.ok and fit.compile_result is not None:
        layout = layout_check(fit.compile_result.pdf_path, tools, plan, removed_units, runner)
        lint = dependency_lint(resume_lines_from_plan(plan, removed_units, analysis.master), plan)
        if lint:
            layout.reasons.append("dependency lint: " + lint[0])
            layout.ok = False
    block["build_gates"] = build_gates(fit, ats, layout)
    block["fitting"]["swapped_for_wrapping"] = fit.swapped
    block["wrap_probe"] = plan.wrap_probe
    if ats is not None and not ats.ok:
        fit.ok = False
    if layout is not None and not layout.ok:
        fit.ok = False
    if fit.ok and fit.compile_result is not None:
        tex_path = safe_output_path(outputs_dir, names["tex"])
        pdf_path = safe_output_path(outputs_dir, names["pdf"])
        log_path = safe_output_path(outputs_dir, names["log"])
        atomic_write_text(tex_path, fit.tex)
        atomic_copy(fit.compile_result.pdf_path, pdf_path)
        atomic_copy(fit.compile_result.log_path, log_path)
        for stale in ("failed_tex", "failed_log", "unverified_tex"):
            stale_path = safe_output_path(outputs_dir, names[stale])
            if stale_path.exists():
                stale_path.unlink()
        shutil.rmtree(build_dir, ignore_errors=True)
        try:
            build_dir.parent.rmdir()  # remove outputs/.build when nothing else is in it
        except OSError:
            pass
        block.update(status="verified", single_page_verified=True,
                     pdf_sha256=file_sha256(pdf_path))
        block["files"] = {"tex": tex_path.name, "pdf": pdf_path.name, "log": log_path.name}
        briefing = build_briefing(analysis, write_proof(removed_units), pdf_path.name)
        if analysis.coverage is not None:
            rendered = resume_lines_from_plan(plan, removed_units, analysis.master)
            fit = assess_fit(analysis.coverage, rendered)
            fit["top_changes"] = top_changes(analysis.coverage, fit,
                                             unused_candidates(plan, removed_units,
                                                               analysis.master),
                                             [] if plan.kit_mode else
                                             rejected_candidates(plan, analysis.master))
            fit["dependency_lint"] = dependency_lint(rendered, plan)
            fit["selection_check"] = plan.selection_check
            fit["rewrites"] = plan.rewrites
            block["fit"] = fit
        block["files"]["briefing_json"] = _write_companion(
            outputs_dir, f"{base}_briefing.json", dump_json(briefing))
        block["files"]["briefing_md"] = _write_companion(
            outputs_dir, f"{base}_briefing.md", render_briefing_markdown(briefing))
        return block, EXIT_OK
    tex_name = names["failed_tex"] if keep_valid else names["tex"]
    log_name = names["failed_log"] if keep_valid else names["log"]
    tex_path = safe_output_path(outputs_dir, tex_name)
    atomic_write_text(tex_path, fit.tex)
    block["files"]["tex"] = tex_path.name
    if fit.compile_result is not None and fit.compile_result.log_path.exists():
        log_path = safe_output_path(outputs_dir, log_name)
        atomic_copy(fit.compile_result.log_path, log_path)
        block["files"]["log"] = log_path.name
    block["files"]["build_dir"] = str(build_dir)
    block["last_valid_pdf_kept"] = keep_valid
    write_proof(removed_units)
    if ats is not None and not ats.ok:
        block["status"] = "ats_check_failed"
        block["reason"] = "Gate 3 (ATS readability) failed: " + "; ".join(ats.reasons)
        return block, EXIT_FAILED
    if layout is not None and not layout.ok:
        block["status"] = "layout_check_failed"
        block["reason"] = "Gate 4 (layout) failed: " + "; ".join(layout.reasons)
        return block, EXIT_FAILED
    if fit.compile_result is not None and not fit.compile_result.ok:
        block["status"] = "compile_failed"
        block["reason"] = fit.compile_result.error
        return block, EXIT_FAILED
    block["status"] = "page_limit_failed"
    block["reason"] = "; ".join(fit.diagnostics or (fit.check.reasons if fit.check else []))
    return block, EXIT_PAGE_LIMIT


# ---------------------------------------------------------------------------
# Workspace and CLI
# ---------------------------------------------------------------------------
def find_master(workspace: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise MissingPrerequisiteError(f"Master profile not found: {explicit}")
        return explicit
    present = [workspace / name for name in MASTER_CANDIDATES if (workspace / name).is_file()]
    if not present:
        raise MissingPrerequisiteError(
            f"No master profile found in {workspace} (looked for "
            f"{', '.join(MASTER_CANDIDATES)}). The pipeline never invents career history.")
    if len(present) > 1:
        raise MissingPrerequisiteError(
            f"Both {present[0].name} and {present[1].name} exist; pass --master to choose the "
            f"single source of truth")
    return present[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description=("Deterministic, local job-fit analysis and one-page resume tailoring. "
                     "Reads the master profile as the only source of career facts."),
        epilog=("Exit codes: 0 verified resume (or dry-run PROCEED), 2 invalid input, "
                "3 REJECT - LOW FIT, 4 missing prerequisite (master, template, LaTeX engine), "
                "5 analysis/compilation/output failure (including an untraceable claim or a "
                "failed ATS gate), 6 could not fit exactly one page."))
    parser.add_argument("job_description", type=Path,
                        help="path to a .md or .txt job description (e.g. target_jobs/x.md)")
    parser.add_argument("--verbose", action="store_true",
                        help="print every requirement with status, weight and evidence lines")
    parser.add_argument("--dry-run", action="store_true",
                        help="analyze, score and plan bullets without writing or compiling")
    parser.add_argument("--master", type=Path, default=None,
                        help="master profile path (default: master.md or master_profile.md)")
    parser.add_argument("--template", type=Path, default=None,
                        help=f"LaTeX template path (default: {TEMPLATE_NAME})")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help=f"output directory (default: {OUTPUT_DIR_NAME}/ in the workspace)")
    parser.add_argument("--workspace", type=Path, default=None,
                        help="workspace root (default: the directory containing pipeline.py)")
    parser.add_argument("--strict-no", action="store_true",
                        help="treat every [NO] mention as global, even when the skill has "
                             "positive evidence in another entry")
    parser.add_argument("--override-low-fit", action="store_true",
                        help="explicitly build a resume even when the verdict is "
                             "REJECT - LOW FIT (the verdict itself is unchanged)")
    parser.add_argument("--debug-proof", action="store_true",
                        help="also write <base>_provenance.json mapping every rendered claim "
                             "to its master lines, tags and repo links")
    parser.add_argument("--score-resume", type=Path, default=None, metavar="PDF",
                        help="score an existing resume PDF against this job (profile fit, "
                             "resume fit, hard requirements) and print the report; writes "
                             "nothing")
    parser.add_argument("--analyze-gap", action="store_true",
                        help="write <base>_gap_analysis.json even when the verdict is PROCEED "
                             f"(always written below {FIT_THRESHOLD})")
    parser.add_argument("--no-cache", action="store_true",
                        help="remove this job's previous outputs before building, so a failed "
                             "build can never leave an older resume in place")
    parser.add_argument("--no-kit", action="store_true",
                        help="legacy mode for a master profile without '## 9. Resume kit': "
                             "candidate bullets come from template.tex")
    return parser


def run(argv: Sequence[str] | None = None, *,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    workspace = (args.workspace or Path(__file__).resolve().parent).resolve()
    jd_path = args.job_description if args.job_description.is_absolute() \
        else (Path.cwd() / args.job_description)
    try:
        jd = read_job_description(jd_path, workspace)
        master_path = find_master(workspace, args.master).resolve()
        master_sha = file_sha256(master_path)
        out(f"Master profile: {master_path} (sha256 {master_sha})")
        template_path = args.template or workspace / TEMPLATE_NAME
        if not template_path.is_file():
            raise MissingPrerequisiteError(
                f"Template not found: {template_path}. The pipeline never invents a template.")
        master = parse_master(master_path, strict_no=args.strict_no)
        has_kit = any(RESUME_KIT_HEADING.match(line.raw) for line in master.lines)
        if not has_kit and not args.no_kit:
            raise MissingPrerequisiteError(
                f"{master_path} (sha256 {master_sha[:12]}) has no '## 9. Resume kit' section. "
                f"Resumes are built from that kit; add it, or pass --no-kit to use the "
                f"template's bullets instead (legacy mode).")
        kit = load_kit(master) if has_kit and not args.no_kit else None
        template = parse_template(template_path)
        analysis = analyze(jd, master, kit)
        outputs_dir = (args.output_dir or workspace / OUTPUT_DIR_NAME).resolve()
        report = build_report(analysis, workspace)
        report["inputs"].update(master_profile_path=str(master_path),
                                master_profile_file_sha256=master_sha,
                                resume_kit="used" if has_kit and not args.no_kit else
                                "not used (--no-kit)" if has_kit else "absent (--no-kit)")
        tools = detect_tools(template, which)
        if args.score_resume is not None:
            return score_resume(args.score_resume, analysis, report, template, tools, runner, out)
        if args.dry_run:
            base = f"{slugify(jd.company) or 'unknown-company'}_{slugify(jd.role) or 'unknown-role'}"
        else:
            ensure_outputs_dir(outputs_dir)
            base, notes = output_base(jd, outputs_dir)
            report["parsing_warnings"].extend(notes)
            if args.no_cache:
                removed = clear_previous_outputs(outputs_dir, base)
                out(f"Cache: --no-cache removed {len(removed)} previous output file(s) for {base}")
        report["inputs"]["no_cache"] = bool(args.no_cache)
        report["output_base"] = base
        exit_code = EXIT_OK if analysis.verdict == VERDICT_PROCEED else EXIT_LOW_FIT
        if analysis.verdict == VERDICT_PROCEED or args.override_low_fit:
            resume, resume_code = produce_resume(analysis, template, outputs_dir, base, tools,
                                                 args.dry_run, runner, args.debug_proof)
            resume["override_low_fit"] = bool(args.override_low_fit
                                              and analysis.verdict != VERDICT_PROCEED)
            report["resume"] = resume
            if resume_code != EXIT_OK:
                exit_code = resume_code
        else:
            report["resume"] = {"generated": False, "status": "skipped_low_fit",
                                "reason": f"fit_score {analysis.score} < {FIT_THRESHOLD}; no "
                                          f"resume is generated without --override-low-fit",
                                "tools": tools.describe()}
        fit = report["resume"].pop("fit", None) or assess_fit(analysis.coverage, None)
        if not fit["top_changes"] and fit["resume_fit"] is None:
            fit["top_changes"] = top_changes(analysis.coverage, fit)
        report["fit"] = fit_summary(fit)
        if not args.dry_run:
            report["fit"]["files"] = {
                "job": _write_companion(outputs_dir, f"{base}_job.json",
                                        dump_json(build_job_document(analysis))),
                "requirements": _write_companion(outputs_dir, f"{base}_requirements.json",
                                                 dump_json(requirements_document(report, fit))),
                "report": _write_companion(outputs_dir, f"{base}_report.md",
                                           render_run_report(report, fit))}
        gap = None
        if analysis.verdict != VERDICT_PROCEED or args.analyze_gap:
            gap = build_gap_analysis(analysis, "--analyze-gap" if args.analyze_gap
                                     else "below_threshold")
            if not args.dry_run:
                report["gap_analysis"] = _write_companion(
                    outputs_dir, f"{base}_gap_analysis.json", dump_json(gap))
        if not args.dry_run:
            report_path = safe_output_path(outputs_dir, f"{base}_analysis.json")
            atomic_write_text(report_path, dump_json(report))
        print_summary(report, outputs_dir, args.verbose, args.dry_run, out, gap)
        return exit_code
    except PipelineError as exc:
        out(f"ERROR: {exc}")
        return exc.exit_code


def print_summary(report: dict[str, Any], outputs_dir: Path, verbose: bool, dry_run: bool,
                  out: Callable[[str], None], gap: dict[str, Any] | None = None) -> None:
    cov = report["evidence_coverage"]
    out(f"{report['company']} | {report['role']}")
    out(f"Fit score: {report['fit_score']} -> {report['verdict']}  "
        f"({cov['matched_requirements']}/{cov['total_requirements']} requirements matched; "
        f"{cov['explicitly_no']} [NO], {cov['not_evidenced']} not evidenced, "
        f"{cov['ambiguous']} ambiguous; confidence {report['score_confidence']})")
    family = report.get("role_family")
    if family:
        out(f"Role family: {family['code']} ({family['name']}; confidence "
            f"{family['confidence']:.2f}, {family['method']}: {family['reasons'][0]})")
    fit = report.get("fit")
    if fit and fit["profile_fit"] is None:
        out("Profile fit: n/a (no scorable requirement lines)")
    elif fit:
        out(f"Profile fit: {fit['profile_fit']:.1f}/10"
            + (f" (with hard requirements {fit['profile_fit_with_hard_requirements']:.1f})"
               if fit['profile_fit_with_hard_requirements'] != fit['profile_fit'] else "")
            + (f" | Resume fit: {fit['resume_fit']:.1f}/10" if fit["resume_fit"] is not None
               else "")
            + (" | undersells the profile" if fit.get("underselling") else ""))
        for flag in fit["hard_requirement_flags"]:
            out(f"  hard requirement: {flag}")
        out(f"  work authorization: {fit['work_authorization_note']}")
    out(f"Domain: {report['primary_domain']}"
        + (f" (secondary: {', '.join(report['secondary_domains'])})"
           if report['secondary_domains'] else ""))
    if verbose:
        for req in report["requirements"]:
            lines = [e["line"] for e in req["evidence"]]
            out(f"  [{req['status']:<13}] w{req['weight']} {req['category']:<9} {req['name']}"
                + (f" (master lines {lines})" if lines else "")
                + (f" via {req['matched_via']}" if req["matched_via"] else ""))
        for warning in report["parsing_warnings"]:
            out(f"  warning: {warning}")
    resume = report.get("resume", {})
    status = resume.get("status")
    if gap is not None:
        for item in gap["actionable_recommendations"][:3]:
            out(f"  gap: {item['recommendation']}")
    if dry_run:
        out("Dry run: nothing written.")
        if "provenance" in resume:
            out(f"  provenance: {resume['provenance']['traced']}/"
                f"{resume['provenance']['claims']} candidate claims traced to the master profile")
        for entry in resume.get("selected_content", []):
            out(f"  {entry['section']}: {entry['entry'][:60]} "
                f"({len(entry['bullets'])} candidate bullets)")
        return
    out(f"Analysis: {outputs_dir / (report['output_base'] + '_analysis.json')}")
    if report.get("gap_analysis"):
        out(f"Gap analysis: {outputs_dir / report['gap_analysis']}")
    gates = resume.get("build_gates")
    if gates and gates["gate_1_syntax"]["status"] != "not_run":
        out("Build gates: " + ", ".join(f"{name.split('_', 2)[2].replace('_', ' ')} "
                                        f"{gate['status']}" for name, gate in gates.items()))
    if status == "verified":
        files = resume["files"]
        out(f"Resume: {outputs_dir / files['pdf']} (1 page verified by "
            f"{resume['verification']['method']}; {resume['fitting']['iterations']} compile "
            f"iteration(s), spacing level {resume['fitting']['spacing_level']})")
        out(f"Briefing: {outputs_dir / files['briefing_md']}")
        if files.get("provenance"):
            out(f"Provenance: {outputs_dir / files['provenance']} "
                f"({resume['provenance']['claims']} claims traced)")
    elif status:
        out(f"Resume status: {status}" + (f" - {resume.get('reason')}" if resume.get("reason")
                                         else ""))
        for name in resume.get("files", {}).values():
            out(f"  file: {name}")
        if resume.get("last_valid_pdf_kept"):
            out(f"Cache hit: {report['output_base']}_resume.pdf is the PREVIOUS verified resume; "
                f"this run did not replace it (use --no-cache to remove it first)")


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
