"""Requirement coverage: JD parsing, evidence, profile fit, resume fit and calibration."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from fractions import Fraction
from pathlib import Path

from fixtures import JD_PROCEED, MASTER, TEMPLATE, FakeTex, load_pipeline
from test_pipeline import WorkspaceCase

P = load_pipeline()
ROOT = Path(__file__).resolve().parent.parent
REGRESSION = Path(__file__).resolve().parent / "fixtures" / "regression"
REAL_MASTER = ROOT / "master_profile.md"
COHERE_JD = REGRESSION / "ashby_cohere_forward_deployed_engineer_sovereign_ai.md"
STRIPE_JD = REGRESSION / "greenhouse_stripe_machine_learning_engineer_growth_platform.md"
COHERE_PDF = REGRESSION / "cohere_forward-deployed-engineer-sovereign-ai_resume.pdf"
STRIPE_PDF = REGRESSION / "stripe_machine-learning-engineer-growth-platform_resume.pdf"
AFFIRM_JD = REGRESSION / "greenhouse_affirm_software_engineer_ii_backend_test_infra_0d10ca79.md"
STRIPE_V2_PDF = REGRESSION / "stripe_mle_v2_resume.pdf"
AFFIRM_PDF = REGRESSION / "affirm_test_infra_resume.pdf"

JD_FIT = """# Platform Engineer at Example Co

**Location**: Toronto, ON

## Responsibilities
- Deploy and roll out services to customers in on-premises environments
- Debug production incidents across client-side and server-side code

