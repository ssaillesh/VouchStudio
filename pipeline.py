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
import hashlib
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
FLOOR_RELEVANT_EXPERIENCE = 2            # bullets kept in a JD-relevant job
FLOOR_OTHER_EXPERIENCE = 1               # bullets kept in any other job
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

    @property
    def relevance(self) -> int:
        return sum(c.req_weight for c in self.selected) if self.selected else 0


@dataclass
class ResumePlan:
    template: ParsedTemplate
    entries: dict[tuple[int, int], EntryPlan]
    section_order: list[int]
    entry_order: dict[int, list[int]]
    summary_text: str | None
    kv_items: dict[tuple[int, int], list[str]]
    spacing_level: int = 0
    removed: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    template_items_removed: list[dict[str, Any]] = field(default_factory=list)


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
    _skill("excel", "Excel", "excel|microsoft excel|ms excel", "data", "data"),
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
                  "technologies")),
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
META_SECTION_PATTERN = re.compile(r"legend|interview log|remaining open items|open items",
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
        text = line.text
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


def analyze(jd: JobDescription, master: MasterProfile) -> Analysis:
    requirements, unscored, not_required, warnings = extract_requirements(jd)
    for req in requirements:
        match_requirement(req, master)
    score, breakdown = compute_score(requirements)
    domain = classify_domain(jd, requirements)
    return Analysis(jd=jd, master=master, requirements=requirements, score=score,
                    verdict=verdict_for(score), breakdown=breakdown, domain=domain,
                    unscored=unscored, not_required=not_required,
                    warnings=[*jd.warnings, *warnings])


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


def build_resume_plan(template: ParsedTemplate, master: MasterProfile,
                      analysis: Analysis) -> ResumePlan:
    """Verify every template claim, then rank and select candidate content."""
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
                             "support": check.support})
            continue
        cand = Candidate(section_index=si, entry_index=ei, bullet_index=bullet.index,
                         raw=bullet.raw, plain=bullet.plain, skills=tuple(check.skills),
                         support=check.support, source_lines=check.source_lines)
        _score_candidate(cand, hits, jd_stems)
        candidates.append(cand)
    ranked = sorted(candidates, key=lambda c: (-c.relevance, c.bullet_index))
    selected = ranked[:_section_candidate_limit(kind, len(ranked))]
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
            bullets = [c for c in ep.selected
                       if ("bullet", si, ei, c.bullet_index) not in removed]
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
            for li, kv in enumerate(section.kv_lines):
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
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        return CompileResult(False, completed.returncode, pdf_path, log_path, log,
                             "compiler reported success but produced no PDF")
    return CompileResult(True, completed.returncode, pdf_path, log_path, log, "")


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
    irrelevant = sorted((k for k in optional if plan.entries[k].relevance == 0),
                        key=lambda k: (priority(k[0]), -position[k]))
    for si, ei in irrelevant:
        units.append((("entry", si, ei), "optional entry with no matched JD requirement"))
    scheduled = set(irrelevant)
    protected: set[tuple[int, int]] = set()
    for si in sorted({k[0] for k in optional}):
        remaining = [k for k in optional if k[0] == si and k not in scheduled]
        if remaining:
            protected.add(min(remaining, key=lambda k: (-plan.entries[k].relevance,
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
                queue.append(((cand.relevance, 0, priority(si), -position[(si, ei)], -rank),
                              ("bullet", si, ei, cand.bullet_index),
                              "lowest-value bullet above its entry floor"))
        if ep.optional and (si, ei) not in protected and ep.selected:
            best = max(c.relevance for c in ep.selected)
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


def _unit_is_noop(unit: tuple, removed: set[tuple]) -> bool:
    return unit[0] == "bullet" and ("entry", unit[1], unit[2]) in removed


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


def fit_to_one_page(plan: ResumePlan, tools: ToolSet, build_dir: Path, jobname: str,
                    texinputs: Path,
                    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
                    ) -> FitResult:
    """Compile; trim (then tighten spacing) until one page; then refill.

    Every decision uses the real compiled page count. Trimming removes up to
    BULLETS_REMOVED_PER_ITERATION units per compile. Because that can
    overshoot, the refill pass tries to restore the most valuable removed units
    one at a time (at most REFILL_ATTEMPTS compiles) and keeps each restore that
    still verifies as one page.
    """
    sequence = trim_sequence(plan)
    removed: set[tuple] = set()
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
            break
        if check.pages is None or check.pages <= 1:
            return FitResult(False, tex, log, spacing, iterations, check, result,
                             ["PDF verification failed for reasons other than length: "
                              + "; ".join(check.reasons)], set(removed))
        taken = 0
        while applied < len(sequence) and taken < BULLETS_REMOVED_PER_ITERATION:
            unit, why = sequence[applied]
            applied += 1
            if _unit_is_noop(unit, removed):
                continue
            removed.add(unit)
            order.append(unit)
            log.append({"iteration": iterations, "unit": _describe_unit(plan, unit),
                        "reason": why})
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
    restored_set = set(restored)
    log = [item for item in log if item["unit"] not in restored_set]
    return FitResult(True, tex, log, spacing, iterations, check, result, [], set(removed),
                     restored)


def _describe_unit(plan: ResumePlan, unit: tuple) -> str:
    section = plan.template.sections[unit[1]]
    entry = section.entries[unit[2]]
    name = latex_to_plain(entry.args[0])[:50]
    if unit[0] == "entry":
        return f"entry '{name}' ({section.title})"
    bullet = entry.bullets[unit[3]].plain
    return f"bullet '{bullet[:60]}' from '{name}'"


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
                "bullets": [{"text": c.plain, "relevance": c.relevance,
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


def produce_resume(analysis: Analysis, template: ParsedTemplate, outputs_dir: Path, base: str,
                   tools: ToolSet, dry_run: bool,
                   runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
                   ) -> tuple[dict[str, Any], int]:
    """Plan, render, compile and verify the resume; returns (resume block, exit code)."""
    plan = build_resume_plan(template, analysis.master, analysis)
    names = {kind: f"{base}_resume{suffix}" for kind, suffix in (
        ("tex", ".tex"), ("pdf", ".pdf"), ("log", ".log"), ("failed_tex", ".failed.tex"),
        ("failed_log", ".failed.log"), ("unverified_tex", ".unverified.tex"))}
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
        "rejected_template_bullets": _rejections(plan),
        "template_items_removed": plan.template_items_removed,
        "warnings": plan.warnings,
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
    fit = fit_to_one_page(plan, tools, build_dir, jobname, template.path.parent.resolve(),
                          runner)
    removed_units = fit.final_removed
    block["selected_content"] = _selection_report(plan, removed_units)
    block["fitting"] = {"iterations": fit.iterations, "removed": fit.removed,
                        "restored_after_overshoot": fit.restored,
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
                "5 analysis/compilation/output failure, 6 could not fit exactly one page."))
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
        master_path = find_master(workspace, args.master)
        template_path = args.template or workspace / TEMPLATE_NAME
        if not template_path.is_file():
            raise MissingPrerequisiteError(
                f"Template not found: {template_path}. The pipeline never invents a template.")
        master = parse_master(master_path, strict_no=args.strict_no)
        template = parse_template(template_path)
        analysis = analyze(jd, master)
        outputs_dir = (args.output_dir or workspace / OUTPUT_DIR_NAME).resolve()
        report = build_report(analysis, workspace)
        tools = detect_tools(template, which)
        if args.dry_run:
            base = f"{slugify(jd.company) or 'unknown-company'}_{slugify(jd.role) or 'unknown-role'}"
        else:
            ensure_outputs_dir(outputs_dir)
            base, notes = output_base(jd, outputs_dir)
            report["parsing_warnings"].extend(notes)
        report["output_base"] = base
        exit_code = EXIT_OK if analysis.verdict == VERDICT_PROCEED else EXIT_LOW_FIT
        if analysis.verdict == VERDICT_PROCEED or args.override_low_fit:
            resume, resume_code = produce_resume(analysis, template, outputs_dir, base, tools,
                                                 args.dry_run, runner)
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
        if not args.dry_run:
            report_path = safe_output_path(outputs_dir, f"{base}_analysis.json")
            atomic_write_text(report_path, dump_json(report))
        print_summary(report, outputs_dir, args.verbose, args.dry_run, out)
        return exit_code
    except PipelineError as exc:
        out(f"ERROR: {exc}")
        return exc.exit_code


def print_summary(report: dict[str, Any], outputs_dir: Path, verbose: bool, dry_run: bool,
                  out: Callable[[str], None]) -> None:
    cov = report["evidence_coverage"]
    out(f"{report['company']} | {report['role']}")
    out(f"Fit score: {report['fit_score']} -> {report['verdict']}  "
        f"({cov['matched_requirements']}/{cov['total_requirements']} requirements matched; "
        f"{cov['explicitly_no']} [NO], {cov['not_evidenced']} not evidenced, "
        f"{cov['ambiguous']} ambiguous; confidence {report['score_confidence']})")
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
    if dry_run:
        out("Dry run: nothing written.")
        for entry in resume.get("selected_content", []):
            out(f"  {entry['section']}: {entry['entry'][:60]} "
                f"({len(entry['bullets'])} candidate bullets)")
        return
    out(f"Analysis: {outputs_dir / (report['output_base'] + '_analysis.json')}")
    if status == "verified":
        files = resume["files"]
        out(f"Resume: {outputs_dir / files['pdf']} (1 page verified by "
            f"{resume['verification']['method']}; {resume['fitting']['iterations']} compile "
            f"iteration(s), spacing level {resume['fitting']['spacing_level']})")
    elif status:
        out(f"Resume status: {status}" + (f" - {resume.get('reason')}" if resume.get("reason")
                                         else ""))
        for name in resume.get("files", {}).values():
            out(f"  file: {name}")


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
