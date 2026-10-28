"""Step 9: the reviewed jobs, scored and rebuilt end to end with the real master profile.

Bad outputs (tests/fixtures/bad_outputs/*.pdf, kept locally) are the earlier resumes the
manual reviews rated poorly; the scorer must rate them in the reviewed band. Every fixture
in tests/fixtures/jobs/ is then rebuilt from the resume kit into a temporary directory and
checked for the bullets it must and must not carry, its layout and its scores.

tests/baselines.json holds the scores of the last accepted run. A change may raise a good
resume's fit, never lower it, and may lower a bad output's score, never raise it. After a
deliberate change, rewrite it with JOBAUTO_UPDATE_BASELINES=1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fixtures import JD_PROCEED, MASTER, load_pipeline
from test_pipeline import WorkspaceCase

P = load_pipeline()
ROOT = Path(__file__).resolve().parent.parent
REAL_MASTER = ROOT / "master_profile.md"
JOBS = Path(__file__).resolve().parent / "fixtures" / "jobs"
BAD = Path(__file__).resolve().parent / "fixtures" / "bad_outputs"
BASELINES = Path(__file__).resolve().parent / "baselines.json"

HAVE_KIT = REAL_MASTER.is_file() and P.resume_kit.has_kit(REAL_MASTER.read_text("utf-8")
                                                          .splitlines())
HAVE_PDFTOTEXT = bool(shutil.which("pdftotext"))
HAVE_TEX = bool(shutil.which("pdflatex") and HAVE_PDFTOTEXT)

# name -> (family, resume fit band of each bad output, profile fit target)
SCORED = {
    "cohere_fde_sovereign": ("fde", {"cohere_fde_sovereign": (3.0, 5.0)}, 7.0),
    "stripe_mle_growth": ("ml", {"stripe_mle_growth_v1": (3.0, 5.5),
                                 "stripe_mle_growth_v2": (3.0, 5.5)}, 6.0),
    "affirm_swe2_test_infra": ("test", {"affirm_swe2_test_infra": (3.0, 5.0)}, 7.0),
    "affirm_analyst_early_career": ("ana", {"affirm_analyst_early_career": (4.0, 5.5)}, 8.0),
    "cohere_fde_agentic": ("fde", {}, None),
}
# name -> (kit IDs that must appear, IDs of which one must appear, IDs that never appear,
#          minimum resume fit)
BUILDS = {
    "affirm_analyst_early_career": (("L06", "O04", "M03", "M05"), ("S18", "S20"),
                                    ("K01", "K02", "Z06", "W01", "W02", "W03", "W04"), 7.0),
    "stripe_mle_growth": (("Z01", "Z02", "Z03", "R04", "R06", "C02"), (), ("K01", "K02", "L07"),
                          5.5),
    "affirm_swe2_test_infra": (("S01b", "S06", "S17", "Z08", "R06", "N05"), (),
                               ("L07", "L08", "X01", "X02", "X03", "X04"), 6.5),
    "cohere_fde_sovereign": (("S06", "S17", "S18", "S20", "R04", "R06", "N04"), (), (), 6.5),
    "cohere_fde_agentic": (("R04", "R05", "R06", "C01", "S07", "S09"), (), (), 6.5),
}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                      "sep", "oct", "nov", "dec"), 1)}


def pdf_text(pdf: Path) -> str:
    return subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True,
                          text=True, check=True).stdout


def load_real():
    master = P.parse_master(REAL_MASTER)
    return master, P.load_kit(master)


def analyze(name: str, master, kit):
    return P.analyze(P.read_job_description(JOBS / f"{name}.txt", ROOT), master, kit)


def bad_fit(analysis, pdf: Path, master, kit) -> dict:
    lines = P.resume_lines_from_text(pdf_text(pdf), master, kit, analysis.kit_hooks)
    return P.assess_fit(analysis.coverage, lines)


@unittest.skipUnless(HAVE_KIT and HAVE_PDFTOTEXT and BAD.is_dir() and
                     any(BAD.glob("*.pdf")), "real master, bad outputs or pdftotext absent")
class BadOutputScoringTests(unittest.TestCase):
    """The earlier resumes score in the band their manual review gave them."""

    @classmethod
    def setUpClass(cls):
        cls.master, cls.kit = load_real()
        cls.analyses = {name: analyze(name, cls.master, cls.kit) for name in SCORED}
        cls.bad = {pdf: bad_fit(cls.analyses[name], BAD / f"{pdf}.pdf", cls.master, cls.kit)
                   for name, (_f, bands, _t) in SCORED.items() for pdf in bands
                   if (BAD / f"{pdf}.pdf").is_file()}

    def test_bad_outputs_score_in_the_reviewed_band(self):
        for name, (_family, bands, _target) in SCORED.items():
            for pdf, (low, high) in bands.items():
                if pdf in self.bad:
                    score = self.bad[pdf]["resume_fit"]
                    self.assertTrue(low <= score <= high, (pdf, score, (low, high)))
                    self.assertLessEqual(score, self.bad[pdf]["profile_fit"], pdf)
                    self.assertTrue(self.bad[pdf]["underselling"], pdf)

    def test_profile_fit_near_the_review(self):
        for name, (_family, _bands, target) in SCORED.items():
            if target is None:
                continue
            profile = P.assess_fit(self.analyses[name].coverage, None)["profile_fit"]
            self.assertLessEqual(abs(profile - target), 1.0, (name, profile, target))

    def test_family_from_the_title(self):
        for name, (family, _bands, _target) in SCORED.items():
            self.assertEqual(self.analyses[name].family.code, family, name)

    def test_hard_requirement_flags_are_raised(self):
        flags = {name: P.assess_fit(a.coverage, None) for name, a in self.analyses.items()}
        for name in ("stripe_mle_growth", "affirm_swe2_test_infra"):  # 3+ years asked
            self.assertIn("years: not_met", flags[name]["hard_requirement_flags"], name)
            self.assertEqual(flags[name]["profile_fit_with_hard_requirements"], 4.0, name)
        cohere = flags["cohere_fde_sovereign"]
        self.assertIn("clearance: eligible", cohere["hard_requirement_flags"])
        self.assertEqual(cohere["profile_fit_with_hard_requirements"], cohere["profile_fit"])
        for name in ("stripe_mle_growth", "affirm_analyst_early_career"):  # US roles
            self.assertIn("work_authorization: unknown", flags[name]["hard_requirement_flags"])
            self.assertIn("TN status", flags[name]["work_authorization_note"], name)
        self.assertTrue(flags["affirm_swe2_test_infra"]["work_authorization_note"]
                        .startswith("Canadian citizen"))

    def test_false_friends_never_prove_a_term(self):
        banned = ("stress tested", "Co-op evaluations", "WATonomous", "Autonomous Robotics",
                  "Certificate of Excellence", "Data Analyst")
        for pdf, fit in self.bad.items():
            for row in fit["requirements"]:
                for term in row["terms"]:
                    quote = (term["resume"] or {}).get("quote", "")
                    for text in banned:
                        self.assertNotIn(text, quote, (pdf, term["display"]))

    def test_never_ten_while_a_core_requirement_is_unmet(self):
        for name, analysis in self.analyses.items():
            fit = P.assess_fit(analysis.coverage, None)
            if fit["core_requirement_unmet"]:
                self.assertLessEqual(fit["profile_fit"], 9.0, name)


def parse_month(text: str) -> tuple[int, int]:
    match = re.search(r"([A-Za-z]{3})[a-z]*\.? (\d{4})", text)
    return (int(match.group(2)), MONTHS[match.group(1).lower()]) if match else (0, 0)


@unittest.skipUnless(HAVE_KIT and HAVE_TEX and (ROOT / "template.tex").is_file(),
                     "real master, template or TeX toolchain absent")
class GeneratedResumeTests(unittest.TestCase):
    """Every fixture rebuilt twice from the kit; the second build checks determinism."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.master, cls.kit = load_real()
        cls.builds = {name: cls.build(name, Path(cls._tmp.name) / name) for name in BUILDS}
        cls.rebuilt = {name: cls.build(name, Path(cls._tmp.name) / f"{name}_again")
                       for name in BUILDS}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @staticmethod
    def build(name: str, out: Path) -> dict:
        lines: list[str] = []
        code = P.run([str(JOBS / f"{name}.txt"), "--workspace", str(ROOT), "--output-dir",
                      str(out), "--master", str(REAL_MASTER), "--override-low-fit"],
                     out=lines.append)
        analysis = next(p for p in out.glob("*_analysis.json")
                        if not p.name.endswith("_gap_analysis.json"))
        report = json.loads(analysis.read_text())
        files = report["resume"].get("files") or {}
        tex = (out / files["tex"]).read_text() if files.get("tex") else ""
        pdf = out / files["pdf"] if files.get("pdf") else None
        base = report["output_base"]
        return {"code": code, "lines": lines, "report": report, "tex": tex, "pdf": pdf,
                "text": pdf_text(pdf) if pdf and pdf.is_file() else "",
                "md": (out / f"{base}_report.md").read_text(),
                "job": json.loads((out / f"{base}_job.json").read_text()),
                "requirements": json.loads((out / f"{base}_requirements.json").read_text())}

    # helpers over the rendered .tex
    def ids(self, name: str) -> list[str]:
        return [b["kit_id"] for e in self.builds[name]["report"]["resume"]["selected_content"]
                for b in e["bullets"] if b.get("kit_id")]

    def section(self, name: str, title: str) -> str:
        tex = self.builds[name]["tex"]
        body = tex.split(f"\\section{{{title}}}", 1)[1].split("\\section{", 1)[0]
        return "\n".join(line for line in body.split("\n") if not line.lstrip().startswith("%"))

    def summary(self, name: str) -> str:
        body = re.search(r"\\item \\small\{(.*)\}\s*\\end\{itemize\}", self.section(name, "Summary"),
                         re.S).group(1)
        return P.collapse_ws(P.latex_to_plain(body)).strip()

    def rows(self, name: str) -> list[tuple[str, list[str]]]:
        text = self.section(name, "Technical Skills")
        if "\\section{Additional}" in self.builds[name]["tex"]:
            text += self.section(name, "Additional")
        found = re.findall(r"\\textbf\{([^}]*)\}\{: (.*?)\}", text)
        return [(label, [i.strip() for i in P.latex_to_plain(items).split(",")])
                for label, items in found]

    def skills(self, name: str) -> set[str]:
        return {item for label, items in self.rows(name) if label != "Interests"
                for item in items}

    def interests(self, name: str) -> list[str]:
        return next((items for label, items in self.rows(name) if label == "Interests"), [])

    # every fixture
    def test_every_build_is_verified_with_all_four_gates(self):
        for name, build in self.builds.items():
            resume = build["report"]["resume"]
            self.assertIn(build["code"], (P.EXIT_OK, P.EXIT_LOW_FIT), (name, build["lines"][-5:]))
            self.assertTrue(resume["single_page_verified"], name)
            self.assertEqual({k: v["status"] for k, v in resume["build_gates"].items()},
                             {"gate_1_syntax": "passed", "gate_2_single_page": "passed",
                              "gate_3_ats_readability": "passed", "gate_4_layout": "passed"},
                             name)

    def test_low_fit_override_never_changes_the_verdict(self):
        for name, build in self.builds.items():
            report = build["report"]
            expected = P.EXIT_OK if report["verdict"] == "PROCEED" else P.EXIT_LOW_FIT
            self.assertEqual(build["code"], expected, name)

    def test_no_wraps_and_no_dependency_lint(self):
        for name, build in self.builds.items():
            layout = build["report"]["resume"]["build_gates"]["gate_4_layout"]
            for problem in ("wrapped", "overlaps", "big_gaps"):
                self.assertEqual(layout[problem], [], (name, problem))
            for title, gap in layout["section_gaps_mm"].items():  # "a couple of millimetres"
                self.assertLessEqual(gap, P.SECTION_GAP_MM, (name, title))
            self.assertEqual(build["report"]["fit"]["dependency_lint"], [], name)

    def test_three_bullets_per_experience_newest_first(self):
        for name, build in self.builds.items():
            jobs = [e for e in build["report"]["resume"]["selected_content"]
                    if e["section"] == "Experience"]
            self.assertTrue(jobs, name)
            for entry in jobs:
                self.assertGreaterEqual(len(entry["bullets"]), 3, (name, entry["entry"]))
            starts = [parse_month(d) for d in re.findall(
                r"\\resumeSubheading\s*\{[^}]*\}\{([^}]*)\}", self.section(name, "Experience"))]
            self.assertEqual(starts, sorted(starts, reverse=True), name)

    def test_skills_come_right_after_the_summary(self):
        for name, build in self.builds.items():
            titles = re.findall(r"\\section\{([^}]*)\}", build["tex"])
            self.assertEqual(titles[:2], ["Summary", "Technical Skills"], name)

    def test_no_old_no_or_gap_facts(self):
        allowed = {"V", "R", "TABLE"}
        flagged = [P._alnum(P.latex_to_plain(line.content)) for line in self.master.lines
                   if line.tag in ("OLD", "NO", "GAP") and len(line.content) > 40]
        for name, build in self.builds.items():
            for entry in build["report"]["resume"]["selected_content"]:
                for bullet in entry["bullets"]:
                    tags = {self.master.line(n).tag for n in bullet["source_lines"]}
                    self.assertLessEqual(tags, allowed, (name, bullet["text"]))
            text = P._alnum(build["text"])
            for fact in flagged:
                self.assertNotIn(fact, text, name)
            for marker in ("[OLD]", "[NO]", "[GAP]"):
                self.assertNotIn(marker, build["text"], name)

    def test_copy_has_no_dashes_and_bold_is_tech(self):
        for name, build in self.builds.items():
            items = re.findall(r"\\resumeItem\{(.*)\}", build["tex"])
            summary = self.section(name, "Summary")
            for text in items + [summary]:
                self.assertNotRegex(text, r"--|\u2014|\u2013| - ", name)
            kit_bold = {b for bullet in self.kit.bullets.values() for b in bullet.bold}
            for term in re.findall(r"\\textbf\{([^}]*)\}", summary + "\n".join(items)):
                plain = P.latex_to_plain(term)
                self.assertTrue(P.SKILL_FINDER.keys(plain) or plain in kit_bold, (name, plain))

    def test_rebuilds_are_identical(self):
        for name, build in self.builds.items():
            again = self.rebuilt[name]
            self.assertEqual(build["report"]["resume"]["tex_sha256"],
                             again["report"]["resume"]["tex_sha256"], name)
            self.assertEqual(build["requirements"]["requirements"],
                             again["requirements"]["requirements"], name)
            self.assertEqual(build["report"]["fit"]["resume_fit"],
                             again["report"]["fit"]["resume_fit"], name)

    def test_inclusions_exclusions_and_fit(self):
        for name, (included, one_of, excluded, minimum) in BUILDS.items():
            ids = self.ids(name)
            for kit_id in included:
                self.assertIn(kit_id, ids, name)
            if one_of:
                self.assertTrue(set(one_of) & set(ids), (name, one_of))
            for kit_id in excluded:
                self.assertNotIn(kit_id, ids, name)
            fit = self.builds[name]["report"]["fit"]
            self.assertGreaterEqual(fit["resume_fit"], minimum, name)
            self.assertLessEqual(fit["resume_fit"], fit["profile_fit"], name)
            self.assertFalse(fit["selection_check"]["violation"], name)

    def test_summary_from_the_family_template(self):
        for name, build in self.builds.items():
            resume = build["report"]["resume"]
            family = build["report"]["role_family"]["code"]
            self.assertEqual(resume["summary_source"]["family"], family, name)
            summary = self.summary(name)
            self.assertNotRegex(summary, r"(?:^|[.] )(?:Also\b|[\w ]{0,30}experience:)", name)
            for award in ("IDEAL Scholar", "Cobalt Medallion", "Physics Excellence"):
                self.assertNotIn(award, summary, name)
            self.assertLessEqual(len(summary), P.SUMMARY_MAX_CHARS, name)
            for fact in resume["summary_facts"]:
                self.assertTrue(fact["maps_to"], (name, fact))

    # one job at a time
    def test_affirm_analyst(self):
        name = "affirm_analyst_early_career"
        self.assertTrue(self.summary(name).startswith("Analyst"), self.summary(name))
        label, first = self.rows(name)[0]
        self.assertEqual(label, "Analytics")
        for skill in ("SQL", "pandas", "Power BI", "Power Query", "DAX"):
            self.assertIn(skill, first)
        text = self.builds[name]["text"]
        for claim in ("$200K+", "SAP", "KPI"):
            self.assertIn(claim, text)

    def test_stripe_mle(self):
        name = "stripe_mle_growth"
        self.assertIn("pytest", self.skills(name))
        gaps = {g.lower() for g in self.interests(name)}
        wanted = {"spark", "apache spark", "contextual bandits", "a/b testing", "xgboost"}
        self.assertGreaterEqual(len(gaps & wanted), 2, gaps)

    def test_affirm_test_infra(self):
        name = "affirm_swe2_test_infra"
        for skill in ("Python", "TypeScript", "pytest"):
            self.assertIn(skill, self.skills(name))
        self.assertRegex(self.summary(name), r"\bCI\b|\btests?\b")
        self.assertIn("Playwright", self.interests(name))

    def test_cohere_sovereign_states_the_clearance_line(self):
        self.assertIn("Canadian citizen, willing to obtain clearance", self.summary(
            "cohere_fde_sovereign"))
        self.assertNotIn("clearance", self.summary("cohere_fde_agentic"))  # not asked there

    def test_report_md_has_every_part(self):
        for name, build in self.builds.items():
            md = build["md"]
            for part in ("Role family: **", "Profile fit: **", "Resume fit: **",
                         "**Work authorization:**", "## Hard requirements", "## Requirements",
                         "| ID | Type | W | Requirement | Matched | Bullet | Resume line | Strength |",
                         "## Bullets considered but dropped", "## Top changes"):
                self.assertIn(part, md, name)
            self.assertEqual(build["job"]["family"]["code"],
                             build["report"]["role_family"]["code"], name)

    def test_scores_never_regress_from_the_baselines(self):
        current = {}
        master, kit = self.master, self.kit
        for name in BUILDS:
            fit = self.builds[name]["report"]["fit"]
            analysis = analyze(name, master, kit)
            bad = {pdf: bad_fit(analysis, BAD / f"{pdf}.pdf", master, kit)["resume_fit"]
                   for pdf in SCORED[name][1] if (BAD / f"{pdf}.pdf").is_file()}
            current[name] = {"profile_fit": fit["profile_fit"], "resume_fit": fit["resume_fit"],
                             "bad_outputs": bad}
        if os.environ.get("JOBAUTO_UPDATE_BASELINES") == "1":
            BASELINES.write_text(json.dumps({"updated": date.today().isoformat(),
                                             "fixtures": current}, indent=2) + "\n")
        baselines = json.loads(BASELINES.read_text())["fixtures"]
        for name, now in current.items():
            base = baselines[name]
            self.assertGreaterEqual(now["resume_fit"], base["resume_fit"], name)
            self.assertLessEqual(abs(now["profile_fit"] - base["profile_fit"]), 0.5, name)
            for pdf, score in now["bad_outputs"].items():
                if pdf in base["bad_outputs"]:
                    self.assertLessEqual(score, base["bad_outputs"][pdf], pdf)


