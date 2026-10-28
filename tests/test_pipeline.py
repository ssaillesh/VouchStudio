"""Deterministic unit and integration tests for pipeline.py (stdlib unittest)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures import (JD_PROCEED, JD_REJECT, MASTER, TEMPLATE, FakeTex, fake_which,
                      load_pipeline, make_workspace)

P = load_pipeline()


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name))
        self.outputs = self.root / "outputs"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def jd(self, text: str, name: str = "job.md"):
        path = self.root / "target_jobs" / name
        path.write_text(text, encoding="utf-8")
        return P.read_job_description(path, self.root)

    def master(self, strict: bool = False):
        return P.parse_master(self.root / "master.md", strict_no=strict)

    def analysis(self, text: str, strict: bool = False):
        return P.analyze(self.jd(text), self.master(strict))

    def req(self, analysis, name: str):
        for req in analysis.requirements:
            if req.name == name:
                return req
        self.fail(f"requirement {name!r} not extracted: {[r.name for r in analysis.requirements]}")

    def run_cli(self, *args, runner=None, which=None):
        lines: list[str] = []
        code = P.run([*args, "--workspace", str(self.root), "--output-dir", str(self.outputs)],
                     which=which or fake_which(), runner=runner or FakeTex(), out=lines.append)
        return code, lines


# ---------------------------------------------------------------------------
# 13.1 Input and workspace
# ---------------------------------------------------------------------------
class InputTests(WorkspaceCase):
    def test_valid_md_and_txt(self):
        md = self.jd(JD_PROCEED, "a.md")
        txt = self.jd(JD_PROCEED, "b.txt")
        self.assertEqual(md.company, "Example Co")
        self.assertEqual(md.role, "Platform Engineer")
        self.assertEqual(txt.sha256, md.sha256)
        self.assertEqual(md.display_path, "target_jobs/a.md")

    def test_missing_input(self):
        with self.assertRaises(P.InvalidInputError):
            P.read_job_description(self.root / "target_jobs" / "nope.md", self.root)

    def test_empty_input(self):
        with self.assertRaises(P.InvalidInputError):
            self.jd("   \n\n", "empty.md")

    def test_unsupported_extension(self):
        path = self.root / "target_jobs" / "job.pdf"
        path.write_text("x", encoding="utf-8")
        with self.assertRaises(P.InvalidInputError) as ctx:
            P.read_job_description(path, self.root)
        self.assertIn(".md or .txt", str(ctx.exception))

    def test_binary_input_rejected(self):
        path = self.root / "target_jobs" / "bin.md"
        path.write_bytes(b"abc\x00def")
        with self.assertRaises(P.InvalidInputError):
            P.read_job_description(path, self.root)

    def test_cli_invalid_input_exit_code(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "missing.md"))
        self.assertEqual(code, P.EXIT_INVALID_INPUT)
        self.assertTrue(lines[0].startswith("ERROR"))

    def test_input_is_never_modified(self):
        path = self.root / "target_jobs" / "proceed.md"
        before = path.read_bytes()
        self.run_cli(str(path))
        self.assertEqual(path.read_bytes(), before)

    def test_missing_master(self):
        (self.root / "master.md").unlink()
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_MISSING_PREREQ)
        self.assertIn("never invents career history", lines[0])
        self.assertFalse(self.outputs.exists())

    def test_ambiguous_master_candidates(self):
        (self.root / "master_profile.md").write_text(MASTER, encoding="utf-8")
        with self.assertRaises(P.MissingPrerequisiteError):
            P.find_master(self.root, None)

    def test_missing_template(self):
        (self.root / "template.tex").unlink()
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_MISSING_PREREQ)
        self.assertIn("never invents a template", lines[0])

    def test_ambiguous_template_structure_refused(self):
        bad = TEMPLATE.replace("\\section{Education}", "\\section{Education}\nSome loose text")
        (self.root / "template.tex").write_text(bad.replace(
            "\\resumeSubHeadingListStart\n    \\resumeSubheading\n        {Example University}",
            "\\resumeSubHeadingListStart\n    stray text\n    \\resumeSubheading\n"
            "        {Example University}"), encoding="utf-8")
        with self.assertRaises(P.TemplateStructureError):
            P.parse_template(self.root / "template.tex")

    def test_output_directory_created(self):
        self.assertFalse(self.outputs.exists())
        code, _ = self.run_cli(str(self.root / "target_jobs" / "reject.md"))
        self.assertEqual(code, P.EXIT_LOW_FIT)
        self.assertTrue((self.outputs / "other-co_data-scientist_analysis.json").is_file())

    def test_slugify(self):
        self.assertEqual(P.slugify("../../etc/passwd Role!!"), "etc-passwd-role")
        self.assertEqual(P.slugify("Café & Co. (Toronto)"), "cafe-co-toronto")
        self.assertLessEqual(len(P.slugify("x" * 500)), P.MAX_SLUG_LENGTH)
        self.assertEqual(P.slugify("***"), "")

    def test_path_traversal_prevented(self):
        self.outputs.mkdir()
        for name in ("../evil.json", "/etc/passwd", "a/b.json", ".hidden", ""):
            with self.assertRaises(P.OutputError):
                P.safe_output_path(self.outputs, name)
        jd = self.jd("Company: ../../evil\nRole: ../../../root\n\n## Requirements\n- Python\n")
        base, _ = P.output_base(jd, self.outputs)
        self.assertEqual(base, "evil_root")
        self.assertEqual(P.safe_output_path(self.outputs, base + "_analysis.json").parent,
                         self.outputs.resolve())

    def test_fallback_names_from_filename(self):
        jd = self.jd("We like tea.\n\n- Python required\n", "devops_role.md")
        self.assertEqual(jd.company, "unknown-company")
        self.assertEqual(jd.role, "devops role")
        self.assertTrue(any("Company name not found" in w for w in jd.warnings))

    def test_collision_gets_deterministic_suffix(self):
        self.run_cli(str(self.root / "target_jobs" / "reject.md"))
        other = self.root / "target_jobs" / "reject_copy.md"
        other.write_text(JD_REJECT, encoding="utf-8")
        jd = P.read_job_description(other, self.root)
        base, notes = P.output_base(jd, self.outputs)
        self.assertNotEqual(base, "other-co_data-scientist")
        self.assertTrue(base.startswith("other-co_data-scientist-"))
        self.assertTrue(notes)
        again, _ = P.output_base(P.read_job_description(
            self.root / "target_jobs" / "reject.md", self.root), self.outputs)
        self.assertEqual(again, "other-co_data-scientist")


# ---------------------------------------------------------------------------
# 13.2 Requirement parsing and scoring
# ---------------------------------------------------------------------------
class ParsingAndScoringTests(WorkspaceCase):
    def test_required_vs_preferred_sections(self):
        a = self.analysis(JD_PROCEED)
        self.assertEqual(self.req(a, "FastAPI").category, "required")
        self.assertEqual(self.req(a, "Kubernetes").category, "preferred")
        self.assertEqual(self.req(a, "Python").category, "mandatory")

    def test_explicit_requirement_not_demoted_by_section(self):
        a = self.analysis("## Nice to have\n- FastAPI is required for this team\n- Docker\n")
        self.assertEqual(self.req(a, "FastAPI").category, "mandatory")
        self.assertEqual(self.req(a, "Docker").category, "preferred")

    def test_preferred_wording_inside_requirements(self):
        a = self.analysis("## Requirements\n- Python\n- Kubernetes experience is a plus\n")
        self.assertEqual(self.req(a, "Python").category, "required")
        self.assertEqual(self.req(a, "Kubernetes").category, "preferred")

    def test_mixed_markers_use_nearest(self):
        a = self.analysis("## Qualifications\n- Python is required, Kubernetes is a plus\n")
        self.assertEqual(self.req(a, "Python").category, "mandatory")
        self.assertEqual(self.req(a, "Kubernetes").category, "preferred")

    def test_info_sections_ignored(self):
        a = self.analysis("## Requirements\n- Docker\n\n## Benefits\n- Python training budget\n")
        self.assertNotIn("Python", [r.name for r in a.requirements])

    def test_not_required_mentions_excluded(self):
        a = self.analysis("## Requirements\n- Docker\n- Kubernetes is not required\n")
        self.assertNotIn("Kubernetes", [r.name for r in a.requirements])
        self.assertEqual(a.not_required[0]["skill"], "Kubernetes")

    def test_weights_are_constants(self):
        self.assertEqual(P.CATEGORY_WEIGHTS, {"mandatory": 3, "required": 2, "preferred": 1})
        a = self.analysis(JD_PROCEED)
        for req in a.requirements:
            self.assertEqual(req.weight, P.CATEGORY_WEIGHTS[req.category])

    def test_threshold_exactly_65(self):
        self.assertEqual(P.FIT_THRESHOLD, 65)
        self.assertEqual(P.verdict_for(65), "PROCEED")
        self.assertEqual(P.verdict_for(64), "REJECT - LOW FIT")
        reqs = []
        for index in range(10):  # 10 required (weight 2) => total 20
            req = P.Requirement(f"r{index}", None, "skill", "required", [1], "", "")
            req.status = P.STATUS_MATCHED if index < 6 else P.STATUS_NOT_EVIDENCED
            reqs.append(req)
        extra = P.Requirement("p", None, "skill", "preferred", [1], "", "")
        extra.status = P.STATUS_MATCHED
        score, breakdown = P.compute_score(reqs + [extra])  # 13 / 21 = 61.9
        self.assertEqual(score, 62)
        reqs[6].status = P.STATUS_MATCHED  # (14 + 1) / 21 = 71.4
        self.assertEqual(P.compute_score(reqs + [extra])[0], 71)
        exact = [P.Requirement(f"x{i}", None, "skill", "preferred", [1], "", "")
                 for i in range(20)]
        for req in exact[:13]:
            req.status = P.STATUS_MATCHED
        score, _ = P.compute_score(exact)  # 13 / 20 = 65.0
        self.assertEqual(score, 65)
        self.assertEqual(P.verdict_for(score), "PROCEED")
        self.assertEqual(breakdown["total_weight"], 21)

    def test_rounding_half_up(self):
        reqs = [P.Requirement(f"x{i}", None, "skill", "preferred", [1], "", "") for i in range(8)]
        for req in reqs[:5]:
            req.status = P.STATUS_MATCHED  # 62.5 -> 63
        self.assertEqual(P.compute_score(reqs)[0], 63)

    def test_scores_above_and_below_threshold(self):
        self.assertGreaterEqual(self.analysis(JD_PROCEED).score, 65)
        low = self.analysis(JD_REJECT)
        self.assertLess(low.score, 65)
        self.assertEqual(low.verdict, "REJECT - LOW FIT")

    def test_empty_requirements_score_zero(self):
        a = self.analysis("# Friendly Team\n\nWe are a nice company that values kindness.\n")
        self.assertEqual(a.score, 0)
        self.assertEqual(a.verdict, "REJECT - LOW FIT")
        self.assertTrue(any("No requirements" in w for w in a.warnings))

    def test_unknown_skill_is_unverified(self):
        a = self.analysis("## Requirements\n- Experience with Crossplane and Python\n")
        req = self.req(a, "Crossplane")
        self.assertEqual(req.kind, "unknown_named")
        self.assertEqual(req.status, P.STATUS_NOT_EVIDENCED)
        self.assertEqual(req.confidence, "low")

    def test_missing_positive_evidence(self):
        a = self.analysis("## Requirements\n- Rust\n")
        self.assertEqual(self.req(a, "Rust").status, P.STATUS_NOT_EVIDENCED)

    def test_hard_no_overrides_positive_evidence(self):
        a = self.analysis("## Requirements\n- Terraform\n- K8s\n")
        terraform = self.req(a, "Terraform")
        self.assertEqual(terraform.status, P.STATUS_NO)  # positive line exists, NO wins
        self.assertEqual(self.req(a, "Kubernetes").status, P.STATUS_NO)

    def test_exposure_table_no_marker(self):
        a = self.analysis("## Requirements\n- ELK stack\n- Helm\n")
        self.assertEqual(self.req(a, "ELK Stack").status, P.STATUS_NO)
        helm = self.req(a, "Helm")
        self.assertEqual(helm.status, P.STATUS_NOT_EVIDENCED)
        self.assertIn("Exposure-level", helm.explanation)

    def test_scoped_no_and_strict_mode(self):
        a = self.analysis("## Requirements\n- Grafana\n")
        grafana = self.req(a, "Grafana")
        self.assertEqual(grafana.status, P.STATUS_MATCHED)
        self.assertTrue(grafana.conflicts)
        strict = self.analysis("## Requirements\n- Grafana\n", strict=True)
        self.assertEqual(self.req(strict, "Grafana").status, P.STATUS_NO)

    def test_explanatory_aside_is_not_a_hard_no(self):
        master = self.master()
        self.assertIn("networking", master.global_no)
        self.assertNotIn("troubleshooting", master.global_no)

    def test_negated_sentence_is_not_evidence(self):
        a = self.analysis("## Requirements\n- Kafka\n")
        kafka = self.req(a, "Apache Kafka")
        self.assertEqual(kafka.status, P.STATUS_NOT_EVIDENCED)
        self.assertIn("negated", kafka.explanation)

    def test_old_gap_and_heading_only_are_ambiguous(self):
        a = self.analysis("## Requirements\n- Jenkins\n- Ansible\n- RBAC\n")
        self.assertEqual(self.req(a, "Jenkins").status, P.STATUS_AMBIGUOUS)
        self.assertEqual(self.req(a, "Ansible").status, P.STATUS_AMBIGUOUS)
        rbac = self.req(a, "RBAC")
        self.assertEqual(rbac.status, P.STATUS_AMBIGUOUS)
        self.assertIn("heading", rbac.explanation)

    def test_aliases_only_when_mapped(self):
        a = self.analysis("## Requirements\n- k8s\n- CI/CD\n- Azure\n- containerization\n"
                          "- container orchestration\n")
        self.assertEqual(self.req(a, "Kubernetes").status, P.STATUS_NO)
        cicd = self.req(a, "CI/CD")
        self.assertEqual(cicd.status, P.STATUS_MATCHED)
        self.assertIn("GitLab CI/CD", cicd.matched_via)
        self.assertEqual(self.req(a, "Containerization").matched_via, "Docker")
        self.assertNotEqual(self.req(a, "Container orchestration").status, P.STATUS_MATCHED)
        self.assertEqual(self.req(a, "Azure").status, P.STATUS_NOT_EVIDENCED)

    def test_languages_need_language_context(self):
        a = self.analysis("## Requirements\n- Fluent German\n")
        self.assertEqual(self.req(a, "German").status, P.STATUS_MATCHED)
        b = self.analysis("## Requirements\n- Work with our German customers on Python\n")
        self.assertNotIn("German", [r.name for r in b.requirements])

    def test_years_and_degree(self):
        a = self.analysis("## Requirements\n- 5+ years of experience with Python\n"
                          "- Bachelor's degree in Computer Science or related field\n")
        self.assertEqual(self.req(a, "5+ years of experience").status, P.STATUS_AMBIGUOUS)
        self.assertEqual(self.req(a, "Degree requirement").status, P.STATUS_AMBIGUOUS)
        b = self.analysis("## Requirements\n- Bachelor's degree in Physics or Engineering\n")
        self.assertEqual(self.req(b, "Degree requirement").status, P.STATUS_MATCHED)
        c = self.analysis("## Requirements\n- PhD in Machine Learning\n")
        self.assertEqual(self.req(c, "Degree requirement").status, P.STATUS_NOT_EVIDENCED)

    def test_stack_description_is_one_either_or_requirement(self):
        a = self.analysis("## Requirements\n- Experience with programming. We work mostly in "
                          "Java, Ruby, Python and Go.\n")
        names = [r.name for r in a.requirements]
        self.assertIn("One of: Java, Ruby, Python, Go", names)
        for single in ("Java", "Ruby", "Go", "Python"):
            self.assertNotIn(single, names)
        group = self.req(a, "One of: Java, Ruby, Python, Go")
        self.assertEqual((group.kind, group.status, group.matched_via),
                         ("skill_group", P.STATUS_MATCHED, "Python"))
        self.assertTrue(group.evidence)
        plan = P.build_resume_plan(P.parse_template(self.root / "template.tex"), a.master, a)
        self.assertTrue(plan.entries)  # groups flow through bullet ranking safely

    def test_team_activity_is_not_a_stack_list(self):
        a = self.analysis("## Requirements\n- We build the services that manage Kubernetes "
                          "and Terraform.\n- We work asynchronously with Docker and Kafka.\n")
        names = [r.name for r in a.requirements]
        self.assertFalse([n for n in names if n.startswith("One of")])
        for single in ("Kubernetes", "Terraform", "Docker", "Apache Kafka"):
            self.assertIn(single, names)

    def test_skill_is_counted_once_with_a_group(self):
        a = self.analysis("## Requirements\n- Our stack includes Kubernetes, Go and Terraform.\n"
                          "- Deep expertise in Kubernetes\n")
        self.assertEqual([r.name for r in a.requirements], ["Kubernetes"])
        b = self.analysis("## Requirements\n- Our stack includes Kubernetes, Go and Terraform."
                          "\n\n## Nice to have\n- Go\n")
        self.assertEqual([r.name for r in b.requirements], ["One of: Kubernetes, Go, Terraform"])
        self.assertEqual(b.requirements[0].category, "required")
        self.assertEqual(len(b.requirements[0].jd_lines), 2)

    def test_alternatives_without_evidence_and_all_no(self):
        a = self.analysis("## Requirements\n- Proficient in one of Rust, Scala or Kotlin\n"
                          "- Experience with any of Kubernetes or Terraform\n")
        self.assertEqual(self.req(a, "One of: Rust, Scala, Kotlin").status,
                         P.STATUS_NOT_EVIDENCED)
        self.assertEqual(self.req(a, "One of: Kubernetes, Terraform").status, P.STATUS_NO)

    def test_umbrella_examples_are_not_separate_requirements(self):
        a = self.analysis("## Requirements\n- Experience with cloud platforms such as AWS, "
                          "Azure or GCP\n")
        self.assertEqual([r.name for r in a.requirements], ["Cloud platforms"])

    def test_pay_text_is_skipped(self):
        a = self.analysis("## Bonus points\n- Docker\n\nThe typical starting salary range for "
                          "this role is listed below. In select locations (including Seattle WA "
                          "and Los Angeles CA), an alternate range may apply.\n\n"
                          "$159,800\u2014$252,800 USD\n\n- Experience managing $2M of AWS spend\n")
        names = [r.name for r in a.requirements]
        self.assertIn("Docker", names)
        self.assertIn("AWS", names)  # a real requirement that mentions money is kept
        for noise in ("Seattle WA", "Los Angeles CA", "USD"):
            self.assertNotIn(noise, names)
        self.assertTrue(any("salary or pay-range" in w for w in a.warnings))

    def test_company_name_is_not_a_requirement(self):
        stripe = self.analysis("Company: Stripe\nRole: Engineer\n\n## Requirements\n"
                               "- Successful Stripes are curious and write Python\n")
        self.assertNotIn("Stripes", [r.name for r in stripe.requirements])
        self.assertIn("Python", [r.name for r in stripe.requirements])
        gitlab = self.analysis("Company: GitLab\nRole: Backend Engineer\n\n## Requirements\n"
                               "- GitLab is the platform we build; experience with GitLab CI and "
                               "Python\n")
        names = [r.name for r in gitlab.requirements]
        self.assertNotIn("GitLab", names)
        self.assertIn("GitLab CI/CD", names)

    def test_domain_classification(self):
        self.assertEqual(self.analysis(JD_PROCEED).domain["primary"], "DevOps / SRE")
        data = self.analysis("# Data Analyst at X\n\n## Requirements\n- SQL, Power BI and "
                             "Tableau dashboards\n- Build ETL data pipelines and reporting\n")
        self.assertEqual(data.domain["primary"], "Data engineering / analytics")
        unknown = self.analysis("# Friendly Team\n\nWe value kindness.\n")
        self.assertEqual(unknown.domain["primary"], "Unknown / mixed")

    def test_stable_ordering(self):
        a = self.analysis(JD_PROCEED)
        keys = [(-P.CATEGORY_RANK[r.category], r.jd_lines[0], r.name) for r in a.requirements]
        self.assertEqual(keys, sorted(keys))


# ---------------------------------------------------------------------------
# 13.3 Evidence provenance
# ---------------------------------------------------------------------------
class ProvenanceTests(WorkspaceCase):
    def test_every_match_has_positive_evidence(self):
        a = self.analysis(JD_PROCEED)
        master = self.master()
        for req in a.requirements:
            if req.status == P.STATUS_MATCHED:
                self.assertTrue(req.evidence, req.name)
                for item in req.evidence:
                    self.assertIn(master.line(item["line"]).tag, P.POSITIVE_TAGS)

    def test_plan_rejects_unsupported_bullets(self):
        a = self.analysis(JD_PROCEED)
        plan = P.build_resume_plan(P.parse_template(self.root / "template.tex"), a.master, a)
        tex = P.render_resume(plan, set(), 0)
        body = tex.split("\\begin{document}", 1)[1]
        self.assertNotIn("Kubernetes", body)  # [NO]: summary sentence, skills item, bullet
        self.assertNotIn("Terraform", body)   # [NO]: removed from the project stack line
        self.assertNotIn("45\\%", body)       # fabricated metric
        self.assertNotIn("Built \\textbf{Grafana} dashboards for the platform", body)
        self.assertIn("\\textbf{Grafana}", body)  # allowed for Gamma App, positive there
        reasons = " ".join(" ".join(r["reasons"]) for ep in plan.entries.values()
                           for r in ep.rejected)
        self.assertIn("number '45'", reasons)
        self.assertIn("marked [NO]", reasons)
        self.assertIn("marked [NO] for this entry", reasons)

    def test_selected_bullets_trace_to_their_entry(self):
        a = self.analysis(JD_PROCEED)
        master = a.master
        plan = P.build_resume_plan(P.parse_template(self.root / "template.tex"), master, a)
        for ep in plan.entries.values():
            for cand in ep.selected:
                self.assertTrue(cand.source_lines, cand.plain)
                for line_no in cand.source_lines:
                    line = master.line(line_no)
                    self.assertTrue(line.entry_id == ep.master_entry_id
                                    or ep.master_entry_id in line.refs, (cand.plain, line_no))
                    self.assertIn(line.tag, P.POSITIVE_TAGS)

    def test_no_fabricated_metrics(self):
        master = self.master()
        scope = P.build_scope(master, "2.1")
        self.assertFalse(P.verify_claim("Cut deploy time 31% with Python", master, scope).ok)
        self.assertFalse(P.verify_claim("Saved $200K with Python tooling", master, scope).ok)
        self.assertTrue(P.verify_claim("Cut deploy time 30% with a Python tool", master,
                                       scope).ok)
        wrong_context = P.verify_claim("Ran Docker pipelines for 30 teams", master, scope)
        self.assertFalse(wrong_context.ok)

    def test_ambiguous_evidence_not_upgraded(self):
        master = self.master()
        scope = P.build_scope(master, "2.1")
        check = P.verify_claim("Wrote Ansible playbooks for patching", master, scope)
        self.assertFalse(check.ok)  # [OLD] block, not positive


# ---------------------------------------------------------------------------
# 13.4 Resume generation
# ---------------------------------------------------------------------------
class ResumeGenerationTests(WorkspaceCase):
    def plan(self, text: str = JD_PROCEED):
        a = self.analysis(text)
        return a, P.build_resume_plan(P.parse_template(self.root / "template.tex"), a.master, a)

    def test_ranking_and_tie_break(self):
        _a, plan = self.plan()
        acme = plan.entries[(2, 0)]
        rel = [c.relevance for c in acme.selected]
        self.assertEqual(rel, sorted(rel, reverse=True))
        for first, second in zip(acme.selected, acme.selected[1:]):
            if first.relevance == second.relevance:
                self.assertLess(first.bullet_index, second.bullet_index)
        self.assertLessEqual(len(acme.selected), P.CANDIDATE_BULLETS_PER_ENTRY)
        # Docker (must have) + GitLab CI/CD + Ubuntu outranks the Python-only bullet
        self.assertEqual([c.bullet_index for c in acme.selected[:2]], [1, 0])

    def test_targeted_keywords_supported(self):
        a, plan = self.plan()
        for ep in plan.entries.values():
            for cand in ep.selected:
                for key in cand.skills:
                    self.assertNotIn(key, a.master.global_no)

    def test_latex_escaping(self):
        self.assertEqual(P.latex_escape("50% & $5 #1 a_b {x} ~ ^"),
                         r"50\% \& \$5 \#1 a\_b \{x\} \textasciitilde{} \textasciicircum{}")
        self.assertEqual(P.latex_escape("C:\\path"), r"C:\textbackslash{}path")
        self.assertEqual(P.latex_escape("µ 3×"), r"\ensuremath{\mu} 3\ensuremath{\times}")
        self.assertEqual(P.latex_escape("Zürich"), "Zürich")

    def test_metrics_preserved_verbatim(self):
        _a, plan = self.plan()
        tex = P.render_resume(plan, set(), 0)
        self.assertIn("Cut deploy time 30\\% with a \\textbf{Python} tool across 50+ services",
                      tex)

    def test_template_preamble_preserved(self):
        _a, plan = self.plan()
        tex = P.render_resume(plan, set(), 0)
        preamble = TEMPLATE.split("\\begin{document}")[0]
        self.assertTrue(tex.startswith(preamble.rstrip("\n")))
        self.assertTrue(tex.rstrip().endswith("\\end{document}"))
        self.assertEqual(tex.count("\\begin{document}"), 1)
        spaced = P.render_resume(plan, set(), 2)
        self.assertIn("bounded spacing level 2", spaced)
        self.assertNotIn("geometry}{", spaced.split("jobauto")[1])

    def test_no_resume_below_threshold(self):
        code, _ = self.run_cli(str(self.root / "target_jobs" / "reject.md"))
        self.assertEqual(code, P.EXIT_LOW_FIT)
        self.assertEqual(sorted(p.name for p in self.outputs.iterdir()),
                         ["other-co_data-scientist_analysis.json",
                          "other-co_data-scientist_gap_analysis.json"])
        report = json.loads((self.outputs / "other-co_data-scientist_analysis.json").read_text())
        self.assertEqual(report["resume"]["status"], "skipped_low_fit")
        self.assertEqual(report["gap_analysis"], "other-co_data-scientist_gap_analysis.json")

    def test_override_low_fit_is_explicit(self):
        code, _ = self.run_cli(str(self.root / "target_jobs" / "reject.md"),
                               "--override-low-fit")
        report = json.loads((self.outputs / "other-co_data-scientist_analysis.json").read_text())
        self.assertEqual(report["verdict"], "REJECT - LOW FIT")  # verdict never changes
        self.assertTrue(report["resume"]["override_low_fit"])
        self.assertEqual(report["resume"]["status"], "verified")
        self.assertEqual(code, P.EXIT_LOW_FIT)

    def test_output_naming_and_report_schema(self):
        code, _ = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_OK)
        names = sorted(p.name for p in self.outputs.iterdir() if p.is_file())
        self.assertEqual(names, ["example-co_platform-engineer_analysis.json",
                                 "example-co_platform-engineer_briefing.json",
                                 "example-co_platform-engineer_briefing.md",
                                 "example-co_platform-engineer_resume.log",
                                 "example-co_platform-engineer_resume.pdf",
                                 "example-co_platform-engineer_resume.tex"])
        report = json.loads((self.outputs / names[0]).read_text())
        for key in ("company", "role", "source_job_description", "fit_score", "verdict",
                    "primary_domain", "secondary_domains", "strong_matches", "missing_gaps",
                    "required_skills", "preferred_skills", "evidence_coverage",
                    "score_breakdown", "parsing_warnings"):
            self.assertIn(key, report)
        self.assertEqual(list(report)[:5], ["company", "role", "source_job_description",
                                            "fit_score", "verdict"])
        self.assertEqual(report["source_job_description"], "target_jobs/proceed.md")
        self.assertTrue(report["resume"]["single_page_verified"])
        self.assertFalse((self.outputs / ".build").exists() and
                         any((self.outputs / ".build").iterdir()))


# ---------------------------------------------------------------------------
# 13.5 PDF compilation and one-page fitting (mocked toolchain)
# ---------------------------------------------------------------------------
def bullets_in(tex: str) -> int:
    return tex.count("\\resumeItem{")


class CompileAndFitTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.analysis_obj = self.analysis(JD_PROCEED)
        self.template = P.parse_template(self.root / "template.tex")
        self.plan_obj = P.build_resume_plan(self.template, self.analysis_obj.master,
                                            self.analysis_obj)
        self.outputs.mkdir()

    def tools(self, available=("pdflatex", "pdftotext", "pdfinfo")):
        return P.detect_tools(self.template, fake_which(available))

    def fit(self, fake):
        return P.fit_to_one_page(self.plan_obj, self.tools(), self.outputs / ".build" / "x",
                                 "x_resume", self.root, fake)

    def test_engine_selection(self):
        self.assertEqual(self.template.engine_requirement, "pdflatex")
        self.assertEqual(self.tools().engine_name, "pdflatex")
        xe = P.ParsedTemplate(self.root, "\\usepackage{fontspec}\n", "", [], "", "xelatex", "")
        self.assertIsNone(P.detect_tools(xe, fake_which(("pdflatex",))).engine_path)
        self.assertEqual(P.detect_tools(xe, fake_which(("xelatex",))).engine_name, "xelatex")
        real = P.parse_template(Path(__file__).resolve().parent.parent / "template.tex")
        self.assertEqual(real.engine_requirement, "pdflatex")

    def test_missing_compiler_writes_tex_but_no_pdf(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   which=fake_which(("pdftotext", "pdfinfo")))
        self.assertEqual(code, P.EXIT_MISSING_PREREQ)
        report = json.loads(next(self.outputs.glob("*_analysis.json")).read_text())
        self.assertEqual(report["resume"]["status"], "not_compiled")
        self.assertFalse(report["resume"]["single_page_verified"])
        self.assertFalse(report["resume"]["compiled"])
        self.assertTrue((self.outputs / "example-co_platform-engineer_resume.tex").exists())
        self.assertFalse((self.outputs / "example-co_platform-engineer_resume.pdf").exists())

    def test_missing_pdftotext_is_a_prerequisite_error(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   which=fake_which(("pdflatex", "pdfinfo")))
        self.assertEqual(code, P.EXIT_MISSING_PREREQ)
        self.assertIn("pdftotext", lines[0])

    def test_compile_command_is_safe(self):
        fake = FakeTex()
        result = P.compile_tex("\\begin{document}x\\end{document}", self.outputs / ".b", "job",
                               self.tools(), self.root, fake)
        self.assertTrue(result.ok)
        command = fake.commands[0]
        self.assertIsInstance(command, list)
        for flag in ("-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
                     "-no-shell-escape"):
            self.assertIn(flag, command)

    def test_compilation_error(self):
        result = self.fit(FakeTex(fail=True))
        self.assertFalse(result.ok)
        self.assertIn("exited with 1", result.compile_result.error)
        code, _ = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                               runner=FakeTex(fail=True))
        self.assertEqual(code, P.EXIT_FAILED)

    def test_timeout(self):
        result = P.compile_tex("\\begin{document}x\\end{document}", self.outputs / ".b", "job",
                               self.tools(), self.root, FakeTex(timeout=True))
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

    def test_page_count_verification(self):
        tools = self.tools()
        build = self.outputs / ".b"
        build.mkdir()
        pdf = build / "a.pdf"
        pdf.write_text(json.dumps({"pages": 1, "text": "Test Person Summary"}))
        ok = P.verify_pdf(pdf, tools, ["Test Person"], FakeTex())
        self.assertTrue(ok.ok)
        self.assertEqual((ok.pages, ok.method), (1, "pdfinfo+pdftotext"))
        pdf.write_text(json.dumps({"pages": 2, "text": "Test Person"}))
        two = P.verify_pdf(pdf, tools, ["Test Person"], FakeTex())
        self.assertFalse(two.ok)
        self.assertEqual(two.pages, 2)
        pdf.write_text(json.dumps({"pages": 1, "text": "Test Person"}))
        mismatch = P.verify_pdf(pdf, tools, ["Test Person"], FakeTex(info_pages_offset=1))
        self.assertFalse(mismatch.ok)
        self.assertTrue(any("inconsistent" in r for r in mismatch.reasons))
        lost = P.verify_pdf(pdf, tools, ["Test Person", "Missing Heading"], FakeTex())
        self.assertFalse(lost.ok)
        self.assertEqual(lost.missing_content, ["Missing Heading"])
        pdf.write_text("")
        self.assertFalse(P.verify_pdf(pdf, tools, [], FakeTex()).ok)
        only_text = P.verify_pdf(build / "a.pdf", self.tools(("pdftotext",)), [], FakeTex())
        self.assertFalse(only_text.ok)

    def test_pdftotext_only_reports_limitation(self):
        build = self.outputs / ".b"
        build.mkdir()
        pdf = build / "a.pdf"
        pdf.write_text(json.dumps({"pages": 1, "text": "Test Person"}))
        check = P.verify_pdf(pdf, self.tools(("pdflatex", "pdftotext")), ["Test Person"],
                             FakeTex())
        self.assertTrue(check.ok)
        self.assertEqual(check.method, "pdftotext")
        self.assertTrue(check.limitations)

    def test_one_page_first_try(self):
        result = self.fit(FakeTex(lambda tex: 1))
        self.assertTrue(result.ok)
        self.assertEqual((result.iterations, result.spacing_level, result.removed), (1, 0, []))

    def test_multi_page_triggers_deterministic_trimming(self):
        start = bullets_in(P.render_resume(self.plan_obj, set(), 0))
        units = P.trim_sequence(self.plan_obj)
        self.assertGreaterEqual(len(units), 1)
        target = start - 1
        first = self.fit(FakeTex(lambda tex: 1 if bullets_in(tex) <= target else 2))
        second = self.fit(FakeTex(lambda tex: 1 if bullets_in(tex) <= target else 2))
        self.assertTrue(first.ok)
        self.assertEqual(first.removed, second.removed)
        self.assertEqual(first.tex, second.tex)
        self.assertLessEqual(bullets_in(first.tex), target)
        expected = [P._describe_unit(self.plan_obj, u)
                    for u, _w in P.trim_sequence(self.plan_obj)][:len(first.removed)]
        self.assertEqual([r["unit"] for r in first.removed], expected)
        self.assertEqual(first.spacing_level, 0)

    def test_spacing_is_bounded_and_applied_after_trimming(self):
        fake = FakeTex(lambda tex: 1 if "bounded spacing level 2" in tex else 2)
        result = self.fit(fake)
        self.assertTrue(result.ok)
        self.assertEqual(result.spacing_level, 2)
        self.assertEqual(sum(1 for r in result.removed if r["unit"].startswith("spacing")), 2)
        # Spacing is only reached once every removable unit is gone; the refill pass
        # then restores the content that fits at that spacing.
        self.assertEqual(result.final_removed, set())
        self.assertEqual(len(result.restored), len(P.trim_sequence(self.plan_obj)))
        self.assertNotIn("\\fontsize", result.tex)
        self.assertEqual(result.tex.count("geometry"), TEMPLATE.count("geometry"))

    def test_refill_restores_overshoot(self):
        bullets = ("            \\resumeItem{Cut deploy time 30\\% with a \\textbf{Python} tool "
                   "across 50+ services}\n"
                   "            \\resumeItem{Ran \\textbf{GitLab CI/CD} pipelines with "
                   "\\textbf{Docker} executors on \\textbf{Ubuntu} runners}\n")
        roomy = TEMPLATE.replace(bullets, bullets * 3)
        (self.root / "template.tex").write_text(roomy, encoding="utf-8")
        template = P.parse_template(self.root / "template.tex")
        plan = P.build_resume_plan(template, self.analysis_obj.master, self.analysis_obj)
        units = [u for u, _w in P.trim_sequence(plan)]
        self.assertGreaterEqual(len(units), 3)
        start = bullets_in(P.render_resume(plan, set(), 0))
        target = start - 1  # two units per iteration overshoot this by one bullet
        fake = FakeTex(lambda tex: 1 if bullets_in(tex) <= target else 2)
        result = P.fit_to_one_page(plan, P.detect_tools(template, fake_which()),
                                   self.outputs / ".build" / "r", "r_resume", self.root, fake)
        self.assertTrue(result.ok)
        self.assertEqual(bullets_in(result.tex), target)
        self.assertEqual(len(result.restored), 1)
        self.assertEqual(len(result.final_removed), 1)

    def roomy_plan(self):
        bullets = ("            \\resumeItem{Cut deploy time 30\\% with a \\textbf{Python} tool "
                   "across 50+ services}\n"
                   "            \\resumeItem{Ran \\textbf{GitLab CI/CD} pipelines with "
                   "\\textbf{Docker} executors on \\textbf{Ubuntu} runners}\n")
        (self.root / "template.tex").write_text(TEMPLATE.replace(bullets, bullets * 3),
                                                encoding="utf-8")
        template = P.parse_template(self.root / "template.tex")
        return template, P.build_resume_plan(template, self.analysis_obj.master,
                                             self.analysis_obj)

    def test_failed_refill_trials_never_leave_a_stale_pdf(self):
        # Regression: every refill trial overflowed, the last trial's 2-page PDF was left
        # in the build directory and got published as the "verified" resume.
        template, plan = self.roomy_plan()
        start = bullets_in(P.render_resume(plan, set(), 0))
        target = start - 2  # one trimming step fits exactly; every restore overflows
        fake = FakeTex(lambda tex: 1 if bullets_in(tex) <= target else 2)
        result = P.fit_to_one_page(plan, P.detect_tools(template, fake_which()),
                                   self.outputs / ".build" / "s", "s_resume", self.root, fake)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, [])
        published = json.loads(result.compile_result.pdf_path.read_text())
        self.assertEqual(published["pages"], 1)
        self.assertEqual(bullets_in(result.tex), target)

    def test_publish_step_rechecks_the_exact_pdf(self):
        original = P.fit_to_one_page

        def lying_fit(plan, tools, build_dir, jobname, texinputs, runner):
            fit = original(plan, tools, build_dir, jobname, texinputs, runner)
            pdf = json.loads(fit.compile_result.pdf_path.read_text())
            pdf["pages"] = 2  # simulate the file changing after verification
            fit.compile_result.pdf_path.write_text(json.dumps(pdf))
            return fit

        P.fit_to_one_page = lying_fit
        try:
            code, _ = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        finally:
            P.fit_to_one_page = original
        self.assertEqual(code, P.EXIT_PAGE_LIMIT)
        self.assertFalse((self.outputs / "example-co_platform-engineer_resume.pdf").exists())
        report = json.loads(next(self.outputs.glob("*_analysis.json")).read_text())
        self.assertFalse(report["resume"]["single_page_verified"])
        self.assertIn("final one-page re-check", report["resume"]["reason"])

    def test_minimum_content_floor_and_failure(self):
        result = self.fit(FakeTex(lambda tex: 2))
        self.assertFalse(result.ok)
        self.assertEqual(result.spacing_level, len(P.SPACING_LEVELS) - 1)
        self.assertLessEqual(result.iterations, P.MAX_FIT_ITERATIONS)
        self.assertTrue(result.diagnostics)
        self.assertEqual(result.final_removed,
                         {u for u, _w in P.trim_sequence(self.plan_obj)})
        for si, entries in P.active_model(self.plan_obj, result.final_removed):
            if P.template_section_kind(self.template.sections[si].title) == "experience":
                for ei, bullets in entries:
                    self.assertGreaterEqual(len(bullets), self.plan_obj.entries[(si, ei)].floor)
                    self.assertGreaterEqual(len(bullets), 1)

    def test_page_limit_exit_code_and_no_pdf_published(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   runner=FakeTex(lambda tex: 2))
        self.assertEqual(code, P.EXIT_PAGE_LIMIT)
        self.assertFalse((self.outputs / "example-co_platform-engineer_resume.pdf").exists())
        report = json.loads(next(self.outputs.glob("*_analysis.json")).read_text())
        self.assertEqual(report["resume"]["status"], "page_limit_failed")
        self.assertFalse(report["resume"]["single_page_verified"])

    def test_existing_valid_pdf_survives_failed_build(self):
        jd = str(self.root / "target_jobs" / "proceed.md")
        code, _ = self.run_cli(jd)
        self.assertEqual(code, P.EXIT_OK)
        pdf = self.outputs / "example-co_platform-engineer_resume.pdf"
        tex = self.outputs / "example-co_platform-engineer_resume.tex"
        before_pdf, before_tex = pdf.read_bytes(), tex.read_bytes()
        code, _ = self.run_cli(jd, runner=FakeTex(fail=True))
        self.assertEqual(code, P.EXIT_FAILED)
        self.assertEqual(pdf.read_bytes(), before_pdf)
        self.assertEqual(tex.read_bytes(), before_tex)
        self.assertTrue((self.outputs / "example-co_platform-engineer_resume.failed.tex").exists())
        report = json.loads(next(self.outputs.glob("*_analysis.json")).read_text())
        self.assertTrue(report["resume"]["last_valid_pdf_kept"])
        code, _ = self.run_cli(jd)
        self.assertEqual(code, P.EXIT_OK)
        self.assertFalse((self.outputs / "example-co_platform-engineer_resume.failed.tex").exists())

    def test_dry_run_writes_nothing(self):
        fake = FakeTex()
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"), "--dry-run",
                                   runner=fake)
        self.assertEqual(code, P.EXIT_OK)
        self.assertFalse(self.outputs.exists() and any(self.outputs.iterdir()))
        self.assertEqual(fake.compiles, 0)
        self.assertTrue(any("Dry run" in line for line in lines))


# ---------------------------------------------------------------------------
# 13.6 Determinism
# ---------------------------------------------------------------------------
class DeterminismTests(WorkspaceCase):
    def test_identical_runs(self):
        reports, texes = [], []
        for _ in range(2):
            a = self.analysis(JD_PROCEED)
            reports.append(P.dump_json(P.build_report(a, self.root)))
            plan = P.build_resume_plan(P.parse_template(self.root / "template.tex"), a.master, a)
            texes.append(P.render_resume(plan, set(), 0))
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(texes[0], texes[1])

    def test_cli_reports_identical(self):
        jd = str(self.root / "target_jobs" / "proceed.md")
        self.run_cli(jd)
        first = next(self.outputs.glob("*_analysis.json")).read_text()
        first_tex = next(self.outputs.glob("*_resume.tex")).read_text()
        self.run_cli(jd)
        self.assertEqual(next(self.outputs.glob("*_analysis.json")).read_text(), first)
        self.assertEqual(next(self.outputs.glob("*_resume.tex")).read_text(), first_tex)


# ---------------------------------------------------------------------------
# Real master profile and template (read-only; skipped when absent)
# ---------------------------------------------------------------------------
REAL_ROOT = Path(__file__).resolve().parent.parent
REAL_MASTER = next((REAL_ROOT / n for n in P.MASTER_CANDIDATES if (REAL_ROOT / n).is_file()),
                   None)
REAL_TEMPLATE = REAL_ROOT / P.TEMPLATE_NAME


@unittest.skipUnless(REAL_MASTER and REAL_TEMPLATE.is_file(), "real workspace files absent")
class RealWorkspaceTests(unittest.TestCase):
    """Guards on the user's real files; they are only read, never written."""

    def test_no_marked_skill_reaches_the_rendered_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            jd_path = Path(tmp) / "synthetic.md"
            jd_path.write_text(JD_PROCEED + "- Terraform, Kubernetes, ELK and HashiCorp Vault\n",
                               encoding="utf-8")
            master = P.parse_master(REAL_MASTER)
            analysis = P.analyze(P.read_job_description(jd_path, Path(tmp)), master)
            template = P.parse_template(REAL_TEMPLATE)
            plan = P.build_resume_plan(template, master, analysis)
            body = P.render_resume(plan, set(), 0).split("\\begin{document}", 1)[1]
            plain = P.latex_to_plain(body)
            for key in master.global_no:
                found = [m for m in P.SKILL_FINDER.find(plain) if m.key == key]
                self.assertFalse(found, f"[NO] skill {key} rendered")
            for ep in plan.entries.values():
                for cand in ep.selected:
                    self.assertTrue(P.verify_claim(cand.plain, master,
                                                   P.build_scope(master, ep.master_entry_id)).ok)
            sequence = P.trim_sequence(plan)
            values = []
            for unit, why in sequence:
                if why.startswith("optional entry with no"):
                    continue
                ep = plan.entries[(unit[1], unit[2])]
                values.append(max(c.relevance for c in ep.selected) if unit[0] == "entry"
                              else next(c.relevance for c in ep.selected
                                        if c.bullet_index == unit[3]))
            self.assertEqual(values, sorted(values))  # weakest content always goes first


