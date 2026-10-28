"""Job index, uploaded profiles, scoring and the local app API (offline)."""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from fetch_helpers import load_fetch_jobs
from fixtures import MASTER, ROOT, load_pipeline, make_workspace

P = load_pipeline()
F = load_fetch_jobs()


def load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load("dashboard")
J = load("jobboard")
A = load("app")

FULL_HTML = ("<h2>Requirements</h2><ul><li>Must have Python and Docker</li>"
             "<li>Experience with Kubernetes and PostgreSQL</li></ul>"
             "<h2>Nice to have</h2><ul><li>FastAPI</li></ul>")
RESUME = """Jordan Okafor
Seattle, WA

EXPERIENCE
Platform Engineer, Harbor Freight Labs
- Ran Kubernetes clusters with Docker and Helm
- Wrote Python services backed by PostgreSQL

EDUCATION
BSc Computer Science, University of Washington

SKILLS
Python, Docker, Kubernetes, PostgreSQL, Linux
"""


def record(**changes):
    base = dict(source="Greenhouse", source_job_id="1", company="Example Co",
                title="Platform Engineer", location="Seattle, WA",
                url="https://job-boards.greenhouse.io/example/jobs/1",
                description=F.html_to_markdown(FULL_HTML), description_complete=True)
    base.update(changes)
    return F.JobRecord(**base)


class BoardCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name))
        target = self.root / "target_jobs"
        for path in target.glob("*.md"):
            path.unlink()
        self.write("greenhouse_exampleco_platform_engineer.md", record())
        self.write("greenhouse_exampleco_staff_engineer.md", record(
            title="Staff Software Engineer", url="https://job-boards.greenhouse.io/example/jobs/2"))
        self.write("greenhouse_thinco_vp_engineering.md", record(
            company="Thin Co", title="VP of Engineering", location="Toronto, ON",
            url="https://job-boards.greenhouse.io/thin/jobs/3",
            description="### Requirements\n\n- Python"))
        self.write("simplify_otherco_backend_intern.md", record(
            source="Simplify", company="Other Co", title="Backend Intern", location="Toronto, ON",
            url="https://other.example/jobs/4", description="", description_complete=False,
            completeness_note="Listing metadata only."))

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, rec):
        (self.root / "target_jobs" / name).write_text(F.render_markdown(rec, "2026-10-27"))

    def index(self):
        return J.load_or_build_index(self.root)

    def master_profile(self):
        return J.load_profile("master.md", MASTER.encode())


