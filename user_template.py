"""Per-account resume templates for Pro builds.

The owner's template.tex carries the owner's own header, roles and projects, so it can
never be used for anyone else. This module builds a template for one account from the
shared formatting base (resume_base.tex) plus:

- a header the account enters (name, email, phone, location, links) and its education;
- the roles, projects, research, leadership, skills rows and summary of the resume kit
  in that account's own master profile (section 9).

The template only carries structure and candidate wording. pipeline.py verifies every
header, bullet and skills item against the same account's master profile, exactly as it
does for the owner's template, so nothing here can add a claim. Standard library only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pipeline
import resume_kit

BASE = Path(__file__).resolve().parent / "resume_base.tex"
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
URL = re.compile(r"^https?://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~/-]*)?$")  # no query strings
PHONE = re.compile(r"^[0-9+() .-]{7,25}$")
MAX_FIELD = 120
MAX_LINKS = 4
MAX_EDUCATION = 3
SECTIONS = (("experience", "Experience"), ("project", "Projects"), ("research", "Research"),
            ("leadership", "Leadership"))


class TemplateInputError(ValueError):
    """The header details cannot be put on a resume as given."""


def _text(value: Any, label: str, required: bool = False) -> str:
    text = pipeline.collapse_ws(str(value or ""))
    if required and not text:
        raise TemplateInputError(f"Add your {label}.")
    if len(text) > MAX_FIELD:
        raise TemplateInputError(f"Keep your {label} under {MAX_FIELD} characters.")
    return text


def clean_header(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate what an account enters for its resume header and education."""
    header = {"name": _text(raw.get("name"), "name", required=True),
              "email": _text(raw.get("email"), "email"),
              "phone": _text(raw.get("phone"), "phone number"),
              "location": _text(raw.get("location"), "location"),
              "links": [], "education": []}
    if header["email"] and not EMAIL.match(header["email"]):
        raise TemplateInputError("Enter a valid email address for your resume.")
    if header["phone"] and not PHONE.match(header["phone"]):
        raise TemplateInputError("Use digits, spaces, +, - and brackets in your phone number.")
    links = [pipeline.collapse_ws(str(link)) for link in raw.get("links") or [] if str(link).strip()]
    if len(links) > MAX_LINKS:
        raise TemplateInputError(f"Add at most {MAX_LINKS} links.")
    for link in links:
        if not link.startswith(("http://", "https://")):
            link = "https://" + link
        if not URL.match(link) or len(link) > MAX_FIELD:
            raise TemplateInputError(f"'{link}' is not a link the resume can show (use a plain "
                                     f"https:// address without ? or #).")
        header["links"].append(link)
    for item in (raw.get("education") or [])[:MAX_EDUCATION + 1]:
        entry = {key: _text(item.get(key), f"education {key}") for key in
                 ("school", "degree", "dates", "location")}
        if any(entry.values()):
            if not entry["school"]:
                raise TemplateInputError("Each education entry needs a school.")
            header["education"].append(entry)
    if len(header["education"]) > MAX_EDUCATION:
        raise TemplateInputError(f"Add at most {MAX_EDUCATION} education entries.")
    return header


def _esc(text: str) -> str:
    return pipeline.latex_escape(pipeline.collapse_ws(text))


def _dates(text: str) -> str:
    return _esc(re.sub(r"\s+(?:to|-|\u2013|\u2014)\s+", " -- ", text)).replace(r"-{}-", "--")


def header_latex(header: dict[str, Any]) -> str:
    parts = []
    if header["phone"]:
        parts.append(_esc(header["phone"]))
    if header["email"]:
        parts.append(f"\\href{{mailto:{header['email']}}}{{\\underline{{{_esc(header['email'])}}}}}")
    for link in header["links"]:
        shown = re.sub(r"^https?://(?:www\.)?", "", link).rstrip("/")
        parts.append(f"\\href{{{link}}}{{\\underline{{{_esc(shown)}}}}}")
    if header["location"]:
        parts.append(f"\\small {_esc(header['location'])}")
    contact = (" \\small " + " $|$\n    ".join(parts)) if parts else ""
    return ("%-----------HEADER-----------\n\\begin{center}\n"
            f"    \\textbf{{\\Huge \\scshape {_esc(header['name'])}}} \\\\ \\vspace{{1pt}}\n"
            f"   {contact}\n\\end{{center}}\n")


