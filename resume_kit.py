"""The resume kit: section 9 of the master profile, parsed into typed objects.

Standard library only, with no dependency on pipeline.py, so the Studio can
validate a profile edit with it. ``parse_kit`` raises ``KitError`` listing every
structural problem; ``pipeline.load_kit`` turns that into exit code 4 and then
audits each piece of wording against the [V]/[R] facts in sections 1 to 8.

Section map (headings ``### 9.N``):
    9.1 role families      9.2 ranked proof points   9.3 summary per family
    9.4 skills rows        9.5 bullet bank           9.6 synonym map
    9.7 inclusion rules    9.8 gap lines             9.9 formatting rules
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FAMILY_CODES = ("dev", "swe", "ml", "fde", "ana", "test", "qa", "ops")
ALL_FAMILIES = "all"
KIT_HEADING = re.compile(r"^##\s+9\.\s+Resume kit\b", re.IGNORECASE)
SUBSECTION = re.compile(r"^###\s+9\.(\d+)\b")
REQUIRED_SUBSECTIONS = {1: "role families", 3: "summaries by family", 4: "skills rows",
                        5: "bullet bank"}
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_RANGE = re.compile(rf"\b{MONTH} \d{{4}} to (?:{MONTH} \d{{4}}|present|now)\b", re.IGNORECASE)
BULLET_ID = re.compile(r"\b[A-Z]\d{2}[a-z]?\b")
ID_RANGE = re.compile(r"\b([A-Z])(\d{2}) to \1(\d{2})\b")
NEW_MARKER = re.compile(r"\s*\(\*new\*\)\s*$", re.IGNORECASE)
ALTERNATE = re.compile(r"\balt(?:ernate)? wording of ([A-Z]\d{2}[a-z]?)\b", re.IGNORECASE)
# A bullet that leans on the one before it: "It ...", "This ...", or in a role "Connected it
# ..." (under a project header "Covered it with tests" means the project itself).
DANGLING = re.compile(r"^(?:it|this|that|these|those|they|its|their)\b", re.IGNORECASE)
DANGLING_IN_ROLE = re.compile(r"^\w+ (?:it|them)\b", re.IGNORECASE)
BOLD = re.compile(r"\*\*(.+?)\*\*")


class KitError(Exception):
    """The resume kit is missing or malformed; ``problems`` lists every issue."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class FamilyRef:
    code: str
    condition: str = ""   # "hardware roles", "frontend", "health": the bullet suits only those


@dataclass
class KitBullet:
    id: str
    text: str                       # Markdown; **bold** marks tech
    themes: tuple[str, ...]
    families: tuple[FamilyRef, ...]
    impact: int
    keep_with: str | None
    group: str                      # KitGroup.key
    line: int
    new: bool = False               # not yet compiled at one line: wrap-checked first
    alternate_of: str | None = None  # another wording of that bullet (pick one, never both)

    @property
    def plain(self) -> str:
        return BOLD.sub(r"\1", self.text)

    @property
    def bold(self) -> list[str]:
        return BOLD.findall(self.text)


@dataclass
class KitGroup:
    key: str
    header: str
    kind: str                       # experience | project | research | leadership
    name: str                       # company, or project name
    title: str = ""
    location: str = ""
    dates: str = ""
    note: str = ""                  # text after the bold header ("Hardware and robotics roles only.")
    line: int = 0
    bullets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SynonymRow:
    fact: str
    bullet_ids: tuple[str, ...]
    words: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class RowSpec:
    row: str
    only: tuple[str, ...] = ()      # "Backend (Python, SQL)": just these items
    light: bool = False             # "AI and ML (light)": job terms plus a few


@dataclass(frozen=True)
class KitRule:
    subject: str
    ids: tuple[str, ...]
    text: str
    line: int


@dataclass
class ResumeKit:
    heading_line: int
    families: dict[str, tuple[str, tuple[str, ...]]]   # code -> (name, typical titles)
    proof_points: list[tuple[int, str]]
    summaries: dict[str, tuple[int, str]]               # code -> (line, Markdown text)
    rows: dict[str, tuple[int, tuple[str, ...]]]         # row name -> (line, items)
    row_order: dict[str, tuple[RowSpec, ...]]
    guard: str
    groups: list[KitGroup]
    bullets: dict[str, KitBullet]                       # bank order
    synonyms: list[SynonymRow]
    rules: list[KitRule]
    gaps: dict[str, tuple[int, tuple[str, ...]]]        # code -> (line, items)
    formatting: list[str]
    warnings: list[str] = field(default_factory=list)

    def group(self, key: str) -> KitGroup:
        return next(g for g in self.groups if g.key == key)

    @property
    def new_bullets(self) -> list[str]:
        return [b.id for b in self.bullets.values() if b.new]