class IndexTests(BoardCase):
    def test_levels(self):
        cases = {
            "Software Engineer Intern": "Intern", "Product Manager Intern": "Intern",
            "Co-op Developer (Winter 2027)": "Intern",
            "New Grad Backend Engineer": "New grad", "Software Engineer, New Graduate": "New grad",
            "Early Career Data Analyst": "New grad", "University Graduate SRE": "New grad",
            "Junior DevOps Engineer": "Junior", "Jr. Platform Developer": "Junior",
            "Software Engineer I": "Junior", "SDE 1": "Junior", "Entry-Level Analyst": "Junior",
            "Software Engineer II": "Mid or unspecified", "Backend Engineer": "Mid or unspecified",
            "Seniority Programs Specialist": "Mid or unspecified",
            "Senior SRE": "Senior", "Sr. Backend Engineer": "Senior",
            "Staff Platform Engineer": "Staff and principal", "Principal SRE": "Staff and principal",
            "Tech Lead, Infrastructure": "Staff and principal",
            "Engineering Manager": "Manager", "Senior Manager, Platform": "Manager",
            "Director, Infrastructure": "Director and above", "VP of Engineering": "Director and above",
            "Head of Platform": "Director and above",
            "Principal Software Engineer I - Serverless": "Staff and principal",
            "Senior Software Engineer 1": "Senior", "Staff Engineer I": "Staff and principal"}
        for title, level in cases.items():
            self.assertEqual(J.level_of(title), level, title)
        self.assertEqual(set(J.LEVELS), {name for name, _p in J.LEVEL_RULES} | {J.MID_LEVEL})
        self.assertEqual(J.LEVELS[:3], ("Intern", "New grad", "Junior"))

    def test_cached_entries_follow_level_rules(self):
        self.index()
        with mock.patch.object(J, "level_of", return_value="Senior"):
            with mock.patch.object(J, "index_job", side_effect=AssertionError("re-indexed")):
                index = J.load_or_build_index(self.root)
        self.assertTrue(all(j["level"] == "Senior" for j in index["jobs"]))

    def test_index_contents(self):
        index = self.index()
        jobs = {j["file"]: j for j in index["jobs"]}
        self.assertEqual(len(jobs), 4)
        platform = jobs["greenhouse_exampleco_platform_engineer.md"]
        names = {r["name"]: r["category"] for r in platform["requirements"]}
        self.assertEqual(names["Python"], "mandatory")
        self.assertEqual(names["FastAPI"], "preferred")
        self.assertEqual((platform["region"], platform["level"]), ("US", "Mid or unspecified"))
        self.assertEqual(jobs["greenhouse_exampleco_staff_engineer.md"]["level"],
                         "Staff and principal")
        self.assertEqual(jobs["simplify_otherco_backend_intern.md"]["level"], "Intern")
        listing = jobs["simplify_otherco_backend_intern.md"]
        self.assertEqual((listing["complete"], listing["requirements"]), (False, []))
        self.assertTrue((self.root / "outputs" / J.INDEX_NAME).is_file())
        public = J.public_job(platform)
        self.assertNotIn("requirements", public)

    def test_incremental_reuse(self):
        first = self.index()
        with mock.patch.object(J, "index_job", side_effect=AssertionError("re-indexed")):
            second = J.load_or_build_index(self.root)  # unchanged files reuse entries
        self.assertEqual(first["jobs"], second["jobs"])
        self.write("greenhouse_exampleco_platform_engineer.md", record(title="Platform Engineer II"))
        third = J.load_or_build_index(self.root)
        titles = {j["title"] for j in third["jobs"]}
        self.assertIn("Platform Engineer II", titles)


class ProfileTests(BoardCase):
    def test_master_mode_keeps_strict_rules(self):
        profile = self.master_profile()
        self.assertEqual(profile.mode, "master")
        self.assertIn("Kubernetes", profile.no_skills)
        self.assertIn("Python", profile.skills)
        self.assertNotIn("Kubernetes", profile.skills)
        self.assertEqual(profile.warnings, [])

    def test_resume_mode(self):
        profile = J.load_profile("resume.txt", RESUME.encode())
        self.assertEqual(profile.mode, "resume")
        self.assertIn("Kubernetes", profile.skills)  # the person's own claim counts
        self.assertTrue(profile.warnings)
        converted = J.resume_to_master(RESUME)
        self.assertIn("## EXPERIENCE", converted)
        self.assertIn("## EDUCATION", converted)
        self.assertIn("- `[V]` Ran Kubernetes clusters with Docker and Helm", converted)

    def test_resume_cannot_inject_tags(self):
        converted = J.resume_to_master("Experience\n- [NO] [V] Python and Docker work\n")
        self.assertIn("- `[V]` Python and Docker work", converted)
        self.assertEqual(converted.count("[NO]"), 0)

    def test_upload_errors(self):
        for name, data, fragment in (
                ("cv.docx", b"PK\x03\x04", ".md, .txt or .pdf"),
                ("cv.pdf", b"not a pdf", "not a PDF"),
                ("cv.txt", b"too short", "too little readable text"),
                ("cv.txt", b"a\x00b" * 100, "not plain text"),
                ("cv.txt", b"x" * (J.MAX_UPLOAD_BYTES + 1), "larger than")):
            with self.assertRaises(J.ProfileError) as ctx:
                J.load_profile(name, data)
            self.assertIn(fragment, str(ctx.exception), name)

    def test_pdf_needs_pdftotext(self):
        with mock.patch.object(J.shutil, "which", return_value=None):
            with self.assertRaises(J.ProfileError) as ctx:
                J.pdf_to_text(b"%PDF-1.4")
        self.assertIn("pdftotext", str(ctx.exception))

    @unittest.skipUnless(shutil.which("pdflatex") and shutil.which("pdftotext"),
                         "pdflatex and pdftotext are needed to build a test PDF")
    def test_real_pdf_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "cv.tex"
            body = RESUME.replace("\n", "\n\n").replace("#", "")
            tex.write_text("\\documentclass{article}\\begin{document}\n" + body +
                           "\\end{document}\n")
            subprocess.run(["pdflatex", "-interaction=nonstopmode", "-output-directory", tmp,
                            str(tex)], capture_output=True, check=True, timeout=120)
            data = (Path(tmp) / "cv.pdf").read_bytes()
        profile = J.load_profile("cv.pdf", data)
        self.assertEqual(profile.mode, "resume")
        self.assertIn("Kubernetes", profile.skills)