def _items(lines: list[str]) -> str:
    return ("        \\resumeItemListStart\n"
            + "".join(f"            \\resumeItem{{{line}}}\n" for line in lines)
            + "        \\resumeItemListEnd\n")


def _stack(kit: resume_kit.ResumeKit, group: resume_kit.KitGroup) -> list[str]:
    seen: dict[str, None] = {}
    for bid in group.bullets:
        for term in kit.bullets[bid].bold:
            seen.setdefault(term, None)
    return list(seen)[:7]


def _entry(kit: resume_kit.ResumeKit, group: resume_kit.KitGroup) -> str:
    bullets = _items([pipeline.kit_latex(kit.bullets[bid].text) for bid in group.bullets])
    if group.kind in ("experience", "leadership"):
        return ("    \\resumeSubheading\n"
                f"        {{{_esc(group.name)}}}{{{_dates(group.dates)}}}\n"
                f"        {{{_esc(group.title)}}}{{{_esc(group.location)}}}\n" + bullets)
    stack = _stack(kit, group)
    label = f"\\textbf{{{_esc(group.name)}}}"
    if stack:
        label += " $|$ \\emph{" + ", ".join(_esc(term) for term in stack) + "}"
    return f"    \\resumeProjectHeading\n        {{{label}}}{{{_dates(group.dates)}}}\n" + bullets


def build_template(header: dict[str, Any], kit: resume_kit.ResumeKit,
                   base: str | None = None) -> str:
    """The full template.tex for one account (header already passed through clean_header)."""
    preamble = (base if base is not None else BASE.read_text(encoding="utf-8")).rstrip("\n")
    if not preamble.endswith("\\begin{document}"):
        raise TemplateInputError("resume_base.tex must end with \\begin{document}")
    body = [header_latex(header)]
    summary = next(iter(kit.summaries.values()), None)
    if summary:
        body.append("%-----------SUMMARY-----------\n\\section{Summary}\n"
                    "\\begin{itemize}[leftmargin=0.15in, label={}]\n    \\item \\small{\n"
                    f"    {pipeline.kit_latex(summary[1])}\n    }}\n\\end{{itemize}}\n")
    if kit.rows:
        rows = " \\\\\n".join(f"        \\textbf{{{_esc(name)}}}{{: "
                              f"{', '.join(_esc(item) for item in items)}}}"
                              for name, (_line, items) in kit.rows.items())
        body.append("%-----------SKILLS-----------\n\\section{Technical Skills}\n"
                    "\\begin{itemize}[leftmargin=0.15in, label={}]\n    \\item \\small{\n"
                    f"{rows}\n    }}\n\\end{{itemize}}\n")
    for kind, title in SECTIONS:
        groups = [g for g in kit.groups if g.kind == kind and g.bullets]
        if groups:
            body.append(f"%-----------{title.upper()}-----------\n\\section{{{title}}}\n"
                        "\\resumeSubHeadingListStart\n"
                        + "".join(_entry(kit, g) for g in groups)
                        + "\\resumeSubHeadingListEnd\n")
    if header["education"]:
        body.append("%-----------EDUCATION-----------\n\\section{Education}\n"
                    "\\resumeSubHeadingListStart\n"
                    + "".join("    \\resumeSubheading\n"
                              f"        {{{_esc(e['school'])}}}{{{_dates(e['dates'])}}}\n"
                              f"        {{{_esc(e['degree'])}}}{{{_esc(e['location'])}}}\n"
                              for e in header["education"])
                    + "\\resumeSubHeadingListEnd\n")
    gaps = next(iter(kit.gaps.values()), None)
    if gaps and gaps[1]:
        body.append("%-----------ADDITIONAL-----------\n\\section{Additional}\n"
                    "\\begin{itemize}[leftmargin=0.15in, label={}]\n    \\item \\small{\n"
                    f"        \\textbf{{Interests}}{{: {', '.join(_esc(g) for g in gaps[1])}}}\n"
                    "    }\n\\end{itemize}\n")
    return preamble + "\n\n" + "\n".join(body) + "\n\\end{document}\n"
