"""evaluate.py: automatic checks, labelling sheet and accuracy report (offline)."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_jobboard import BoardCase, J, RESUME, load

E = load("evaluate")


class CheckTests(BoardCase):
    def test_all_checks_pass_on_a_working_scorer(self):
        results = E.run_checks(self.root, RESUME, "resume.txt")
        failures = [(r.name, r.detail) for r in results if r.status == "FAIL"]
        self.assertEqual(failures, [])
        names = " ".join(r.name for r in results)
        for expected in ("scored twice", "Unrelated line", "Reordering", "Removing",
                         "different resume", "get no score", "pipeline.py"):
            self.assertIn(expected, names)

    def test_checks_catch_a_scorer_that_ignores_the_resume(self):
        real = J.score_jobs
        frozen = real(J.load_profile("r.txt", RESUME.encode()), self.index()["jobs"])
        with mock.patch.object(J, "score_jobs", lambda profile, jobs: [dict(s) for s in frozen]):
            results = E.run_checks(self.root, RESUME, "resume.txt")
        failed = {r.name for r in results if r.status == "FAIL"}
        self.assertTrue(any(name.startswith("Removing") for name in failed), failed)

    def test_contrast_check_runs_on_a_large_board(self):
        with mock.patch.object(E, "CONTRAST_MIN_RANKED", 1):
            results = {r.name: r for r in E.run_checks(self.root, RESUME, "resume.txt")}
        self.assertIn(results["A very different resume ranks jobs differently"].status,
                      ("PASS", "FAIL"))

    def test_remove_skill_only_deletes_that_skill(self):
        self.assertEqual(E.remove_skill("Python, Docker and Kubernetes", "docker"),
                         "Python,  and Kubernetes")
        self.assertEqual(E.remove_skill("Ran python-gitlab tools", "python"),
                         "Ran -gitlab tools")

    def test_spearman(self):
        self.assertAlmostEqual(E.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(E.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertAlmostEqual(E.spearman([1, 2, 2, 3], [1, 2, 3, 4]), 0.9486832980505138)
        self.assertIsNone(E.spearman([1, 2], [2, 1]))
        self.assertIsNone(E.spearman([5, 5, 5], [1, 2, 3]))


class SheetAndReportTests(BoardCase):
    def make(self):
        resume = self.root / "my resume.txt"
        resume.write_text(RESUME, encoding="utf-8")
        return E.make_sheet(self.root, resume, jobs_wanted=10, seed=3)

    def test_sheet_contents_and_privacy(self):
        folder = self.make()
        for name in ("jobs.csv", "requirements.csv", "meta.json", "README.md"):
            self.assertTrue((folder / name).is_file(), name)
        for path in folder.iterdir():
            text = path.read_text(encoding="utf-8-sig")
            self.assertNotIn("Harbor Freight Labs", text)   # resume text never stored
        jobs = list(csv.DictReader((folder / "jobs.csv").open(encoding="utf-8-sig")))
        self.assertTrue(jobs)
        self.assertEqual({r["your_fit_1_to_5"] for r in jobs}, {""})
        thin = [r for r in jobs if r["engine_status"] == "not enough detail"]
        self.assertTrue(all(r["engine_score"] == "" for r in thin))
        again = self.make()
        self.assertNotEqual(folder, again)  # never overwrites an existing sheet

    def test_report_from_labels(self):
        folder = self.make()
        jobs_path, reqs_path = folder / "jobs.csv", folder / "requirements.csv"
        jobs = list(csv.DictReader(jobs_path.open(encoding="utf-8-sig")))
        reqs = list(csv.DictReader(reqs_path.open(encoding="utf-8-sig")))
        for i, row in enumerate(reqs):
            row["in_posting_yes_no"] = "no" if i == 0 else "yes"
            if row["engine_call"] != "not scored" and i != 0:
                row["engine_call_correct_yes_no"] = "no" if i == 1 else "yes"
        for row in jobs:
            score = float(row["engine_score"] or 0)
            row["your_fit_1_to_5"] = str(1 + min(4, int(score // 25)))
        jobs[0]["missed_requirements"] = "On-call rotation; Terraform"
        for path, rows in ((jobs_path, jobs), (reqs_path, reqs)):
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        result = E.build_report(folder)
        yes, total = result["extraction_precision"]
        self.assertEqual((yes, total), (len(reqs) - 1, len(reqs)))
        self.assertEqual(result["extraction_recall"], (len(reqs) - 1, len(reqs) + 1))
        correct, calls = result["matching_accuracy"]
        self.assertEqual(calls - correct, 1 if reqs[1]["engine_call"] != "not scored" else 0)
        if result["rated_scored_jobs"] >= 3:
            self.assertGreater(result["spearman"], 0.5)
        lines: list[str] = []
        self.assertEqual(E.main(["report", str(folder), "--workspace", str(self.root)],
                                out=lines.append), 0)
        self.assertTrue((folder / "report.md").is_file())
        self.assertIn("Requirement extraction", lines[0])

    def test_sheet_for_chosen_jobs(self):
        resume = self.root / "cv.txt"
        resume.write_text(RESUME, encoding="utf-8")
        wanted = ["greenhouse_thinco_vp_engineering", "greenhouse_exampleco_platform_engineer"]
        folder = E.make_sheet(self.root, resume, job_ids=wanted, name="My Picks")
        self.assertTrue(folder.name.endswith("-cv-my-picks"))
        rows = list(csv.DictReader((folder / "jobs.csv").open(encoding="utf-8-sig")))
        self.assertEqual([r["job_id"] for r in rows], wanted)
        self.assertEqual(rows[0]["sample_band"], "not enough detail")
        with self.assertRaises(ValueError):
            E.make_sheet(self.root, resume, job_ids=["no_such_job"])

    def test_carry_labels_to_a_fresh_sheet(self):
        first = self.make()
        rows = list(csv.DictReader((first / "jobs.csv").open(encoding="utf-8-sig")))
        rows[0]["your_fit_1_to_5"], rows[0]["notes"] = "5", "great fit"
        with (first / "jobs.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        resume = self.root / "my resume.txt"
        second = E.make_sheet(self.root, resume, carry_from=first, name="rescored")
        again = list(csv.DictReader((second / "jobs.csv").open(encoding="utf-8-sig")))
        self.assertEqual([r["job_id"] for r in again], [r["job_id"] for r in rows])
        self.assertEqual((again[0]["your_fit_1_to_5"], again[0]["notes"]), ("5", "great fit"))

    def test_blank_sheet_reports_without_crashing(self):
        result = E.build_report(self.make())
        self.assertEqual(result["extraction_precision"], (0, 0))
        self.assertIn("No labels found yet", E.render_report(result))


if __name__ == "__main__":
    unittest.main()