def has_kit(lines: list[str]) -> bool:
    return any(KIT_HEADING.match(line) for line in lines)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c) and any(cells)


def split_outside_parens(text: str, separator: str = ",") -> list[str]:
    """'AWS (EC2, S3), Docker' -> ['AWS (EC2, S3)', 'Docker']."""
    parts, depth, current = [], 0, []
    for char in text:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def _code(text: str) -> str:
    return text.strip().strip("`").strip().lower()


def _tables(block: list[tuple[int, str]]) -> list[tuple[int, list[str], list[tuple[int, list[str]]]]]:
    """(line before the table, header cells, rows) for each Markdown table in a block."""
    tables = []
    index = 0
    while index < len(block):
        number, text = block[index]
        if text.strip().startswith("|") and index + 1 < len(block) and \
                _is_separator(_cells(block[index + 1][1])):
            header = [h.lower() for h in _cells(text)]
            rows = []
            index += 2
            while index < len(block) and block[index][1].strip().startswith("|"):
                rows.append((block[index][0], _cells(block[index][1])))
                index += 1
            tables.append((number, header, rows))
            continue
        index += 1
    return tables


def _column(header: list[str], *names: str) -> int | None:
    for i, cell in enumerate(header):
        if any(cell.startswith(name) for name in names):
            return i
    return None


def _expand_ids(text: str) -> list[str]:
    ids = []
    for match in ID_RANGE.finditer(text):
        letter, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        ids += [f"{letter}{n:02d}" for n in range(start, end + 1)]
    ids += BULLET_ID.findall(ID_RANGE.sub(" ", text))
    return list(dict.fromkeys(ids))


def _parse_header(header: str, note: str, number: int) -> KitGroup:
    text = header.strip().rstrip(".").strip()
    key = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    dates = DATE_RANGE.search(text)
    if dates:
        parts = split_outside_parens(text[:dates.start()].rstrip(", "))
        return KitGroup(key, text, "experience", parts[0] if parts else "",
                        parts[1] if len(parts) > 1 else "", ", ".join(parts[2:]),
                        dates.group(0), note, number)
    name = re.split(r"\s*[,(]", text, maxsplit=1)[0].strip()
    kind = {"research": "research", "leadership": "leadership"}.get(name.lower(), "project")
    return KitGroup(key, text, kind, name, note=note, line=number)