# ---------------------------------------------------------------------------
# Real toolchain (runs only when pdflatex and pdftotext are installed)
# ---------------------------------------------------------------------------
HAVE_TEX = bool(shutil.which("pdflatex") and shutil.which("pdftotext"))


@unittest.skipUnless(HAVE_TEX, "pdflatex and pdftotext are required for the real PDF test")
class RealCompileTests(WorkspaceCase):
    def test_end_to_end_single_page_with_synthetic_template(self):
        lines: list[str] = []
        code = P.run([str(self.root / "target_jobs" / "proceed.md"), "--workspace",
                      str(self.root), "--output-dir", str(self.outputs)], out=lines.append)
        self.assertEqual(code, P.EXIT_OK, "\n".join(lines))
        pdf = self.outputs / "example-co_platform-engineer_resume.pdf"
        self.assertTrue(pdf.stat().st_size > 0)
        info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True)
        self.assertRegex(info.stdout, r"Pages:\s+1\b")
        text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True,
                              check=True).stdout
        self.assertIn("Test Person", text.replace("\n", " ").title())
        self.assertNotIn("Kubernetes", text)

    def test_real_compile_error_is_reported(self):
        tools = P.detect_tools(P.parse_template(self.root / "template.tex"))
        result = P.compile_tex("\\documentclass{article}\\begin{document}\\undefinedmacro"
                               "\\end{document}", self.outputs / ".b", "bad", tools, self.root)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