## Minimum requirements
- 5+ years of experience shipping Python
- Security clearance required
- Experience with Docker is a plus
- You excel in fast-paced environments
- See [the policy](https://example.com/security-request.html) for details

## Preferred qualifications
- Experience with FastAPI, or PostgreSQL
"""


def concept(key: str):
    return P.CONCEPT_INDEX[key]


class JudgeTests(unittest.TestCase):
    """Phrase-aware matching with a context judge that quotes and rejects."""

    def test_false_friends_never_count(self):
        cases = [
            ("software_quality", "Added rain and snow weatherproofing, stress tested the design "
                                 "against industry standards"),
            ("model_evaluation", "Co-op evaluations: Outstanding at OmniaBio, Excellent on other "
                                 "terms"),
            ("ai_agents", "WATonomous, University of Waterloo Autonomous Vehicle Design Team"),
            ("ai_agents", "Silver Medal and Certificate of Excellence, Autonomous Robotics"),
            ("production", "Ran one main production line for 70+ operators"),
        ]
        for key, text in cases:
            ok, rejected = P.concept_proven(concept(key), text)
            self.assertFalse(ok, (key, text))
        ok, rejected = P.concept_proven(concept("model_evaluation"),
                                        "Co-op evaluations: Outstanding at OmniaBio")
        self.assertTrue(rejected and "co-op evaluations" in rejected[0].lower())

    def test_real_evidence_counts(self):
        cases = [
            ("software_quality", "Covered it with 167 pytest tests and ruff and mypy checks"),
            ("model_evaluation", "Built an offline eval harness of 17 tests that replays chats"),
            ("deployment", "Upgraded Maven across the pipelines for 5 banks"),
            ("customers", "Defended $200K+ in retailer non-delivery claims"),
            ("code_outside", "Debugged failing pipelines, routing complex code issues to "
                             "developers"),
            ("policy_learning", "Trained an AI race driver with PPO"),
        ]
        for key, text in cases:
            self.assertTrue(P.concept_proven(concept(key), text)[0], (key, text))

    def test_word_boundaries_and_negation(self):
        self.assertFalse(P.concept_proven(concept("software_quality"), "contested the claim")[0])
        self.assertFalse(P.concept_proven(concept("production"), "a prodigy of planning")[0])
        ok, rejected = P.concept_proven(concept("deployment"),
                                        "He built it to deploy at Symcor but didn't finish.")
        self.assertTrue(ok)  # "but" ends the clause: "to deploy at Symcor" stands alone
        ok, rejected = P.concept_proven(concept("deployment"), "The tool was never deployed.")
        self.assertFalse(ok)
        self.assertIn("negated", rejected[0])

    def test_full_stack_needs_both_sides(self):
        self.assertTrue(P.concept_proven(concept("full_stack"),
                                         "React and TypeScript dashboard on FastAPI")[0])
        self.assertFalse(P.concept_proven(concept("full_stack"), "A FastAPI service")[0])


class ScorerFalsePositiveTests(unittest.TestCase):
    """Look-alike words never prove a term; real inflections of the same word do."""

    def asks(self, text: str) -> set[str]:
        return {c.key for c in P._jd_concepts(text)}

    def proves(self, key: str, text: str) -> bool:
        return P.concept_proven(concept(key), text)[0]

    def test_spec_is_not_inspected(self):
        self.assertIn("documentation", self.asks("Write tech specs, design docs and ADRs"))
        self.assertNotIn("documentation", self.asks("Inspected incoming materials daily"))
        for key in ("documentation", "software_quality", "test_automation"):
            self.assertFalse(self.proves(key, "Materials at the front were inspected first"),
                             key)

    def test_department_names_are_not_qa_experience(self):
        line = ("A Power BI report used by 4 teams (QC, QA, supply chain, warehouse) tracks "
                "inbound products to inspect")
        for key in ("software_quality", "test_automation", "e2e_testing", "production"):
            self.assertFalse(self.proves(key, line), key)
        self.assertEqual(P.SKILL_FINDER.keys(line), ["power_bi"])

    def test_product_is_not_production(self):
        self.assertFalse(self.proves("production", "Tracks inbound products to inspect"))
        self.assertNotIn("production", self.asks("Partner with product managers on the roadmap"))
        self.assertIn("production", self.asks("Own ML models in production"))

    def test_coop_evaluations_are_not_testing_or_evaluation(self):
        line = "Co-op evaluations: Outstanding at OmniaBio, Excellent on other terms"
        for key in ("model_evaluation", "test_automation", "software_quality"):
            self.assertFalse(self.proves(key, line), key)

    def test_embedded_and_embeddings(self):
        self.assertIn("embeddings", self.asks("Experience with embeddings and vector search"))
        self.assertNotIn("embeddings", self.asks("Embedded systems experience"))
        self.assertTrue(self.proves("embeddings", "Generated MiniLM embeddings of code into FAISS"))
        self.assertTrue(self.proves("embeddings", "Embedded code with MiniLM"))  # MiniLM proves it
        self.assertFalse(self.proves("embeddings", "Embedded systems firmware in C"))

    def test_tests_and_well_tested(self):
        self.assertIn("software_quality",
                      self.asks("Writing clear, well tested and extensible code"))
        self.assertIn("software_quality", self.asks("Write tests for new services"))
        automation = self.asks("3+ years of experience in test automation")
        self.assertIn("test_automation", automation)
        self.assertNotIn("software_quality", automation)  # one ask, one term
        self.assertTrue(self.proves("software_quality", "Covered it with 1,147 Python tests"))
        self.assertTrue(self.proves("test_automation", "Covered it with 1,147 Python tests"))

    def test_stable_and_stability(self):
        self.assertIn("reliability", self.asks("Enhance test stability"))
        self.assertIn("test_automation", self.asks("Enhance test stability"))
        self.assertIn("reliability", self.asks("Keep builds stable"))
        self.assertTrue(self.proves("reliability", "Improved CI stability, cutting failures 40%"))
        self.assertFalse(self.proves("reliability", "Trained a driver with Stable-Baselines3 PPO"))

    def test_triage_is_debugging(self):
        self.assertIn("debugging", self.asks("Support teams by assisting with triage requests"))


class RequirementParsingTests(WorkspaceCase):
    def model(self, text: str = JD_FIT):
        return P.build_coverage_model(self.jd(text), self.master())

    def test_types_weights_and_terms(self):
        model = self.model()
        by_text = {r.text: r for r in model.requirements}
        deploy = by_text["Deploy and roll out services to customers in on-premises environments"]
        self.assertEqual((deploy.type, deploy.weight), ("responsibility", 2))
        self.assertEqual({t.key for t in deploy.terms},
                         {"concept:deployment", "concept:customers", "concept:on_premises"})
        debug = by_text["Debug production incidents across client-side and server-side code"]
        self.assertNotIn("concept:customers", {t.key for t in debug.terms})  # client-side
        docker = by_text["Experience with Docker is a plus"]
        self.assertEqual((docker.type, docker.weight), ("preferred", 1))
        python = by_text["5+ years of experience shipping Python"]
        self.assertEqual((python.type, python.weight), ("core", 3))
        preferred = by_text["Experience with FastAPI, or PostgreSQL"]
        self.assertEqual(preferred.mode, "any_of")
        self.assertEqual([r.id for r in model.requirements],
                         [f"R{n:02d}" for n in range(1, len(model.requirements) + 1)])

    def test_verbs_and_links_are_not_skills(self):
        model = self.model()
        keys = {t.key for r in model.requirements for t in r.terms}
        self.assertNotIn("skill:excel", keys)  # "You excel in fast-paced environments"
        self.assertNotIn("skill:html", keys)   # a link target ending in .html
        self.assertIn("You excel in fast-paced environments",
                      [u["text"] for u in model.unscored])
        legacy = {r.key for r in self.analysis(JD_FIT).requirements}
        self.assertFalse({"excel", "html"} & legacy)  # the skill-coverage scorer too

    def test_what_we_look_for_and_unlabelled_asks_are_core(self):
        jd = ("# Test Engineer at Example Co\n\n## What We Look For\n"
              "- Hands-on experience with Python\n\n## Toolbox\n"
              "- 3+ years of experience with Docker\n- Hands-on experience with FastAPI\n"
              "- Knowledge of PostgreSQL\n")
        by_text = {r.text: r for r in self.model(jd).requirements}
        for text in ("Hands-on experience with Python", "3+ years of experience with Docker",
                     "Knowledge of PostgreSQL"):
            self.assertEqual(by_text[text].type, "core", text)

    def test_soft_lines_are_not_scored(self):
        model = self.model(JD_FIT + "- Strong collaboration and ownership\n")
        self.assertIn("Strong collaboration and ownership", [u["text"] for u in model.unscored])

    def test_hard_requirements(self):
        hard = {h.type: h for h in self.model().hard}
        self.assertEqual(hard["years"].status, "not_met")  # 16 months of terms < 5 years
        self.assertEqual(hard["clearance"].status, "not_met")  # no citizenship on file
        self.assertEqual(hard["location"].status, "unknown")
        self.assertEqual(self.model().region, "Canada")

    def test_clearance_and_work_authorization_with_citizenship(self):
        master = MASTER.replace("## 2. Experience",
                                "## 0. Identity\n\n- `[V]` Toronto, ON. Email x@example.com.\n"
                                "- `[V]` Citizenship: Canadian. No sponsorship needed in Canada."
                                "\n\n## 2. Experience")
        (self.root / "master.md").write_text(master, encoding="utf-8")
        model = self.model()
        hard = {h.type: h for h in model.hard}
        self.assertEqual(hard["clearance"].status, "unknown")
        self.assertIn("willingness", hard["clearance"].detail)
        self.assertEqual(hard["location"].status, "met")
        self.assertTrue(model.work_authorization.startswith("Canadian citizen: no sponsorship"))
        us = self.jd(JD_FIT.replace("Toronto, ON", "San Francisco, CA"), "us.md")
        note = P.build_coverage_model(us, self.master()).work_authorization
        self.assertIn("authorization is not recorded", note)


def manual_model(requirements, profile, hard=()):
    return P.CoverageModel(requirements, list(hard), [], "Canada", "note", None, profile, {})


def req(rid, rtype, keys, mode="all_of"):
    terms = [P.CoverageTerm(k, k, k.split(":")[0]) for k in keys]
    return P.CoverageRequirement(rid, rid, rtype, P.REQ_TYPE_WEIGHTS[rtype], "s", 1, terms, mode)


def proof(strength, entry="e1"):
    return {entry: P.Proof(Fraction(strength, 3), strength, "quote", entry, "master", 1)}


class ScoringTests(unittest.TestCase):
    def test_weighted_coverage_and_never_ten_with_core_unmet(self):
        reqs = [req(f"R{i}", "responsibility", ["skill:python"]) for i in range(20)]
        reqs.append(req("C1", "core", ["skill:rust"]))
        model = manual_model(reqs, {"skill:python": proof(3)})
        fit = P.assess_fit(model, None)
        self.assertEqual(fit["profile_fit"], 9.0)  # raw 40/43 = 9.3, capped
        self.assertTrue(fit["core_requirement_unmet"])
        self.assertTrue(any("capped" in note for note in fit["calibration"]))
        full = manual_model(reqs[:-1], {"skill:python": proof(3)})
        self.assertEqual(P.assess_fit(full, None)["profile_fit"], 10.0)

    def test_evidence_strength_scale(self):
        model = manual_model([req("R1", "core", ["skill:python"])], {"skill:python": proof(2)})
        self.assertEqual(P.assess_fit(model, None)["profile_fit"], 6.7)
        model.profile = {"skill:python": proof(1)}
        self.assertEqual(P.assess_fit(model, None)["profile_fit"], 3.3)

    def test_coherence_prefers_one_body_of_experience(self):
        reqs = [req("R1", "core", ["skill:python", "skill:docker"])]
        together = manual_model(reqs, {"skill:python": proof(3, "job"),
                                       "skill:docker": proof(3, "job")})
        apart = manual_model(reqs, {"skill:python": proof(3, "job"),
                                    "skill:docker": proof(3, "other job")})
        self.assertEqual(P.assess_fit(together, None)["profile_fit"], 10.0)
        # credit = 1/5 independent (1.0) + 4/5 best single entry (0.5)
        self.assertEqual(P.assess_fit(apart, None)["profile_fit"], 6.0)

    def test_generic_terms_weigh_half(self):
        reqs = [P.CoverageRequirement("R1", "x", "core", 3, "s", 1, [
            P.CoverageTerm("concept:deployment", "d", "concept"),
            P.CoverageTerm("concept:leadership", "l", "concept")])]
        model = manual_model(reqs, {"concept:leadership": proof(3)})
        self.assertEqual(P.assess_fit(model, None)["profile_fit"], 3.3)  # 0.5 of 1.5

    def test_resume_fit_rules(self):
        reqs = [req("R1", "core", ["skill:python"]), req("R2", "core", ["skill:docker"])]
        model = manual_model(reqs, {"skill:python": proof(3), "skill:docker": proof(3)})
        bullet = P.ResumeLine("Built a Python tool", "bullet", "experience", "Acme", 3)
        skills = P.ResumeLine("Cloud: Docker", "skills", "skills")
        fit = P.assess_fit(model, [bullet, skills])
        rows = {r["id"]: r for r in fit["requirements"]}
        self.assertEqual(rows["R1"]["resume_coverage"], 1.0)
        self.assertEqual(rows["R2"]["resume_coverage"], 0.3)  # skills list only
        self.assertEqual(fit["resume_fit"], 6.5)
        self.assertFalse(fit["underselling"] is None)
        self.assertTrue(fit["underselling"])  # 10.0 profile vs 6.5 resume

    def test_resume_fit_never_exceeds_profile_fit(self):
        reqs = [req("R1", "core", ["skill:python"])]
        model = manual_model(reqs, {"skill:python": proof(1)})
        fit = P.assess_fit(model, [P.ResumeLine("Built a Python tool", "bullet", "experience",
                                                "Acme", 3)])
        self.assertEqual(fit["resume_fit_raw"], 10.0)
        self.assertEqual(fit["resume_fit"], fit["profile_fit"])
        self.assertTrue(any("capped at profile fit" in n for n in fit["calibration"]))

    def test_nothing_scorable_is_not_a_zero(self):
        fit = P.assess_fit(manual_model([], {}), [])
        self.assertIsNone(fit["profile_fit"])
        self.assertIsNone(fit["resume_fit"])
        self.assertIn("not scorable", fit["calibration"][0])

    def test_hard_requirement_cap_and_separate_scores(self):
        hard = [P.HardRequirement("years", "5+ years", 3, "not_met", "short")]
        model = manual_model([req("R1", "core", ["skill:python"])], {"skill:python": proof(3)},
                             hard)
        fit = P.assess_fit(model, None)
        self.assertEqual(fit["profile_fit"], 10.0)
        self.assertEqual(fit["profile_fit_with_hard_requirements"], 4.0)
        self.assertEqual(fit["hard_requirement_flags"], ["years: not_met"])
        self.assertEqual(fit["work_authorization_note"], "note")


class FitReportTests(WorkspaceCase):
    def test_reports_are_written_for_every_run(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_OK)
        base = "example-co_platform-engineer"
        report = json.loads((self.outputs / f"{base}_analysis.json").read_text())
        fit = report["fit"]
        self.assertIsNotNone(fit["resume_fit"])
        self.assertLessEqual(fit["resume_fit"], fit["profile_fit"])
        self.assertEqual(fit["files"], {"job": f"{base}_job.json",
                                        "requirements": f"{base}_requirements.json",
                                        "report": f"{base}_fit_report.md"})
        doc = json.loads((self.outputs / f"{base}_requirements.json").read_text())
        row = doc["requirements"][0]
        for key in ("id", "text", "type", "weight", "terms", "profile_coverage",
                    "resume_coverage"):
            self.assertIn(key, row)
        self.assertIn("synonyms", row["terms"][0])
        md = (self.outputs / f"{base}_fit_report.md").read_text()
        for heading in ("Profile fit", "Resume fit", "## Hard requirements", "## Requirements",
                        "## Top changes"):
            self.assertIn(heading, md)
        self.assertTrue(any(line.startswith("Profile fit:") for line in lines))

    def test_low_fit_report_has_no_resume_fit(self):
        self.run_cli(str(self.root / "target_jobs" / "reject.md"))
        report = json.loads((self.outputs / "other-co_data-scientist_analysis.json").read_text())
        self.assertIsNone(report["fit"]["resume_fit"])
        self.assertIn("Resume fit: **n/a**",
                      (self.outputs / "other-co_data-scientist_fit_report.md").read_text())

    def test_score_resume_prints_and_writes_nothing(self):
        self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        pdf = self.outputs / "example-co_platform-engineer_resume.pdf"
        before = sorted(p.name for p in self.outputs.iterdir())
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   "--score-resume", str(pdf))
        self.assertEqual(code, P.EXIT_OK)
        self.assertTrue(lines[0].startswith("Master profile: "), lines[0])
        self.assertTrue(lines[1].startswith("# Fit report: Platform Engineer at Example Co"))
        self.assertEqual(sorted(p.name for p in self.outputs.iterdir()), before)
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   "--score-resume", str(self.root / "missing.pdf"))
        self.assertEqual(code, P.EXIT_INVALID_INPUT)

    def test_resume_text_parser(self):
        text = ("Name\nSummary\nEngineer who ships Python. Builds RAG services.\n"
                "Technical Skills\nBackend: Python, FastAPI\nExperience\nAcme Corp\n\n"
                "Jan 2024 – Dec 2024\n\nPlatform Engineer\nToronto, ON\n"
                "• Built a Python tool that cut deploy time\nacross 50+ services\n"
                "• Ran Docker workloads\nProjects\nGamma App | FastAPI, PostgreSQL\n"
                "• Built a FastAPI service\nEducation\nExample University\n"
                "• Co-op evaluations: Outstanding\n")
        lines = P.resume_lines_from_text(text)
        roles = [(l.role, l.section, l.entry) for l in lines]
        self.assertEqual(roles[:2], [("summary", "summary", ""), ("summary", "summary", "")])
        bullets = [l for l in lines if l.role == "bullet"]
        self.assertEqual(bullets[0].text, "Built a Python tool that cut deploy time across 50+ "
                                          "services")
        self.assertEqual((bullets[0].entry, bullets[0].strength), ("Acme Corp", 3))
        self.assertEqual((bullets[2].entry, bullets[2].section), ("Gamma App", "projects"))
        self.assertIn(("stack", "projects", "Gamma App"), roles)
        self.assertEqual(bullets[3].strength, 1)  # education


# ---------------------------------------------------------------------------
# Generation: set cover, dependency groups, summary and skills
# ---------------------------------------------------------------------------
DEPENDENT_TEMPLATE_BULLETS = (
    "            \\resumeItem{Ran \\textbf{GitLab CI/CD} pipelines with \\textbf{Docker} "
    "executors on \\textbf{Ubuntu} runners}\n")
INTERESTS = ("\\section{Additional}\n\\begin{itemize}[leftmargin=0.15in, label={}]\n"
             "    \\item \\small{\n        \\textbf{Languages}{: English, German (fluent)} \\\\\n"
             "        \\textbf{Interests}{: Helm}\n    }\n\\end{itemize}\n\n\\end{document}")


class GenerationTests(WorkspaceCase):
    def plan(self, jd: str = JD_PROCEED, template: str | None = None):
        if template is not None:
            (self.root / "template.tex").write_text(template, encoding="utf-8")
        analysis = self.analysis(jd)
        return analysis, P.build_resume_plan(P.parse_template(self.root / "template.tex"),
                                             analysis.master, analysis)

    def entry(self, plan, name):
        return next(ep for (si, ei), ep in plan.entries.items()
                    if P._entry_name(plan, si, ei) == name)

    def test_irrelevant_roles_dropped_and_jobs_keep_three_bullets(self):
        _a, plan = self.plan()
        acme = self.entry(plan, "Acme Corp")
        self.assertTrue(acme.keep)
        self.assertEqual(len(acme.selected), 3)  # every verified Acme bullet, floor 3
        self.assertEqual(acme.floor, 3)
        self.assertFalse(self.entry(plan, "Beta Labs").keep)  # proves nothing this job asks
        removed = [i for i in plan.template_items_removed if i["item"] == "Beta Labs"]
        self.assertEqual(removed[0]["reason"], "no bullet proves a requirement of this job")
        self.assertTrue(self.entry(plan, "Gamma App").keep)  # the only FastAPI/PostgreSQL proof

    def test_set_cover_prefers_the_bullet_with_new_coverage(self):
        _a, plan = self.plan()
        acme = self.entry(plan, "Acme Corp")
        first = acme.selected[0]
        self.assertIn("Docker", first.plain)  # Docker (must have) + GitLab CI/CD + Ubuntu
        values = [c.value for c in acme.selected]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_dependent_bullets_follow_their_parent(self):
        dependent = DEPENDENT_TEMPLATE_BULLETS + (
            "            \\resumeItem{Drove it from \\textbf{Python} tooling on the platform team}\n")
        _a, plan = self.plan(template=TEMPLATE.replace(DEPENDENT_TEMPLATE_BULLETS, dependent))
        acme = self.entry(plan, "Acme Corp")
        child = next(c for c in acme.candidates if c.plain.startswith("Drove it"))
        parent = next(c for c in acme.candidates if c.plain.startswith("Ran GitLab"))
        self.assertEqual(child.parent_index, parent.bullet_index)
        order = [c.bullet_index for c in acme.selected]
        if child in acme.selected:
            self.assertEqual(order[order.index(child.bullet_index) - 1], parent.bullet_index)
        lines = P.resume_lines_from_plan(plan, set(), plan_master := self.master())
        self.assertEqual(P.dependency_lint(lines, plan), [])
        si, ei = acme.section_index, acme.entry_index
        trimmed = {("bullet", si, ei, parent.bullet_index)}
        rendered = dict(P.active_model(plan, trimmed))[si]
        bullets = dict(rendered)[ei]
        self.assertNotIn(child, bullets)  # never shown without its parent
        self.assertTrue(P._unit_is_noop(("bullet", si, ei, child.bullet_index), trimmed, plan))
        swapped = [lines[1], lines[0]] if len(lines) > 1 else lines
        orphan = [P.ResumeLine("Drove it from the tickets", "bullet", "experience", "Acme", 3)]
        self.assertTrue(P.dependency_lint(orphan))
        del plan_master, swapped

    def test_orphan_dependent_bullet_is_rejected(self):
        orphan = "            \\resumeItem{Drove it from \\textbf{Kubernetes} clusters}\n"
        broken = TEMPLATE.replace(
            "            \\resumeItem{Operated \\textbf{Kubernetes} clusters for 50+ services}\n",
            "            \\resumeItem{Operated \\textbf{Kubernetes} clusters for 50+ services}\n"
            + orphan)
        _a, plan = self.plan(template=broken)
        reasons = [r["reasons"][0] for r in self.entry(plan, "Acme Corp").rejected]
        self.assertTrue(any("depends on the bullet before it" in r or "[NO]" in r
                            for r in reasons))

    def test_summary_clauses_keep_lists_and_proof(self):
        text = ("Caught 10 SAP tracking mismatches, escalated to a VP review across Quality, "
                "Supply Chain, and Warehouse with CCRM validating")
        self.assertEqual(P._summary_clause(text), "caught 10 SAP tracking mismatches")
        maven = ("Upgraded Maven across the pipelines for 5 banks (RBC, TD, Scotiabank, BMO, "
                 "National Bank), pulling from JFrog Artifactory")
        self.assertEqual(P._summary_clause(maven, ["concept:deployment"]),
                         "upgraded Maven across the pipelines for 5 banks (RBC, TD, Scotiabank, "
                         "BMO, National Bank)")
        self.assertIsNone(P._summary_clause(
            "Version controlled the pipeline in GitHub under MasterControl SOPs; it stayed in "
            "production and my handoff docs onboard new hires", ["concept:end_to_end"]))

    def test_generated_summary_is_tailored_and_verified(self):
        analysis, plan = self.plan()
        self.assertTrue(plan.summary_sentences)
        summary = " ".join(P.latex_to_plain(s) for s in plan.summary_sentences)
        self.assertNotIn("Platform engineer who built a Python tool", summary)  # template text
        self.assertNotIn("Kubernetes", summary)
        scope = P.build_scope(analysis.master, None)
        for sentence in plan.summary_sentences:
            self.assertTrue(P.verify_claim(P.latex_to_plain(sentence), analysis.master,
                                           scope).ok, sentence)
        self.assertIn("\\textbf{", " ".join(plan.summary_sentences))  # tech is bold
        self.assertNotIn("Canadian", summary)  # no clearance or citizenship requirement
        P.build_provenance(plan, analysis.master, set())  # the hard gate accepts it

    def test_required_statement_only_when_true_and_needed(self):
        master = MASTER.replace("## 2. Experience",
                                "## 0. Identity\n\n- `[V]` Toronto, ON.\n"
                                "- `[V]` Citizenship: Canadian. No sponsorship needed in Canada."
                                "\n\n## 2. Experience")
        (self.root / "master.md").write_text(master, encoding="utf-8")
        jd = JD_PROCEED.replace("## Responsibilities", "**Location**: Toronto, ON\n\n"
                                "## Responsibilities") + "- Security clearance required\n"
        _a, plan = self.plan(jd)
        summary = " ".join(P.latex_to_plain(s) for s in plan.summary_sentences)
        self.assertIn("Canadian citizen; no sponsorship needed in Canada.", summary)
        self.assertNotIn("willing", summary)  # never claimed without a [V] line

    def write_rules(self, *rules):
        (self.root / P.REWRITES_NAME).write_text(json.dumps({"rewrites": list(rules)}),
                                                 encoding="utf-8")

    def test_rewrites_need_the_ask_and_pass_verification(self):
        self.write_rules(
            {"find": "Led troubleshooting of runner outages",
             "replace": "Led triage of runner outages", "when": ["debugging"]},
            {"find": "on Ubuntu runners", "replace": "on Ubuntu and Kubernetes runners",
             "when": ["debugging"]})
        _a, plan = self.plan()
        self.assertEqual(plan.rewrites, [])  # the job asks for no debugging
        jd = JD_PROCEED.replace("- Run Docker", "- Triage and debug runner outages\n- Run Docker")
        analysis, plan = self.plan(jd)
        self.assertEqual([(r["kind"], r["rewritten"]) for r in plan.rewrites],
                         [("vocabulary", "Led triage of runner outages with the platform team")])
        acme = self.entry(plan, "Acme Corp")
        self.assertNotIn("Kubernetes", " ".join(c.plain for c in acme.candidates))  # [NO]
        P.build_provenance(plan, analysis.master, set())

    def test_repair_rules_restate_rejected_bullets_in_master_wording(self):
        template = TEMPLATE.replace(
            DEPENDENT_TEMPLATE_BULLETS, DEPENDENT_TEMPLATE_BULLETS +
            "            \\resumeItem{Ran \\textbf{GitLab CI/CD} pipelines for 60+ services}\n")
        self.write_rules({"find": "60+", "replace": "50+", "repair": True},
                         {"find": "Cut deploy time 45%", "replace": "Cut deploy time 30%",
                          "repair": True})
        analysis, plan = self.plan(template=template)
        self.assertEqual([r["rewritten"] for r in plan.rewrites],
                         ["Ran GitLab CI/CD pipelines for 50+ services"])
        self.assertIn("'60'", plan.rewrites[0]["rejected_because"])
        acme = self.entry(plan, "Acme Corp")
        # "Cut deploy time 30% with a Python tool" would only restate a kept bullet
        self.assertFalse([c for c in acme.candidates if c.plain.endswith("a Python tool")])
        P.build_provenance(plan, analysis.master, set())

    def test_bad_rewrite_files_are_refused(self):
        self.write_rules({"find": "x", "replace": "y", "when": ["not_a_concept"]})
        with self.assertRaises(P.TemplateStructureError):
            P.load_rewrites(self.root)
        (self.root / P.REWRITES_NAME).write_text("{\"rewrites\": [{\"find\": \"\"}]}")
        with self.assertRaises(P.TemplateStructureError):
            P.load_rewrites(self.root)

    def test_interests_list_unmet_asks_traced_to_the_jd(self):
        template = TEMPLATE.replace("\\end{document}", INTERESTS.split("\n\n\\end{document}")[0]
                                    + "\n\n\\end{document}")
        jd = JD_PROCEED + "- Experience with Apache Spark and A/B testing is a plus\n"
        analysis, plan = self.plan(jd, template=template)
        additional = next(si for si, sec in enumerate(plan.template.sections)
                          if sec.title == "Additional")
        items = [P.latex_to_plain(i) for i in plan.kv_items[(additional, 1)]]
        self.assertEqual(sorted(items), ["A/B testing", "Apache Spark"])
        self.assertNotIn("Kubernetes", items)  # asked, but [NO]
        claims = [c for c in P.build_provenance(plan, analysis.master, set())["claims"]
                  if c["type"] == "interest_gap"]
        self.assertEqual(len(claims), 2)
        for claim in claims:
            self.assertTrue(claim["jd_requirements"] and claim["jd_lines"], claim)
            self.assertEqual(claim["evidence"], [])  # an interest, never a claimed skill

    def test_core_skills_with_evidence_always_appear(self):
        jd = JD_PROCEED + "- Helm and Kubernetes are required\n"
        _a, plan = self.plan(jd)
        skills = next(si for si, sec in enumerate(plan.template.sections)
                      if sec.title == "Technical Skills")
        shown = [P.latex_to_plain(i) for li in plan.kv_order[skills]
                 for i in plan.kv_items[(skills, li)]]
        self.assertIn("PostgreSQL", shown)  # core, evidenced, absent from the template
        self.assertIn("Python", shown)
        self.assertNotIn("Kubernetes", shown)  # [NO]
        self.assertNotIn("Helm", shown)  # exposure only
        self.assertTrue(any("Added core skill 'PostgreSQL'" in w for w in plan.warnings))

    def test_summary_has_no_label_opener(self):
        for bad in ("Relevant experience: built things.", "Machine learning experience: x.",
                    "Also built a tool."):
            self.assertTrue(P.BANNED_SUMMARY_OPENERS.match(bad), bad)
        _a, plan = self.plan()
        for sentence in plan.summary_sentences:
            self.assertFalse(P.BANNED_SUMMARY_OPENERS.match(P.latex_to_plain(sentence)), sentence)
        self.assertLessEqual(len(" ".join(P.latex_to_plain(s) for s in plan.summary_sentences)),
                             P.SUMMARY_MAX_CHARS)

    def test_selection_maps_bullets_to_requirements(self):
        _a, plan = self.plan()
        self.assertFalse(plan.selection_check["violation"], plan.selection_check)
        acme = self.entry(plan, "Acme Corp")
        for cand in acme.selected:
            self.assertTrue(cand.requirement_ids, cand.plain)
        mapped = {rid for ep in plan.entries.values() if ep.keep for c in ep.selected
                  for rid in c.requirement_ids}
        self.assertIn("R04", mapped)  # FastAPI and PostgreSQL, proven only by Gamma App

    def test_skills_reordered_and_leftovers_dropped(self):
        template = TEMPLATE.replace("\\end{document}", INTERESTS.split("\n\n\\end{document}")[0]
                                    + "\n\n\\end{document}")
        _a, plan = self.plan(template=template)
        skills = next(si for si, sec in enumerate(plan.template.sections)
                      if sec.title == "Technical Skills")
        rows = [plan.template.sections[skills].kv_lines[li].label
                for li in plan.kv_order[skills]]
        self.assertEqual(rows[0], "Cloud and DevOps")  # Docker + GitLab CI/CD: the job's core
        additional = next(si for si, sec in enumerate(plan.template.sections)
                          if sec.title == "Additional")
        labels = {kv.label: li for li, kv in enumerate(plan.template.sections[additional].kv_lines)}
        self.assertEqual(plan.kv_items[(additional, labels["Interests"])], [])  # Helm: not asked
        self.assertTrue(plan.kv_items[(additional, labels["Languages"])])
        _a, asked = self.plan(JD_PROCEED + "- Helm is a plus\n", template=template)
        self.assertEqual([P.latex_to_plain(i) for i in asked.kv_items[(additional,
                                                                      labels["Interests"])]],
                         ["Helm"])


HAVE_TEX = bool(shutil.which("pdflatex") and shutil.which("pdftotext"))


@unittest.skipUnless(HAVE_REAL_BUILD := (REAL_MASTER.is_file() and HAVE_TEX and
                                         (ROOT / "template.tex").is_file()),
                     "real master profile, template or TeX toolchain absent")
class RegeneratedResumesTests(unittest.TestCase):
    """The reviewed jobs, rebuilt end to end with the real files (outputs to a temp dir).

    Affirm scores below the skill-coverage gate, so it is built with --override-low-fit;
    its exit code stays REJECT - LOW FIT.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name)
        cls.builds = {}
        for name, jd, extra in (("cohere", COHERE_JD, []), ("stripe", STRIPE_JD, []),
                                ("affirm", AFFIRM_JD, ["--override-low-fit"])):
            lines: list[str] = []
            code = P.run([str(jd), "--workspace", str(ROOT), "--output-dir", str(cls.out),
                          "--master", str(REAL_MASTER), *extra], out=lines.append)
            report = json.loads(next(path for path in cls.out.glob(f"{name}_*_analysis.json")
                                     if not path.name.endswith("_gap_analysis.json")).read_text())
            tex = (cls.out / report["resume"]["files"]["tex"]).read_text()
            cls.builds[name] = (code, report, tex, lines)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def summary(self, tex: str) -> str:
        body = tex.split("\\section{Summary}", 1)[1].split("\\section{", 1)[0]
        body = re.search(r"\\item \\small\{(.*)\}\s*\\end\{itemize\}", body, re.S).group(1)
        return P.collapse_ws(P.latex_to_plain(body)).strip().lower()

    def items(self, tex: str) -> list[str]:
        return [P.latex_to_plain(m) for m in re.findall(r"\\resumeItem\{(.*)\}", tex)]

    def skills(self, tex: str) -> dict[str, list[str]]:
        rows = re.findall(r"\\textbf\{([^}]*)\}\{: (.*?)\}", tex)
        return {label: [i.strip() for i in P.latex_to_plain(items).split(",")]
                for label, items in rows}

    def test_all_build_verified_one_page(self):
        for name, (code, report, _tex, lines) in self.builds.items():
            expected = P.EXIT_LOW_FIT if name == "affirm" else P.EXIT_OK
            self.assertEqual(code, expected, (name, lines))
            self.assertTrue(report["resume"]["single_page_verified"], name)

    def test_every_selected_bullet_maps_to_a_requirement_when_coverage_remains(self):
        for name, (_c, report, _t, _l) in self.builds.items():
            check = report["fit"]["selection_check"]
            self.assertFalse(check["violation"], (name, check))

    def test_resume_fit_within_one_point_of_profile_fit(self):
        for name, (_c, report, _t, _l) in self.builds.items():
            fit = report["fit"]
            self.assertGreaterEqual(fit["resume_fit"], fit["profile_fit"] - 1, name)
            self.assertFalse(fit["underselling"], name)

    def test_strongest_evidence_is_included(self):
        stripe = self.builds["stripe"][2]
        self.assertIn("LapZero", stripe)
        self.assertIn("\\textbf{PPO}", stripe)
        cohere = self.builds["cohere"][2]
        self.assertTrue("Upgraded \\textbf{Maven} across the pipelines for 5 banks" in cohere
                        or "Built a preflight stage" in cohere)
        # The triage rewrite (rewrites.json) restates the same verified bullet.
        self.assertTrue("Triaged failing pipelines for teams from error code to" in cohere or
                        "Debugged failing pipelines from error code to" in cohere)

    def test_summary_is_tailored(self):
        stripe = self.summary(self.builds["stripe"][2])
        self.assertTrue(re.search(r"\bml\b|machine learning", stripe), stripe)
        affirm = self.summary(self.builds["affirm"][2])
        self.assertTrue(re.search(r"\btests?\b|\bci\b", affirm), affirm)
        cohere = self.summary(self.builds["cohere"][2])
        self.assertIn("deploy", cohere)
        self.assertIn("canadian citizen", cohere)
        for name, (_c, _r, tex, _l) in self.builds.items():
            summary = self.summary(tex)
            self.assertNotIn("devops and platform engineer who automates", summary, name)
            self.assertNotRegex(summary, r"(?:^|[.] )(?:also\b|[\w ]{0,30}experience:)", name)
            self.assertLessEqual(len(summary), P.SUMMARY_MAX_CHARS, name)

    def test_affirm_test_infra(self):
        _code, report, tex, _l = self.builds["affirm"]
        skills = [item for row in self.skills(tex).values() for item in row]
        for skill in ("Python", "TypeScript", "pytest"):
            self.assertIn(skill, skills)
        self.assertIn("LapZero", tex)
        self.assertTrue(any("eval harness" in item for item in self.items(tex)))
        self.assertGreaterEqual(report["fit"]["resume_fit"], 6.5)

    def test_stripe_mle(self):
        _code, report, tex, _l = self.builds["stripe"]
        items = self.items(tex)
        self.assertIn("Roamly", tex)
        self.assertTrue(any("eval harness" in item for item in items))
        self.assertTrue(any("suggests what's near the user" in item for item in items))
        self.assertGreaterEqual(report["fit"]["resume_fit"], 5.5)
        interests = self.skills(tex).get("Interests", [])
        self.assertTrue({"Apache Spark", "contextual bandits", "A/B testing"} & set(interests),
                        interests)
        for dropped in ("Traced the mismatches", "Finished that cleanup"):  # flagged for Stripe
            self.assertNotIn(dropped, tex)

    def test_flagged_bullets_are_dropped(self):
        flagged = ("Ran floor operations", "CEO visit", "truckload", "trailer loads",
                   "Shipped a \\textbf{Power BI} report", "Fed \\textbf{Power BI}", "Truth Social",
                   "circadian")
        for name, (_c, _r, tex, _l) in self.builds.items():
            for text in flagged:
                self.assertNotIn(text, tex, name)

    def test_affirm_shows_test_infrastructure_from_every_source(self):
        bullets = " ".join(self.items(self.builds["affirm"][2]))
        for text in ("1,147 Python tests", "eval harness", "9 case validation suite",
                     "preflight stage", "Built validation gates"):
            self.assertIn(text, bullets)

    def test_three_bullets_per_experience_and_no_leftovers(self):
        for name, (_c, report, tex, _l) in self.builds.items():
            for entry in report["resume"]["selected_content"]:
                if entry["section"] == "Experience":
                    self.assertGreaterEqual(len(entry["bullets"]), 3, (name, entry["entry"]))
            self.assertNotIn("WATonomous", tex, name)
            self.assertNotIn("Familiar with", tex, name)
            self.assertNotIn("Argo CD", tex, name)
            self.assertEqual(report["fit"]["dependency_lint"], [], name)

    def test_dependent_bullets_stay_with_their_parent(self):
        for name, (_c, _r, tex, _l) in self.builds.items():
            items = [P.latex_to_plain(m) for m in re.findall(r"\\resumeItem\{(.*)\}", tex)]
            for index, item in enumerate(items):
                if item.startswith("Drove it"):
                    self.assertTrue(items[index - 1].startswith("Automated access requests"),
                                    name)


