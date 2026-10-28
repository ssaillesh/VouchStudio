"""Provenance engine, proof-of-build gates, gap analysis and interview briefing."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from fixtures import JD_PROCEED, MASTER, FakeTex, load_pipeline, make_workspace
from test_pipeline import WorkspaceCase, bullets_in

P = load_pipeline()
ROOT = Path(__file__).resolve().parent.parent
BASE = "example-co_platform-engineer"
REJECT_BASE = "other-co_data-scientist"

JD_GAPS = """# Platform Engineer at Gap Co

## Requirements
- Must have Jenkins, Helm and Ansible
- Experience with Apache Kafka and CircleCI required
- Kubernetes and Rust required
- Python required
"""


class FakeTexCorruptText(FakeTex):
    """Valid one-page PDFs whose text layer loses the percent sign (as an ATS might)."""

    def __call__(self, command, **kwargs):
        result = super().__call__(command, **kwargs)
        if Path(command[0]).name == "pdftotext":
            result = subprocess.CompletedProcess(command, 0, result.stdout.replace("%", ""), "")
        return result


class FakeTexFatalLog(FakeTex):
    """pdflatex exits 0 but its log records a fatal error."""

    def __call__(self, command, **kwargs):
        result = super().__call__(command, **kwargs)
        if Path(command[0]).name == "pdflatex":
            outdir = Path(next(a.split("=", 1)[1] for a in command
                               if a.startswith("-output-directory=")))
            job = next(a.split("=", 1)[1] for a in command if a.startswith("-jobname="))
            with (outdir / f"{job}.log").open("a", encoding="utf-8") as handle:
                handle.write("! LaTeX Error: Something's wrong--perhaps a missing \\item.\n")
        return result


class PlanCase(WorkspaceCase):
    def plan(self, jd_text: str = JD_PROCEED):
        analysis = self.analysis(jd_text)
        template = P.parse_template(self.root / "template.tex")
        return analysis, P.build_resume_plan(template, analysis.master, analysis)

    def report(self, base: str = BASE) -> dict:
        return json.loads((self.outputs / f"{base}_analysis.json").read_text())


# ---------------------------------------------------------------------------
# 1. Provenance and evidence engine
# ---------------------------------------------------------------------------
class ProvenanceEngineTests(PlanCase):
    def test_every_rendered_claim_cites_positive_lines_of_its_entry(self):
        analysis, plan = self.plan()
        proof = P.build_provenance(plan, analysis.master, set())
        self.assertEqual(proof["summary"]["claims"], proof["summary"]["traced"])
        types = proof["summary"]["by_type"]
        for kind in ("summary_sentence", "skills_item", "entry_header", "bullet", "stack_item"):
            self.assertGreater(types.get(kind, 0), 0, kind)
        master = analysis.master
        for claim in proof["claims"]:
            self.assertTrue(claim["traced"])
            self.assertTrue(claim["evidence"], claim["text"])
            if claim["type"] != "bullet":
                continue
            entry_id = claim["master_entry"]["id"]
            for item in claim["evidence"]:
                line = master.line(item["line"])
                self.assertIn(line.tag, P.POSITIVE_TAGS)
                self.assertTrue(line.entry_id == entry_id or entry_id in line.refs)
                self.assertIn(item["tag"], ("[V]", "[R]", "skills table"))

    def test_metrics_point_at_the_line_that_states_them(self):
        analysis, plan = self.plan()
        proof = P.build_provenance(plan, analysis.master, set())
        bullet = next(c for c in proof["claims"] if c["text"].startswith("Cut deploy time 30%"))
        lines = {m["value"]: m["line"] for m in bullet["metrics"]}
        self.assertEqual(set(lines), {"30", "50"})
        for value, number in lines.items():
            self.assertIn(value, analysis.master.line(number).raw)

    def test_repo_links_come_from_the_master_entry(self):
        master = MASTER.replace("### 3.1 Gamma App\n",
                                "### 3.1 Gamma App\nRepo github.com/test/gamma-app.\n")
        make_workspace(self.root, master=master)
        analysis, plan = self.plan()
        proof = P.build_provenance(plan, analysis.master, set())
        header = next(c for c in proof["claims"]
                      if c["type"] == "entry_header" and c["entry"].startswith("Gamma App"))
        self.assertEqual(header["master_entry"]["links"], ["github.com/test/gamma-app"])

    def test_untraceable_bullet_raises(self):
        analysis, plan = self.plan()
        cand = plan.entries[(2, 0)].selected[0]
        cand.plain = "Operated Kubernetes clusters for 900 services"
        with self.assertRaises(P.ProvenanceValidationError) as caught:
            P.build_provenance(plan, analysis.master, set())
        self.assertIn("Kubernetes", str(caught.exception))
        self.assertEqual(caught.exception.exit_code, P.EXIT_FAILED)

    def test_untraceable_skills_item_raises(self):
        analysis, plan = self.plan()
        plan.kv_items[(1, 0)].append("Kubernetes")
        with self.assertRaises(P.ProvenanceValidationError):
            P.build_provenance(plan, analysis.master, set())

    def test_hard_gate_fails_the_build_before_anything_is_written(self):
        real_plan = P.build_resume_plan

        def tampered(template, master, analysis):
            plan = real_plan(template, master, analysis)
            plan.entries[(2, 0)].selected[0].plain = "Cut deploy time 95% for 50+ services"
            return plan

        with mock.patch.object(P, "build_resume_plan", tampered):
            code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_FAILED)
        self.assertIn("cannot be traced", lines[0])
        written = [p.name for p in self.outputs.iterdir()] if self.outputs.exists() else []
        self.assertFalse([n for n in written if "_resume" in n], written)

    def test_debug_proof_writes_the_map_of_the_rendered_resume(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   "--debug-proof")
        self.assertEqual(code, P.EXIT_OK)
        proof = json.loads((self.outputs / f"{BASE}_provenance.json").read_text())
        tex = (self.outputs / f"{BASE}_resume.tex").read_text()
        bullets = [c for c in proof["claims"] if c["type"] == "bullet"]
        self.assertEqual(len(bullets), bullets_in(tex))
        for claim in bullets:
            self.assertIn(claim["latex"], tex)
        report = self.report()
        self.assertEqual(report["resume"]["provenance"]["file"], f"{BASE}_provenance.json")
        self.assertEqual(report["resume"]["provenance"]["scope"], "rendered resume")
        self.assertTrue(any(line.startswith("Provenance:") for line in lines))

    def test_no_provenance_file_without_the_flag(self):
        self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertFalse((self.outputs / f"{BASE}_provenance.json").exists())
        summary = self.report()["resume"]["provenance"]
        self.assertEqual(summary["claims"], summary["traced"])
        self.assertIsNone(summary["file"])

    def test_match_sh_passes_the_new_flags_through(self):
        result = subprocess.run(
            ["bash", str(ROOT / "match.sh"), str(self.root / "target_jobs" / "proceed.md"),
             "--dry-run", "--debug-proof", "--analyze-gap", "--workspace", str(self.root)],
            capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, P.EXIT_OK, result.stdout + result.stderr)
        self.assertIn("candidate claims traced", result.stdout)
        self.assertIn("gap:", result.stdout)
        self.assertFalse((self.root / "outputs").exists())  # dry run writes nothing


# ---------------------------------------------------------------------------
# 2. Proof-of-build gates
# ---------------------------------------------------------------------------
class BuildGateTests(PlanCase):
    def gates(self, base: str = BASE) -> dict:
        return self.report(base)["resume"]["build_gates"]

    def test_all_three_gates_recorded_on_success(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_OK)
        gates = self.gates()
        self.assertEqual([g["status"] for g in gates.values()], ["passed"] * 3)
        self.assertEqual(gates["gate_1_syntax"]["returncode"], 0)
        self.assertEqual(gates["gate_2_single_page"]["form_feed_pages"], 1)
        ats = gates["gate_3_ats_readability"]
        self.assertEqual(ats["section_order"],
                         ["Summary", "Technical Skills", "Experience", "Projects", "Education"])
        self.assertEqual(ats["core_sections"], {"education": True, "experience": True,
                                                "skills": True, "projects": True})
        self.assertGreater(ats["terms_checked"], 0)
        self.assertGreater(ats["metrics_checked"], 0)
        self.assertIn("Build gates: syntax passed, single page passed, ats readability passed",
                      lines)

    def test_gate_1_fails_on_a_fatal_log_line_even_with_exit_0(self):
        code, _ = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                               runner=FakeTexFatalLog())
        self.assertEqual(code, P.EXIT_FAILED)
        gates = self.gates()
        self.assertEqual(gates["gate_1_syntax"]["status"], "failed")
        self.assertIn("LaTeX Error", gates["gate_1_syntax"]["fatal_errors"][0])
        self.assertEqual(gates["gate_2_single_page"]["status"], "not_run")
        self.assertEqual(gates["gate_3_ats_readability"]["status"], "not_run")
        self.assertFalse((self.outputs / f"{BASE}_resume.pdf").exists())

    def test_gate_2_trims_until_one_page_and_reports_it(self):
        _analysis, plan = self.plan()
        target = bullets_in(P.render_resume(plan, set(), 0)) - 1
        runner = FakeTex(lambda tex: 1 if bullets_in(tex) <= target else 2)
        code, _ = self.run_cli(str(self.root / "target_jobs" / "proceed.md"), runner=runner)
        self.assertEqual(code, P.EXIT_OK)
        gate = self.gates()["gate_2_single_page"]
        self.assertEqual(gate["status"], "passed")
        self.assertGreater(gate["units_trimmed"], 0)
        self.assertGreater(gate["compile_iterations"], 1)

    def test_gate_3_failure_blocks_publishing(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                                   runner=FakeTexCorruptText())
        self.assertEqual(code, P.EXIT_FAILED)
        resume = self.report()["resume"]
        self.assertEqual(resume["status"], "ats_check_failed")
        ats = resume["build_gates"]["gate_3_ats_readability"]
        self.assertEqual(ats["status"], "failed")
        self.assertIn("30%", ats["missing_metrics"])
        self.assertFalse((self.outputs / f"{BASE}_resume.pdf").exists())
        self.assertFalse((self.outputs / f"{BASE}_briefing.json").exists())
        self.assertFalse(resume["single_page_verified"])

    def test_ats_text_checks(self):
        titles = ["Experience", "Technical Skills", "Projects", "Education"]
        good = ("Experience\nAcme\n- Ran Kubernetes for 30% of 50+ services\n"
                "Technical Skills\nPython, GitLab CI/CD\nProjects\nGamma\nEducation\nBSc\n")
        terms = ["Kubernetes", "GitLab CI/CD", "Python"]
        ok = P.check_ats_text(good, titles, terms, ["30%", "50+"])
        self.assertTrue(ok.ok, ok.reasons)
        swapped = good.replace("Projects\nGamma\nEducation\nBSc", "Education\nBSc\nProjects\nGamma")
        self.assertEqual(P.check_ats_text(swapped, titles, terms, []).out_of_order_sections,
                         ["Education"])
        broken = good.replace("Kubernetes", "Kuber-\nnetes").replace("GitLab CI/CD", "GitLab CI /CD")
        self.assertEqual(P.check_ats_text(broken, titles, terms, []).fragmented_terms,
                         ["Kubernetes", "GitLab CI/CD"])
        residue = P.check_ats_text(good + "\\textbf{Go} \\&", titles, terms, [])
        self.assertEqual(residue.latex_artifacts, ["\\&", "\\textbf"])
        lost = P.check_ats_text(good.replace("30%", "30"), titles, terms, ["30%"])
        self.assertEqual(lost.missing_metrics, ["30%"])
        missing = P.check_ats_text(good.replace("Projects\n", ""), titles, terms, [])
        self.assertEqual(missing.missing_sections, ["Projects"])
        ligature = P.check_ats_text(good.replace("Python", "Py\ufb01thon"), titles,
                                    ["Pyfithon"], [])
        self.assertTrue(ligature.ok)  # NFKC folds ligatures before comparing

    def test_compile_tex_rejects_fatal_log_with_zero_exit(self):
        template = P.parse_template(self.root / "template.tex")
        tools = P.detect_tools(template, lambda n: f"/fake/bin/{n}")
        self.outputs.mkdir()
        result = P.compile_tex("\\begin{document}x\\end{document}", self.outputs / ".b", "job",
                               tools, self.root, FakeTexFatalLog())
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertIn("fatal error", result.error)


# ---------------------------------------------------------------------------
# 3. Reverse gap analysis
# ---------------------------------------------------------------------------
class GapAnalysisTests(PlanCase):
    def gap(self, jd_text: str = JD_GAPS) -> dict:
        return P.build_gap_analysis(self.analysis(jd_text), "below_threshold")

    def test_low_fit_writes_gap_analysis(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "reject.md"))
        self.assertEqual(code, P.EXIT_LOW_FIT)
        gap = json.loads((self.outputs / f"{REJECT_BASE}_gap_analysis.json").read_text())
        self.assertEqual(list(gap)[:3], ["company", "role", "source_job_description"])
        self.assertIsInstance(gap["match_score"], float)
        self.assertEqual(gap["trigger"], "below_threshold")
        self.assertLess(gap["match_score"], P.FIT_THRESHOLD)
        names = {m["skill"]: m for m in gap["missing_critical_skills"]}
        self.assertEqual(names["Kubernetes"]["master_status"], "NO")
        self.assertFalse(names["Kubernetes"]["claimable"])
        self.assertEqual(names["Rust"]["master_status"], "MISSING")
        self.assertTrue(gap["actionable_recommendations"])
        self.assertTrue(any(line.startswith("Gap analysis:") for line in lines))

    def test_master_statuses(self):
        rows = {m["skill"]: m["master_status"] for m in self.gap()["missing_critical_skills"]}
        self.assertEqual(rows, {"Ansible": "OLD", "Helm": "EXPOSURE_ONLY", "Jenkins": "OLD",
                                "Apache Kafka": "NEGATED", "CircleCI": "MISSING",
                                "Kubernetes": "NO", "Rust": "MISSING"})

    def test_recommendations_never_suggest_claiming_no_or_negated_skills(self):
        recs = {r["requirement"]: r for r in self.gap()["actionable_recommendations"]}
        self.assertEqual(recs["Kubernetes"]["action"], "do_not_claim")
        self.assertIsNone(recs["Kubernetes"]["target_entry"])
        self.assertIsNone(recs["Kubernetes"]["score_if_evidenced"])
        self.assertEqual(recs["Apache Kafka"]["action"], "acknowledge_gap")
        unlock = recs[None]
        self.assertNotIn("Kubernetes", unlock["recommendation"])
        self.assertNotIn("Kafka", unlock["recommendation"])

    def test_missing_skill_points_at_the_closest_entry(self):
        recs = {r["requirement"]: r for r in self.gap()["actionable_recommendations"]}
        self.assertEqual(recs["CircleCI"]["action"], "add_evidence_if_true")
        self.assertEqual(recs["CircleCI"]["target_entry"], "Acme Corp")
        self.assertIn("If you have real CircleCI experience", recs["CircleCI"]["recommendation"])
        self.assertEqual(recs["Rust"]["target_entry"], "Gamma App")
        self.assertEqual(recs["Helm"]["target_entry"], "Acme Corp")
        self.assertEqual(recs["Jenkins"]["action"], "confirm_existing_mention")

    def test_unlock_plan_matches_a_rescored_profile(self):
        analysis = self.analysis(JD_GAPS)
        gap = P.build_gap_analysis(analysis, "below_threshold")
        plan = gap["unlock_plan"]
        self.assertTrue(plan["reachable"])
        for req in analysis.requirements:
            if req.name in plan["requirements"]:
                req.status = P.STATUS_MATCHED
        score, _ = P.compute_score(analysis.requirements)
        self.assertEqual(score, plan["projected_score"])
        self.assertGreaterEqual(score, P.FIT_THRESHOLD)
        self.assertEqual(gap["actionable_recommendations"][0]["action"], "unlock_plan")

    def test_score_if_evidenced_is_exact(self):
        analysis = self.analysis(JD_GAPS)
        gap = P.build_gap_analysis(analysis, "below_threshold")
        row = next(m for m in gap["missing_critical_skills"] if m["skill"] == "Rust")
        req = self.req(analysis, "Rust")
        req.status = P.STATUS_MATCHED
        self.assertEqual(P.compute_score(analysis.requirements)[0], row["score_if_evidenced"])

    def test_analyze_gap_flag_on_proceed(self):
        self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertFalse((self.outputs / f"{BASE}_gap_analysis.json").exists())
        code, _ = self.run_cli(str(self.root / "target_jobs" / "proceed.md"), "--analyze-gap")
        self.assertEqual(code, P.EXIT_OK)
        gap = json.loads((self.outputs / f"{BASE}_gap_analysis.json").read_text())
        self.assertEqual(gap["trigger"], "--analyze-gap")
        self.assertEqual(gap["verdict"], P.VERDICT_PROCEED)
        self.assertEqual(gap["unlock_plan"]["requirements"], [])

    def test_dry_run_prints_but_writes_nothing(self):
        code, lines = self.run_cli(str(self.root / "target_jobs" / "reject.md"), "--dry-run")
        self.assertEqual(code, P.EXIT_LOW_FIT)
        self.assertTrue(any(line.startswith("  gap:") for line in lines))
        self.assertFalse(self.outputs.exists())

    def test_gap_analysis_is_deterministic(self):
        first = json.dumps(self.gap())
        self.assertEqual(first, json.dumps(self.gap()))


# ---------------------------------------------------------------------------
# 4. Interview briefing
# ---------------------------------------------------------------------------
class BriefingTests(PlanCase):
    def build(self) -> tuple[dict, str, str]:
        code, lines = self.run_cli(str(self.root / "target_jobs" / "proceed.md"))
        self.assertEqual(code, P.EXIT_OK, lines)
        briefing = json.loads((self.outputs / f"{BASE}_briefing.json").read_text())
        markdown = (self.outputs / f"{BASE}_briefing.md").read_text()
        tex = (self.outputs / f"{BASE}_resume.tex").read_text()
        return briefing, markdown, tex

    def test_briefing_written_with_every_required_part(self):
        briefing, markdown, _tex = self.build()
        for key in ("technical_context", "highlighted_metrics", "star_prompts"):
            self.assertTrue(briefing[key], key)
        for heading in ("## Technical context", "## Highlighted metrics in this resume",
                        "## STAR talking points"):
            self.assertIn(heading, markdown)
        files = self.report()["resume"]["files"]
        self.assertEqual((files["briefing_json"], files["briefing_md"]),
                         (f"{BASE}_briefing.json", f"{BASE}_briefing.md"))

    def test_technical_context_is_verbatim_jd_text(self):
        briefing, _md, _tex = self.build()
        jd_lines = JD_PROCEED.split("\n")
        for item in briefing["technical_context"]:
            self.assertIn(item["text"], jd_lines[item["jd_line"] - 1])
            self.assertTrue(item["requirements"])

    def test_metrics_are_exactly_those_in_this_build(self):
        briefing, _md, tex = self.build()
        plain = P.latex_to_plain(tex)
        values = [m for item in briefing["highlighted_metrics"] for m in item["metrics"]]
        self.assertIn("30%", values)
        for item in briefing["highlighted_metrics"]:
            self.assertIn(item["bullet"], plain)
            for value in item["metrics"]:
                self.assertIn(value, item["bullet"])
        self.assertNotIn("45%", values)  # rejected template bullet never reaches the briefing

    def test_star_prompts_link_resume_bullets_to_requirements(self):
        briefing, _md, tex = self.build()
        plain = P.latex_to_plain(tex)
        prompts = briefing["star_prompts"]
        self.assertEqual(len(prompts), 3)
        self.assertEqual(len({p["action"] for p in prompts}), 3)
        analysis = self.analysis(JD_PROCEED)
        matched = {r.name for r in analysis.requirements if r.status == P.STATUS_MATCHED}
        for item in prompts:
            self.assertIn(item["action"], plain)
            self.assertTrue(item["evidence_lines"])
            if item["requirement"]:
                self.assertIn(item["requirement"], matched)
                self.assertIn(item["requirement"], item["task"])

    def test_no_briefing_when_the_build_fails(self):
        code, _ = self.run_cli(str(self.root / "target_jobs" / "proceed.md"),
                               runner=FakeTex(lambda tex: 2))
        self.assertEqual(code, P.EXIT_PAGE_LIMIT)
        self.assertFalse((self.outputs / f"{BASE}_briefing.json").exists())
        self.assertFalse((self.outputs / f"{BASE}_briefing.md").exists())

    def test_briefing_is_deterministic(self):
        first, first_md, _ = self.build()
        second, second_md, _ = self.build()
        self.assertEqual((first, first_md), (second, second_md))


# ---------------------------------------------------------------------------
# Real toolchain (runs only when pdflatex and pdftotext are installed)
# ---------------------------------------------------------------------------
HAVE_TEX = bool(shutil.which("pdflatex") and shutil.which("pdftotext"))


@unittest.skipUnless(HAVE_TEX, "pdflatex and pdftotext are required for the real PDF test")
class RealProofOfBuildTests(PlanCase):
    def test_real_pdf_passes_all_gates_and_ships_companions(self):
        lines: list[str] = []
        code = P.run([str(self.root / "target_jobs" / "proceed.md"), "--workspace",
                      str(self.root), "--output-dir", str(self.outputs), "--debug-proof"],
                     out=lines.append)
        self.assertEqual(code, P.EXIT_OK, "\n".join(lines))
        gates = self.report()["resume"]["build_gates"]
        self.assertEqual([g["status"] for g in gates.values()], ["passed"] * 3)
        self.assertEqual(gates["gate_3_ats_readability"]["fragmented_terms"], [])
        for suffix in ("provenance.json", "briefing.json", "briefing.md"):
            self.assertTrue((self.outputs / f"{BASE}_{suffix}").is_file(), suffix)
        text = subprocess.run(["pdftotext", str(self.outputs / f"{BASE}_resume.pdf"), "-"],
                              capture_output=True, text=True, check=True).stdout
        self.assertEqual(text.count("\f"), 1)  # the documented grep -c $'\f' check

    def test_real_ats_check_flags_a_misordered_pdf(self):
        _analysis, plan = self.plan()
        template = plan.template
        tools = P.detect_tools(template)
        self.outputs.mkdir()
        build = self.outputs / ".b"
        tex = P.render_resume(plan, set(), 0)
        projects = tex.index("\\section{Projects}")
        education = tex.index("\\section{Education}")
        end = tex.index("\\end{document}")
        swapped = tex[:projects] + tex[education:end] + tex[projects:education] + tex[end:]
        result = P.compile_tex(swapped, build, "swapped", tools, self.root)
        self.assertTrue(result.ok, result.error)
        ats = P.ats_readability_check(result.pdf_path, tools, plan, set())
        self.assertFalse(ats.ok)
        self.assertEqual(ats.out_of_order_sections, ["Education"])


if __name__ == "__main__":
    unittest.main()
