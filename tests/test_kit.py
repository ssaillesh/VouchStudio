"""The resume kit (section 9 of the master profile) and the kit-driven engine.

Synthetic fixtures run everywhere. Tests against the real career bank and the
regression job descriptions run only when those local files exist.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fixtures import MASTER, MASTER_KIT, FakeTex, fake_which, load_pipeline, make_workspace

P = load_pipeline()


class KitCase(unittest.TestCase):
    master_text = MASTER_KIT

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name), master=self.master_text)
        self.outputs = self.root / "outputs"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args, runner=None):
        lines: list[str] = []
        code = P.run([*args, "--workspace", str(self.root), "--output-dir", str(self.outputs)],
                     which=fake_which(), runner=runner or FakeTex(), out=lines.append)
        return code, lines

    def report(self) -> dict:
        return json.loads((self.outputs / "example-co_platform-engineer_analysis.json")
                          .read_text(encoding="utf-8"))


class StepZeroTests(KitCase):
    """Why identical output could happen, and the guards against it."""

    def test_startup_logs_the_absolute_path_and_file_sha256(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_OK, "\n".join(lines))
        master = (self.root / "master.md").resolve()
        digest = hashlib.sha256(master.read_bytes()).hexdigest()  # same as `shasum -a 256`
        self.assertEqual(lines[0], f"Master profile: {master} (sha256 {digest})")
        inputs = self.report()["inputs"]
        self.assertEqual(inputs["master_profile_path"], str(master))
        self.assertEqual(inputs["master_profile_file_sha256"], digest)
        self.assertEqual(inputs["resume_kit"], "used")

    def test_a_master_without_the_resume_kit_fails_loudly(self):
        (self.root / "master.md").write_text(MASTER, encoding="utf-8")
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_MISSING_PREREQ)
        self.assertIn("has no '## 9. Resume kit' section", lines[-1])
        self.assertFalse(self.outputs.exists())  # nothing written
        code, _lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"), "--no-kit")
        self.assertEqual(code, P.EXIT_OK)  # legacy mode is an explicit choice

    def test_no_cache_removes_the_previous_outputs_first(self):
        self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        stale = self.outputs / "example-co_platform-engineer_resume.failed.tex"
        stale.write_text("old attempt", encoding="utf-8")
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"), "--no-cache")
        self.assertEqual(code, P.EXIT_OK)
        self.assertTrue(any(l.startswith("Cache: --no-cache removed ") for l in lines), lines)
        self.assertFalse(stale.exists())
        self.assertTrue(self.report()["inputs"]["no_cache"])

    def test_a_kept_previous_pdf_is_reported_as_a_cache_hit(self):
        self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   runner=FakeTex(fail=True))
        self.assertEqual(code, P.EXIT_FAILED)
        self.assertTrue(any(l.startswith("Cache hit: ") and "PREVIOUS verified resume" in l
                            for l in lines), lines)
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"), "--no-cache",
                                   runner=FakeTex(fail=True))
        self.assertFalse(any(l.startswith("Cache hit: ") for l in lines))
        self.assertFalse((self.outputs / "example-co_platform-engineer_resume.pdf").exists())


import resume_kit as K  # noqa: E402  (load_pipeline put the repository on sys.path)

ROOT = Path(__file__).resolve().parent.parent
REAL_MASTER = ROOT / "master_profile.md"
HAVE_REAL_KIT = REAL_MASTER.is_file() and K.has_kit(
    REAL_MASTER.read_text(encoding="utf-8").split("\n"))


def kit_problems(text: str) -> list[str]:
    try:
        K.parse_kit(text.split("\n"))
    except K.KitError as exc:
        return exc.problems
    return []


class KitParsingTests(unittest.TestCase):
    def test_sections_become_typed_objects(self):
        kit = K.parse_kit(MASTER_KIT.split("\n"))
        self.assertEqual(set(kit.families), set(K.FAMILY_CODES))
        self.assertIn("Platform engineer who cut deploy time 30%", kit.summaries["dev"][1])
        self.assertEqual(kit.rows["Backend"][1], ("Python", "FastAPI"))
        ana = {spec.row: spec for spec in kit.row_order["ana"]}
        self.assertEqual(ana["Backend"].only, ("Python",))
        acme = kit.groups[0]
        self.assertEqual((acme.kind, acme.name, acme.title, acme.location, acme.dates),
                         ("experience", "Acme Corp", "Platform Engineer", "Toronto",
                          "Jan 2024 to Dec 2024"))
        self.assertEqual(kit.groups[-1].kind, "project")
        a01 = kit.bullets["A01"]
        self.assertEqual((a01.plain, a01.bold, a01.impact, a01.themes),
                         ("Cut deploy time 30% with a Python tool across 50+ services",
                          ["Python"], 5, ("automation", "deployment")))
        self.assertEqual([f.code for f in kit.bullets["A02"].families], ["dev", "swe"])
        self.assertEqual(kit.gaps["dev"][1], ("Kubernetes", "Terraform", "Helm"))
        self.assertEqual(kit.synonyms[0].words, ("release automation", "developer productivity"))

    def test_every_validation_rule_names_the_problem(self):
        cases = {
            "duplicate bullet ID A01": MASTER_KIT.replace("| A02 |", "| A01 |"),
            "keeps with Z99, which does not exist": MASTER_KIT.replace(
                "| dev | 3 | |", "| dev | 3 | Z99 |"),
            "names unknown family 'devops'": MASTER_KIT.replace("| dev, swe | 4 |",
                                                                "| devops | 4 |"),
            "needs a company, title, location and dates ('location' missing)":
                MASTER_KIT.replace("Acme Corp, Platform Engineer, Toronto, Jan 2024",
                                   "Acme Corp, Platform Engineer, Jan 2024"),
            "starts with a dangling pronoun": MASTER_KIT.replace(
                "| A03 | Led troubleshooting", "| A03 | It led troubleshooting"),
            "section 9.5 (bullet bank) is missing": MASTER_KIT.replace("### 9.5 Bullet bank",
                                                                       "### Bullet bank"),
        }
        for expected, text in cases.items():
            with self.subTest(expected):
                self.assertTrue(any(expected in p for p in kit_problems(text)),
                                kit_problems(text))

    def test_verb_it_is_the_project_itself_but_dangles_in_a_role(self):
        project = MASTER_KIT.replace("| G01 | Built a **FastAPI** service",
                                     "| G01 | Covered it with a **FastAPI** service")
        self.assertEqual(kit_problems(project), [])
        role = MASTER_KIT.replace("| A03 | Led troubleshooting", "| A03 | Drove it, leading "
                                                                  "troubleshooting")
        self.assertTrue(any("dangling pronoun" in p for p in kit_problems(role)))

    def test_new_bullets_and_alternate_wordings_are_flagged(self):
        text = MASTER_KIT.replace("with the platform team |", "with the platform team (*new*) |")
        text = text.replace("| A03 | Led troubleshooting of runner outages with the platform team "
                            "(*new*) | debugging, collaboration |",
                            "| A03 | Led troubleshooting of runner outages with the platform team "
                            "(*new*) | debugging (alt wording of A02) |")
        kit = K.parse_kit(text.split("\n"))
        self.assertEqual(kit.new_bullets, ["A03"])
        self.assertTrue(kit.bullets["A03"].plain.endswith("platform team"))
        self.assertEqual(kit.bullets["A03"].alternate_of, "A02")

    def test_an_invalid_kit_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(Path(tmp), master=MASTER_KIT.replace("| A02 |", "| A01 |"))
            lines: list[str] = []
            code = P.run([str(root / "target_jobs" / "proceed.md"), "--workspace", str(root)],
                         which=fake_which(), runner=FakeTex(), out=lines.append)
        self.assertEqual(code, P.EXIT_MISSING_PREREQ)
        self.assertIn("The resume kit in", lines[-1])
        self.assertIn("duplicate bullet ID A01", lines[-1])


class KitAuditTests(unittest.TestCase):
    def audit(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "master.md"
            path.write_text(text, encoding="utf-8")
            master = P.parse_master(path)
        return P.audit_kit(P.load_kit(master), master)

    def test_clean_kit_has_nothing_blocked(self):
        audit = self.audit(MASTER_KIT)
        self.assertEqual(audit.blocked, {})
        kit = K.parse_kit(MASTER_KIT.split("\n"))
        self.assertEqual([audit.entries[g.key] for g in kit.groups], ["2.1", "2.2", "3.1"])

    def test_no_tools_unknown_numbers_and_no_echoes_are_blocked(self):
        text = MASTER_KIT.replace("| A03 | Led troubleshooting of runner outages with the "
                                  "platform team |",
                                  "| A03 | Led troubleshooting of **Kubernetes** outages for 70 "
                                  "teams |")
        audit = self.audit(text)
        self.assertIn("names 'Kubernetes', which is marked [NO]", audit.blocked["A03"])
        self.assertTrue(any("number '70'" in p for p in audit.blocked["A03"]))

    def test_derived_and_paraphrased_wording_is_reported_not_blocked(self):
        text = MASTER_KIT.replace("| A01 | Cut deploy time 30% with a **Python** tool across "
                                  "50+ services |",
                                  "| A01 | Accelerated releases with **Python** automation "
                                  "spanning 50+ services |")
        audit = self.audit(text)
        self.assertNotIn("A01", audit.blocked)
        self.assertTrue(any(n.startswith("paraphrase") for n in audit.notes["A01"]))


@unittest.skipUnless(HAVE_REAL_KIT, "the local career bank with a resume kit is not present")
class RealKitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.master = P.parse_master(REAL_MASTER)
        cls.kit = P.load_kit(cls.master)
        cls.audit = P.audit_kit(cls.kit, cls.master)

    def test_bullet_bank_loads_and_validates(self):
        self.assertEqual(len(self.kit.bullets), len(set(self.kit.bullets)))
        self.assertGreaterEqual(len(self.kit.bullets), 90)
        for bullet in self.kit.bullets.values():
            if bullet.keep_with:
                self.assertIn(bullet.keep_with, self.kit.bullets)
        self.assertEqual(self.kit.bullets["S01b"].alternate_of, "S01")
        self.assertIn("S20", self.kit.new_bullets)
        self.assertEqual({g.kind for g in self.kit.groups},
                         {"experience", "project", "research", "leadership"})
        for group in self.kit.groups:
            self.assertIsNotNone(self.audit.entries[group.key], group.header)

    def test_audit_blocks_unevidenced_tools_and_explains_derived_numbers(self):
        self.assertIn("S21", self.audit.blocked)  # SSH keys: no [V]/[R] line mentions SSH
        self.assertIn("'17' is not stated; verified counts add up to it (11 + 6)",
                      self.audit.notes["R06"])


if __name__ == "__main__":
    unittest.main()


JD_ANALYST = """# Risk Analyst at Example Bank