# ---------------------------------------------------------------------------
# Regression: the two reviewed runs (needs the real master profile and pdftotext)
# ---------------------------------------------------------------------------
HAVE_REAL = REAL_MASTER.is_file() and COHERE_PDF.is_file() and bool(shutil.which("pdftotext"))


def pdf_text(pdf: Path) -> str:
    return subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True,
                          text=True, check=True).stdout


@unittest.skipUnless(HAVE_REAL, "real master profile, regression PDFs or pdftotext absent")
class ReviewedResumesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = P.parse_master(REAL_MASTER)
        cls.fits = {}
        for name, jd_path, pdf in (("cohere", COHERE_JD, COHERE_PDF),
                                   ("stripe", STRIPE_JD, STRIPE_PDF)):
            analysis = P.analyze(P.read_job_description(jd_path, REGRESSION), cls.master)
            lines = P.resume_lines_from_text(pdf_text(pdf), cls.master)
            cls.fits[name] = (analysis, P.assess_fit(analysis.coverage, lines), lines)

    def test_existing_resumes_score_between_3_and_5(self):
        for name, (_a, fit, _l) in self.fits.items():
            self.assertGreaterEqual(fit["resume_fit"], 3.0, name)
            self.assertLessEqual(fit["resume_fit"], 5.0, name)
            self.assertTrue(fit["underselling"], name)

    def test_profile_fit_matches_the_manual_review(self):
        # Manual review: about 7 (Cohere) and about 6 ignoring the 3-year minimum (Stripe).
        # The rubric gives 7.8 and 7.1; Stripe stays higher because real evidence for
        # deploying, monitoring and debugging comes from DevOps work rather than ML systems.
        cohere = self.fits["cohere"][1]["profile_fit"]
        stripe = self.fits["stripe"][1]["profile_fit"]
        self.assertTrue(6.0 <= cohere <= 8.0, cohere)
        self.assertTrue(5.0 <= stripe <= 7.5, stripe)
        self.assertGreater(cohere, stripe)

    def test_hard_requirement_flags(self):
        stripe = self.fits["stripe"][1]
        self.assertIn("years: not_met", stripe["hard_requirement_flags"])
        self.assertEqual(stripe["profile_fit_with_hard_requirements"], 4.0)
        self.assertIn("TN status", stripe["work_authorization_note"])
        cohere = self.fits["cohere"][1]
        self.assertIn("clearance: unknown", cohere["hard_requirement_flags"])
        self.assertTrue(cohere["work_authorization_note"].startswith("Canadian citizen"))

    def test_false_positive_lines_do_not_count(self):
        for name, (_a, fit, lines) in self.fits.items():
            quotes = [t["resume"]["quote"] for row in fit["requirements"] for t in row["terms"]
                      if t["resume"]]
            for quote in quotes:
                self.assertNotIn("stress tested", quote, name)
                self.assertNotIn("Co-op evaluations", quote, name)
                self.assertNotIn("WATonomous", quote, name)
                self.assertNotIn("Autonomous Robotics", quote, name)

    def test_the_previous_scores_overstated_fit(self):
        # The skill-coverage gate called these 100 and 78; the resumes show far less.
        for name, (analysis, fit, _l) in self.fits.items():
            self.assertLess(fit["resume_fit"] * 10, analysis.score, name)


@unittest.skipUnless(HAVE_REAL and STRIPE_V2_PDF.is_file() and AFFIRM_PDF.is_file(),
                     "round-2 regression PDFs absent")
class RoundTwoResumesTests(unittest.TestCase):
    """The round-2 review: resume fit never exceeds profile fit on any fixture."""

    def test_resume_fit_never_exceeds_profile_fit(self):
        master = P.parse_master(REAL_MASTER)
        for jd_path, pdf in ((COHERE_JD, COHERE_PDF), (STRIPE_JD, STRIPE_PDF),
                             (STRIPE_JD, STRIPE_V2_PDF), (AFFIRM_JD, AFFIRM_PDF)):
            analysis = P.analyze(P.read_job_description(jd_path, REGRESSION), master)
            fit = P.assess_fit(analysis.coverage, P.resume_lines_from_text(pdf_text(pdf), master))
            self.assertLessEqual(fit["resume_fit"], fit["profile_fit"], pdf.name)
            self.assertTrue(1.0 <= fit["resume_fit"] <= 10.0, pdf.name)


if __name__ == "__main__":
    unittest.main()
