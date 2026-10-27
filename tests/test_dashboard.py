"""Static dashboard: data collection, escaping and links (offline, synthetic data)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from fetch_helpers import load_fetch_jobs
from fixtures import ROOT, load_pipeline, make_workspace

P = load_pipeline()
F = load_fetch_jobs()


def load_dashboard():
    if "dashboard" in sys.modules:
        return sys.modules["dashboard"]
    spec = importlib.util.spec_from_file_location("dashboard", ROOT / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dashboard"] = module
    spec.loader.exec_module(module)
    return module


D = load_dashboard()
FULL = """<h2>Requirements</h2><ul><li>Must have Python and Docker</li>
<li>Experience with FastAPI and PostgreSQL</li></ul>"""


def record(**changes):
    base = dict(source="Greenhouse", source_job_id="1", company="Example Co",
                title="Platform Engineer", location="Seattle, WA",
                url="https://job-boards.greenhouse.io/example/jobs/1",
                description=F.html_to_markdown(FULL), description_complete=True)
    base.update(changes)
    return F.JobRecord(**base)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name))
        target = self.root / "target_jobs"
        for path in target.glob("*.md"):
            path.unlink()
        write = lambda name, rec: (target / name).write_text(F.render_markdown(rec, "2026-10-27"))
        write("greenhouse_exampleco_platform_engineer.md", record())
        write("simplify_otherco_backend_engineer.md", record(
            source="Simplify", company="Other Co", title="Backend Engineer",
            location="Toronto, ON", url="https://other.example/jobs/2", description="",
            description_complete=False, completeness_note="Listing metadata only."))
        write("jooble_evil_sre.md", record(
            source="Jooble", company="Evil <script>alert(1)</script>",
            title='SRE "><img src=x onerror=alert(1)>', location="Remote",
            url="https://jooble.org/desc/3", description="Python snippet",
            description_complete=False, completeness_note="Partial snippet."))
        outputs = self.root / "outputs"
        outputs.mkdir()
        (outputs / "example-co_platform-engineer_resume.pdf").write_bytes(b"%PDF-1.5 fake")
        (outputs / "example-co_platform-engineer_analysis.json").write_text(json.dumps({
            "source_job_description": "target_jobs/greenhouse_exampleco_platform_engineer.md",
            "resume": {"single_page_verified": True,
                       "files": {"pdf": "example-co_platform-engineer_resume.pdf"}}}))
        self.page = D.build(self.root, now=dt.datetime(2026, 10, 27, 9, 0)).read_text()

    def tearDown(self):
        self._tmp.cleanup()

    def test_collects_and_ranks_every_job(self):
        views = D.collect(self.root)
        self.assertEqual(len(views), 3)
        first = views[0]
        self.assertEqual(first.title, "Platform Engineer")
        self.assertIsNotNone(first.score)
        self.assertEqual(first.region, "US")
        self.assertIn("Python", first.matched)
        listing = next(v for v in views if v.source == "Simplify")
        self.assertIsNone(listing.score)  # listing-only jobs are never scored
        self.assertEqual(listing.region, "Canada")
        self.assertEqual(self.page.count('<article class="job'), 3)

    def test_resume_link_only_for_verified_pdf(self):
        self.assertIn('href="example-co_platform-engineer_resume.pdf"', self.page)
        self.assertEqual(self.page.count("Resume PDF"), 1)

    def test_untrusted_text_is_escaped(self):
        self.assertNotIn("<script>alert(1)</script>", self.page)
        self.assertNotIn("<img src=x", self.page)
        self.assertIn("&lt;script&gt;", self.page)

    def test_links_and_no_external_resources(self):
        hrefs = re.findall(r'href="([^"]+)"', self.page)
        for href in hrefs:
            self.assertTrue(href.startswith(("https://", "http://", "../target_jobs/"))
                            or href.endswith(".pdf"), href)
        self.assertIsNone(re.search(r'<(?:link|script)[^>]+(?:href|src)="https?:', self.page))

    def test_copyable_pipeline_command(self):
        self.assertIn('data-copy="python3 pipeline.py target_jobs/'
                      'greenhouse_exampleco_platform_engineer.md"', self.page)

    def test_stats(self):
        self.assertIn("Generated 2026-10-27 09:00", self.page)
        self.assertRegex(self.page, r'stat-big">3</p>\s*<p class="stat-label">Jobs in target_jobs, '
                                    r'2 of them listing-only')


if __name__ == "__main__":
    unittest.main()