class ScoringTests(BoardCase):
    def test_score_matches_pipeline_exactly(self):
        index = self.index()
        profile = self.master_profile()
        scores = {s["id"]: s for s in J.score_jobs(profile, index["jobs"])}
        master_path = self.root / "master.md"
        for job in index["jobs"]:
            if not job["requirements"]:
                self.assertNotIn(job["id"], scores)
                continue
            if not J.has_enough_detail(job):
                self.assertTrue(scores[job["id"]]["insufficient"])
                self.assertIsNone(scores[job["id"]]["score"])
                continue
            jd = P.read_job_description(self.root / "target_jobs" / job["file"], self.root)
            expected = P.analyze(jd, P.parse_master(master_path)).score
            self.assertEqual(scores[job["id"]]["score"], expected, job["file"])

    def test_minimum_evidence_rule(self):
        index = self.index()
        scores = {s["id"]: s for s in J.score_jobs(J.load_profile("r.txt", RESUME.encode()),
                                                   index["jobs"])}
        thin = scores["greenhouse_thinco_vp_engineering"]
        self.assertTrue(thin["insufficient"])
        self.assertEqual((thin["score"], thin["rank"], thin["verdict"]), (None, None, None))
        self.assertEqual(thin["recognized"], ["Python"])
        full = scores["greenhouse_exampleco_platform_engineer"]
        self.assertFalse(full["insufficient"])
        self.assertGreaterEqual(full["skill_requirements"], J.MIN_SKILL_REQUIREMENTS)
        ordered = J.score_jobs(J.load_profile("r.txt", RESUME.encode()), index["jobs"])
        flags = [s["insufficient"] for s in ordered]
        self.assertEqual(flags, sorted(flags))  # every ranked job comes before held-back ones
        public = J.public_job(next(j for j in index["jobs"]
                                   if j["id"] == "greenhouse_thinco_vp_engineering"))
        self.assertEqual((public["enough_detail"], public["skill_requirements"]), (False, 1))

    def test_no_marker_and_resume_claims(self):
        index = self.index()
        job_id = "greenhouse_exampleco_platform_engineer"
        strict = {s["id"]: s for s in J.score_jobs(self.master_profile(), index["jobs"])}[job_id]
        self.assertIn(["Kubernetes", "explicitly_no"], strict["gaps"])
        claimed = {s["id"]: s for s in J.score_jobs(J.load_profile("r.txt", RESUME.encode()),
                                                    index["jobs"])}[job_id]
        self.assertIn("Kubernetes", claimed["matched"])

    def test_either_or_groups_in_index_and_scores(self):
        self.write("greenhouse_groupco_backend_engineer.md", record(
            company="Group Co", title="Backend Engineer", url="https://job-boards.greenhouse.io/g/jobs/7",
            description="### Requirements\n\n- We work mostly in Java, Rust and Python\n"
                        "- Docker and PostgreSQL\n- Linux"))
        index = self.index()
        job = next(j for j in index["jobs"] if j["id"] == "greenhouse_groupco_backend_engineer")
        group = next(r for r in job["requirements"] if r["kind"] == "skill_group")
        self.assertEqual(group["members"], ["java", "rust", "python"])
        self.assertTrue(J.has_enough_detail(job))
        score = next(s for s in J.score_jobs(J.load_profile("r.txt", RESUME.encode()),
                                             index["jobs"]) if s["id"] == job["id"])
        self.assertIn("One of: Java, Rust, Python", score["matched"])

    def test_ranking_discounts_few_requirements(self):
        with mock.patch.object(J, "MIN_SKILL_REQUIREMENTS", 1):
            index = self.index()
            scores = J.score_jobs(J.load_profile("r.txt", RESUME.encode()), index["jobs"])
        thin = next(s for s in scores if s["id"] == "greenhouse_thinco_vp_engineering")
        full = next(s for s in scores if s["id"] == "greenhouse_exampleco_platform_engineer")
        self.assertEqual((thin["score"], thin["requirements"]), (100, 1))
        self.assertGreater(full["rank"], thin["rank"])
        self.assertEqual(scores[0]["id"], full["id"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AppApiTests(BoardCase):
    def setUp(self):
        super().setUp()
        self.server = A.serve(self.root, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def upload(self, name, content):
        return self.request("/api/score", {"filename": name,
                                           "content_base64": base64.b64encode(content).decode()})

    def test_root_redirects_to_studio_and_security_headers(self):
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(self.base + "/", timeout=30)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 302)
            self.assertEqual(exc.headers["Location"], A.STUDIO_URL)
            self.assertIn("default-src 'self'", exc.headers["Content-Security-Policy"])
        for path in ("/master.md", "/../master.md", "/web/app.js", "/app.js", "/api/nothing"):
            self.assertEqual(self.request(path)[0], 404, path)

    def test_jobs_and_meta(self):
        status, _h, body = self.request("/api/jobs")
        jobs = json.loads(body)
        self.assertEqual((status, len(jobs)), (200, 4))
        self.assertNotIn("requirements", jobs[0])
        meta = json.loads(self.request("/api/meta")[2])
        self.assertEqual((meta["jobs"], meta["scorable"], meta["threshold"]), (4, 2, 65))
        self.assertEqual(meta["not_enough_detail"], 1)
        self.assertEqual(meta["levels"], list(J.LEVELS))

    def test_score_upload(self):
        status, _h, body = self.upload("master.md", MASTER.encode())
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["profile"]["mode"], "master")
        self.assertEqual(len(data["scores"]), 3)
        self.assertEqual(sum(1 for s in data["scores"] if s["insufficient"]), 1)
        status, _h, body = self.upload("resume.txt", RESUME.encode())
        self.assertEqual((status, json.loads(body)["profile"]["mode"]), (200, "resume"))

    def test_upload_is_never_written_to_disk(self):
        before = {p for p in self.root.rglob("*")}
        self.upload("resume.txt", RESUME.encode())
        self.assertEqual({p for p in self.root.rglob("*")}, before)

    def test_bad_requests(self):
        self.assertEqual(self.upload("cv.docx", b"PK")[0], 422)
        self.assertEqual(self.request("/api/score", raw=b"{nope")[0], 400)
        self.assertEqual(self.request("/api/score", {"filename": "a.txt",
                                                     "content_base64": "%%%"})[0], 400)
        big = b'{"filename":"a.txt","content_base64":"' + b"A" * (A.MAX_BODY_BYTES + 10) + b'"}'
        self.assertEqual(self.request("/api/score", raw=big)[0], 413)

    def test_reindex_picks_up_new_jobs(self):
        self.write("greenhouse_newco_sre.md", record(company="New Co", title="SRE",
                                                     url="https://job-boards.greenhouse.io/n/jobs/9"))
        status, _h, body = self.request("/api/reindex", raw=b"{}")
        self.assertEqual((status, json.loads(body)["jobs"]), (200, 5))


if __name__ == "__main__":
    unittest.main()