## Responsibilities
- Build Power BI dashboards on SQL Server and flag anomalies in lending data

## Requirements
- Must have SQL and Python
- 3+ years of experience in analytics
- Bachelor's degree required
- Must be eligible for a U.S. security clearance
- Start date: January 2027

Location: New York, NY. Visa sponsorship is not available for this role.
"""


class FamilyTests(unittest.TestCase):
    kit = K.parse_kit(MASTER_KIT.split("\n"))

    def family(self, title: str, body: str = "## Requirements\n- Python\n"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.md"
            path.write_text(f"# {title} at Example Co\n\n{body}", encoding="utf-8")
            jd = P.read_job_description(path, Path(tmp))
        return P.classify_family(jd, self.kit)

    def test_title_rules_in_order(self):
        cases = {"Data Analyst": "ana", "Analyst, Early Career (Full Stack)": "ana",
                 "Machine Learning Engineer, Growth Platform": "ml",
                 "Forward Deployed Engineer, Sovereign AI": "fde",
                 "Solutions Engineer": "fde",
                 "Software Engineer II, Backend (Test Infra)": "test", "SDET": "test",
                 "QA Automation Engineer": "test", "Site Reliability Engineer": "dev",
                 "Platform Engineer": "dev"}
        for title, code in cases.items():
            with self.subTest(title):
                self.assertEqual(self.family(title).code, code)

    def test_ambiguous_titles_lower_confidence_and_say_why(self):
        decision = self.family("Machine Learning Engineer, Growth Platform")
        self.assertEqual((decision.code, decision.method), ("ml", "title rule"))
        self.assertLess(decision.confidence, 0.9)
        self.assertIn("also matched dev", decision.reasons[1])

    def test_quality_needs_a_regulated_industry(self):
        self.assertEqual(self.family("Quality Engineer", "## Requirements\n- GMP and FDA "
                                                         "validation\n").code, "qa")
        self.assertNotEqual(self.family("Quality Engineer").code, "qa")

    def test_typical_titles_then_requirement_lines(self):
        self.assertEqual(self.family("BI Analyst").code, "ana")
        decision = self.family("Member of Technical Staff", "## Requirements\n- Train models "
                                                           "with PyTorch for ranking\n")
        self.assertEqual((decision.code, decision.method), ("ml", "requirement lines"))

    def test_traits_come_from_the_title_and_requirements(self):
        decision = self.family("Robotics Software Engineer")
        self.assertIn("robotics", decision.traits)
        self.assertNotIn("healthcare", self.family("Software Engineer").traits)


class JobDocumentTests(KitCase):
    def test_job_json_has_family_requirements_and_hard_flags(self):
        (self.root / "target_jobs" / "analyst.md").write_text(JD_ANALYST, encoding="utf-8")
        code, lines = self.run_cli(str(self.root / "target_jobs" / "analyst.md"),
                                   "--override-low-fit")
        self.assertIn("Role family: ana", "\n".join(lines))
        job = json.loads((self.outputs / "example-bank_risk-analyst_job.json").read_text())
        self.assertEqual((job["title"], job["company"], job["family"]["code"]),
                         ("Risk Analyst", "Example Bank", "ana"))
        types = {r["type"] for r in job["requirements"]}
        self.assertTrue({"core", "responsibility", "hard"} <= types, types)
        sql = next(r for r in job["requirements"] if "SQL and Python" in r["text"])
        self.assertEqual((sql["type"], sql["weight"]), ("core", 3))
        self.assertIn("SQL", sql["phrases"])
        hard = job["hard_requirements"]
        self.assertEqual(set(hard), {"years", "degree", "clearance", "location",
                                     "work_authorization", "start_date"})
        self.assertEqual(hard["years"]["status"], "not_met")
        self.assertEqual(hard["clearance"]["status"], "not_met")  # US clearance
        self.assertIn("not obtainable", hard["clearance"]["detail"])
        self.assertEqual(hard["work_authorization"]["status"], "unknown")
        self.assertIn("no visa sponsorship", hard["work_authorization"]["detail"])
        self.assertFalse(hard["work_authorization"]["affects_fit"])
        self.assertEqual(hard["start_date"]["status"], "unknown")  # synthetic master: not recorded