def parse_kit(raw_lines: list[str]) -> ResumeKit:
    """Parse section 9 (1-based line numbers); raise KitError listing every problem."""
    start = next((i for i, line in enumerate(raw_lines) if KIT_HEADING.match(line)), None)
    if start is None:
        raise KitError(["no '## 9. Resume kit' section"])
    end = next((i for i in range(start + 1, len(raw_lines))
                if re.match(r"^##\s", raw_lines[i])), len(raw_lines))
    subsections: dict[int, list[tuple[int, str]]] = {}
    current: int | None = None
    for i in range(start + 1, end):
        match = SUBSECTION.match(raw_lines[i])
        if match:
            current = int(match.group(1))
            subsections[current] = []
        elif current is not None:
            subsections[current].append((i + 1, raw_lines[i]))
    problems = [f"section 9.{n} ({name}) is missing" for n, name in REQUIRED_SUBSECTIONS.items()
                if n not in subsections]
    warnings: list[str] = []
    if problems:
        raise KitError(problems)

    families: dict[str, tuple[str, tuple[str, ...]]] = {}
    for _n, header, rows in _tables(subsections[1]):
        code_i, name_i = _column(header, "code"), _column(header, "family")
        titles_i = _column(header, "typical")
        for number, cells in rows:
            code = _code(cells[code_i or 0])
            if code not in FAMILY_CODES:
                problems.append(f"line {number}: unknown family code '{code}' (use "
                                f"{', '.join(FAMILY_CODES)})")
                continue
            titles = tuple(t.strip() for t in cells[titles_i].split(",")) if titles_i else ()
            families[code] = (cells[name_i] if name_i is not None else code, titles)
    missing = [c for c in FAMILY_CODES if c not in families]
    if missing:
        problems.append(f"9.1 does not define the families {', '.join(missing)}")
    valid = set(FAMILY_CODES)

    proof_points = [(n, re.sub(r"^\d+\.\s*", "", t.strip())) for n, t in subsections.get(2, [])
                    if re.match(r"^\s*\d+\.\s", t)]

    summaries: dict[str, tuple[int, str]] = {}
    for number, text in subsections[3]:
        match = re.match(r"^\s*-\s*\*\*`?([a-z]+)`?:?\*\*:?\s*(.+)$", text)
        if not match:
            continue
        code = match.group(1).lower()
        if code not in valid:
            problems.append(f"line {number}: summary for unknown family '{code}'")
        summaries[code] = (number, match.group(2).strip())

    rows: dict[str, tuple[int, tuple[str, ...]]] = {}
    row_order: dict[str, tuple[RowSpec, ...]] = {}
    guard = ""
    for number, text in subsections[4]:
        if re.match(r"^\s*\*\*Guard:?\*\*", text):
            guard = BOLD.sub("", text).strip(" :")
    for _n, header, table_rows in _tables(subsections[4]):
        if header and header[0].startswith("row"):
            for number, cells in table_rows:
                rows[cells[0]] = (number, tuple(split_outside_parens(cells[1])))
        elif header and header[0].startswith("family"):
            for number, cells in table_rows:
                code = _code(cells[0])
                if code not in valid:
                    problems.append(f"line {number}: row order for unknown family '{code}'")
                    continue
                specs = []
                for part in split_outside_parens(cells[1]):
                    match = re.match(r"^(.*?)\s*\((.*)\)\s*$", part)
                    name, inner = (match.group(1), match.group(2)) if match else (part, "")
                    light = inner.strip().lower() == "light"
                    only = () if light or not inner else tuple(split_outside_parens(inner))
                    specs.append(RowSpec(name.strip(), only, light))
                row_order[code] = tuple(specs)
    for code, specs in row_order.items():
        for spec in specs:
            if spec.row not in rows:
                problems.append(f"9.4 row order for '{code}' names '{spec.row}', which is not in "
                                f"the row library")
            else:
                library = rows[spec.row][1]
                problems += [f"9.4 row order for '{code}': '{item}' is not in the "
                             f"'{spec.row}' row" for item in spec.only if item not in library]

    groups: list[KitGroup] = []
    bullets: dict[str, KitBullet] = {}
    block = subsections[5]
    for index, (number, text) in enumerate(block):
        match = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", text.strip())
        if not match or match.group(1).rstrip(":").lower().startswith("how to read"):
            continue
        following = next((t for _n, t in block[index + 1:] if t.strip()), "")
        if not following.strip().startswith("|"):
            continue
        groups.append(_parse_header(match.group(1), match.group(2).strip(), number))
    table_owner: dict[int, KitGroup] = {}
    for group in groups:
        table_owner[group.line] = group
    for table_line, header, table_rows in _tables(block):
        owner = max((g for g in groups if g.line < table_line), key=lambda g: g.line,
                    default=None)
        if owner is None:
            problems.append(f"line {table_line}: a bullet table has no group header above it")
            continue
        cols = {name: _column(header, name) for name in ("id", "bullet", "themes", "families",
                                                          "impact", "keep")}
        if any(cols[name] is None for name in ("id", "bullet", "families", "impact")):
            problems.append(f"line {table_line}: bullet table needs ID, Bullet, Families and "
                            f"Impact columns")
            continue
        for number, cells in table_rows:
            def cell(name: str) -> str:
                i = cols[name]
                return cells[i].strip() if i is not None and i < len(cells) else ""
            bid = cell("id")
            if not re.fullmatch(r"[A-Z]\d{2}[a-z]?", bid):
                problems.append(f"line {number}: '{bid}' is not a bullet ID like S01 or S01b")
                continue
            if bid in bullets:
                problems.append(f"line {number}: duplicate bullet ID {bid} (first on line "
                                f"{bullets[bid].line})")
                continue
            text = cell("bullet")
            new = bool(NEW_MARKER.search(text))
            text = NEW_MARKER.sub("", text).strip()
            themes_raw = cell("themes")
            alternate = ALTERNATE.search(themes_raw)
            themes = tuple(t.strip().lower() for t in split_outside_parens(
                re.sub(r"\(.*?\)", "", themes_raw)) if t.strip())
            refs = []
            for part in split_outside_parens(cell("families")):
                match = re.match(r"^`?([a-z]+)`?\s*(?:\((.*)\))?$", part.strip(), re.IGNORECASE)
                code = match.group(1).lower() if match else part
                if code != ALL_FAMILIES and code not in valid:
                    problems.append(f"line {number}: {bid} names unknown family '{part}'")
                    continue
                refs.append(FamilyRef(code, (match.group(2) or "").strip().lower()
                                      if match else ""))
            try:
                impact = int(cell("impact"))
            except ValueError:
                impact = 0
            if not 1 <= impact <= 5:
                problems.append(f"line {number}: {bid} impact must be 1 to 5, not "
                                f"'{cell('impact')}'")
            keep = cell("keep") or None
            bullet = KitBullet(bid, text, themes, tuple(refs), impact, keep, owner.key, number,
                               new, alternate.group(1) if alternate else None)
            if not text:
                problems.append(f"line {number}: {bid} has no text")
            elif keep is None and (DANGLING.match(bullet.plain) or (
                    owner.kind == "experience" and DANGLING_IN_ROLE.match(bullet.plain))):
                problems.append(f"line {number}: {bid} starts with a dangling pronoun "
                                f"('{bullet.plain.split()[0]} ...') but names no Keep with bullet")
            bullets[bid] = bullet
            owner.bullets.append(bid)
    for bullet in bullets.values():
        if bullet.keep_with is not None:
            target = bullets.get(bullet.keep_with)
            if target is None:
                problems.append(f"line {bullet.line}: {bullet.id} keeps with "
                                f"{bullet.keep_with}, which does not exist")
            elif target.group != bullet.group:
                problems.append(f"line {bullet.line}: {bullet.id} keeps with {bullet.keep_with} "
                                f"from another role or project")
        if bullet.alternate_of is not None and bullet.alternate_of not in bullets:
            problems.append(f"line {bullet.line}: {bullet.id} is an alternate of "
                            f"{bullet.alternate_of}, which does not exist")
    for group in groups:
        if not group.bullets:
            problems.append(f"line {group.line}: '{group.header}' has no bullets")
        if group.kind == "experience":
            for part in ("name", "title", "location", "dates"):
                if not getattr(group, part):
                    problems.append(f"line {group.line}: role header '{group.header}' needs a "
                                    f"company, title, location and dates ('{part}' missing)")
        elif not group.name:
            problems.append(f"line {group.line}: project header has no name")
    if not bullets:
        problems.append("9.5 has no bullets")

    synonyms: list[SynonymRow] = []
    for _n, header, table_rows in _tables(subsections.get(6, [])):
        for number, cells in table_rows:
            if len(cells) < 2:
                continue
            ids = _expand_ids(cells[0])
            unknown = [i for i in ids if i not in bullets]
            if unknown:
                problems.append(f"line {number}: synonym row names unknown bullets "
                                f"{', '.join(unknown)}")
            words = tuple(w.strip() for w in split_outside_parens(cells[1]) if w.strip())
            synonyms.append(SynonymRow(cells[0], tuple(i for i in ids if i in bullets), words,
                                       number))

    rules: list[KitRule] = []
    for number, text in subsections.get(7, []):
        match = re.match(r"^\s*-\s*\*\*(.+?):?\*\*:?\s*(.+)$", text)
        if match:
            subject = match.group(1).strip().rstrip(":")
            rules.append(KitRule(subject, tuple(_expand_ids(subject)), match.group(2).strip(),
                                 number))

    gaps: dict[str, tuple[int, tuple[str, ...]]] = {}
    for _n, header, table_rows in _tables(subsections.get(8, [])):
        for number, cells in table_rows:
            code = _code(cells[0])
            if code not in valid:
                problems.append(f"line {number}: gap line for unknown family '{code}'")
                continue
            gaps[code] = (number, tuple(split_outside_parens(cells[1])))
    formatting = [BOLD.sub(r"\1", t.strip().lstrip("- ")) for _n, t in subsections.get(9, [])
                  if t.strip().startswith("-")]
    for number, name in ((2, "proof points"), (6, "synonym map"), (7, "inclusion rules"),
                         (8, "gap lines"), (9, "formatting rules")):
        if number not in subsections:
            warnings.append(f"section 9.{number} ({name}) is missing")
    missing_summaries = [c for c in FAMILY_CODES if c not in summaries]
    if missing_summaries:
        warnings.append(f"no 9.3 summary for {', '.join(missing_summaries)}")
    if problems:
        raise KitError(problems)
    return ResumeKit(start + 1, families, proof_points, summaries, rows, row_order, guard,
                     groups, bullets, synonyms, rules, gaps, formatting, warnings)