@unittest.skipUnless(HAVE_KIT, "real master profile with a resume kit absent")
class RealKitEligibilityTests(unittest.TestCase):
    """The family filter, 9.7 restrictions and alternate wordings on the real kit."""

    @classmethod
    def setUpClass(cls):
        cls.master, cls.kit = load_real()
        cls.restrictions, cls.warnings = P.kit_restrictions(cls.kit)

    def reasons(self, name: str) -> dict[str, str]:
        return P.kit_eligibility(self.kit, analyze(name, self.master, self.kit).family,
                                 self.restrictions)

    def test_restrictions_parse_cleanly(self):
        self.assertEqual(self.warnings, [])

    def test_analyst_leaves_out_hardware_and_robotics(self):
        reasons = self.reasons("affirm_analyst_early_career")
        for kit_id in ("K01", "K02", "W01", "W02", "W03", "W04"):
            self.assertIn(kit_id, reasons)
        for kit_id in ("L06", "O04", "M03", "M05"):
            self.assertNotIn(kit_id, reasons)

    def test_test_family_takes_the_test_wording(self):
        reasons = self.reasons("affirm_swe2_test_infra")
        self.assertNotIn("S01b", reasons)
        self.assertIn("alternate wording of S01b", reasons.get("S01", ""))
        for kit_id in ("L07", "L08"):
            self.assertIn(kit_id, reasons)

    def test_ml_family_leaves_out_operations_bullets(self):
        reasons = self.reasons("stripe_mle_growth")
        for kit_id in ("K01", "K02", "L07"):
            self.assertIn(kit_id, reasons)


class ScorerUnitTests(unittest.TestCase):
    """Look-alikes that must not count, and the parsing rules behind the scores."""

    def asks(self, text: str) -> set[str]:
        return {c.key for c in P._jd_concepts(text)}

    def test_excellence_is_not_excel(self):
        self.assertEqual(P.SKILL_FINDER.keys("Certificate of Excellence, Physics Excellence"), [])
        self.assertEqual(P.SKILL_FINDER.keys("An excellent communicator"), [])
        self.assertEqual(P.SKILL_FINDER.keys("Advanced Excel and SQL"), ["excel", "sql"])

    def test_the_data_analyst_title_is_not_data_analysis(self):
        self.assertNotIn("data_analysis", self.asks("Work as a Data Analyst on the team"))
        self.assertFalse(P.concept_proven(P.CONCEPT_INDEX["data_analysis"],
                                          "Data Analyst co-op at Example Co")[0])
        self.assertIn("data_analysis", self.asks("Perform data analysis to drive insights"))

    def test_years_lines_score_only_topics_asked_nowhere_else(self):
        self.assertTrue(P.PURE_YEARS_LINE.match("3+ years of experience in test automation."))
        self.assertFalse(P.PURE_YEARS_LINE.match("3+ years of experience with Python, and SQL"))
        jd = ("# Test Engineer at Example Co\n\n## Requirements\n"
              "- 3+ years of experience in test automation\n"
              "- 5+ years of experience with Kubernetes\n"
              "- Build and maintain test automation frameworks\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.md"
            path.write_text(jd, encoding="utf-8")
            (Path(tmp) / "master.md").write_text(MASTER, encoding="utf-8")
            model = P.build_coverage_model(P.read_job_description(path, Path(tmp)),
                                           P.parse_master(Path(tmp) / "master.md"))
        texts = [r.text for r in model.requirements]
        self.assertNotIn("3+ years of experience in test automation", texts)  # asked again
        self.assertIn("5+ years of experience with Kubernetes", texts)  # its only ask
        self.assertEqual([r.id for r in model.requirements],
                         [f"R{i:02d}" for i in range(1, len(texts) + 1)])

    def test_concept_alternatives_become_one_any_of_term(self):
        concepts = P._jd_concepts("Experience in consumer lending, fintech, or financial services")
        groups = P._concept_alternatives(
            "Experience in consumer lending, fintech, or financial services", concepts)
        self.assertEqual(len(groups), 1)
        self.assertGreaterEqual(len(groups[0]), 2)
        self.assertEqual(P._concept_alternatives("Consumer lending and financial services",
                                                 P._jd_concepts(
                                                     "Consumer lending and financial services")),
                         [])  # "and": every one is needed

    def test_probe_log_lines(self):
        log = ("JOBAUTO-W:S01:300.0pt:310.5pt:0\n"   # narrower than the line
               "JOBAUTO-W:S02:312.0pt:310.5pt:3\n"   # a tiny squeeze stays on one line
               "JOBAUTO-W:S03:320.0pt:310.5pt:100\n"  # too wide: it wraps
               "JOBAUTO-W:row.1:200pt:400pt:0\n")
        self.assertEqual(P.read_probe(log), {"S01": 1, "S02": 1, "S03": 2, "row.1": 1})
        self.assertEqual(P.read_probe("no marks here"), {})


BBOX = """<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
<page width="612.0" height="792.0">
{lines}
</page></doc></body></html>"""


def bbox_line(y: float, text: str, height: float = 10.0, x: float = 40.0) -> str:
    words = "".join(f'<word xMin="{x}" yMin="{y}" xMax="{x + 50}" yMax="{y + height}">{w}</word>'
                    for w in text.split())
    return (f'<flow><block><line xMin="{x}" yMin="{y}" xMax="{x + 500}" yMax="{y + height}">'
            f"{words}</line></block></flow>")


class LayoutCheckTests(WorkspaceCase):
    """Gate 4 on synthetic pdftotext output: wraps, overlaps and blank bands."""

    def plan(self):
        analysis = self.analysis(JD_PROCEED)
        return P.build_resume_plan(P.parse_template(self.root / "template.tex"),
                                   analysis.master, analysis)

    def check(self, plan, layout_text: str, lines: list[str]):
        def runner(cmd, **_kw):
            out = BBOX.format(lines="\n".join(lines)) if "-bbox-layout" in cmd else layout_text
            return subprocess.CompletedProcess(cmd, 0, out, "")
        tools = P.ToolSet("pdflatex", "/bin/true", "pdftotext", None, "pdflatex", "test")
        return P.layout_check(Path("x.pdf"), tools, plan, set(), runner)

    def bullets(self, plan) -> list[str]:
        return [c.plain for _si, entries in P.active_model(plan, set())
                for _ei, cands in entries for c in cands]

    def test_one_line_bullets_pass_and_a_wrap_fails(self):
        plan = self.plan()
        bullets = self.bullets(plan)
        self.assertTrue(bullets)
        lines = [bbox_line(40 + 12 * i, b[:30]) for i, b in enumerate(bullets)]
        fine = self.check(plan, "\n".join(bullets), lines)
        self.assertTrue(fine.ok, fine.reasons)
        first = bullets[0].split()
        broken = [" ".join(first[:3]), " ".join(first[3:])] + bullets[1:]
        wrapped = self.check(plan, "\n".join(broken), lines)
        self.assertFalse(wrapped.ok)
        self.assertEqual(wrapped.wrapped, [bullets[0]])

    def test_overlapping_text_and_blank_bands_fail(self):
        plan = self.plan()
        layout = "\n".join(self.bullets(plan))
        overlap = self.check(plan, layout, [bbox_line(40, "first line"),
                                            bbox_line(44, "second line")])
        self.assertTrue(overlap.overlaps)
        gap = self.check(plan, layout, [bbox_line(40, "first line"),
                                        bbox_line(40 + 10 + 12 * P.MM, "after a gap")])
        self.assertTrue(gap.big_gaps)
        self.assertFalse(gap.ok)


if __name__ == "__main__":
    unittest.main()
