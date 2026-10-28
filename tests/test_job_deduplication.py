"""Canonical URLs, cross-source deduplication and manifest-protected writing."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fetch_helpers import load_fetch_jobs

F = load_fetch_jobs()
TODAY = dt.date(2026, 10, 26)


def rec(source="Greenhouse", company="Acme", title="DevOps Engineer", location="Toronto, ON",
        url="https://job-boards.greenhouse.io/acme/jobs/1", description="Full text " * 20,
        complete=True, job_id="1"):
    return F.JobRecord(source=source, source_job_id=job_id, company=company, title=title,
                       location=location, url=url, description=description,
                       description_complete=complete,
                       completeness_note=None if complete else "Partial snippet.")


class CanonicalUrlTests(unittest.TestCase):
    def test_tracking_parameters_removed(self):
        self.assertEqual(
            F.canonical_url("HTTP://WWW.Example.com/jobs/1/?utm_source=Simplify&ref=Simplify"
                            "&gh_src=abc#apply"),
            "https://example.com/jobs/1")

    def test_trailing_slash_and_default_port(self):
        self.assertEqual(F.canonical_url("https://example.com:443/jobs/1/"),
                         F.canonical_url("https://example.com/jobs/1"))

    def test_identifying_query_parameters_kept(self):
        a = F.canonical_url("https://careers.example.com/job?id=10&utm_medium=x")
        b = F.canonical_url("https://careers.example.com/job?id=11")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "https://careers.example.com/job?id=10")

    def test_path_case_preserved(self):
        self.assertNotEqual(F.canonical_url("https://x.example/Job/AbC"),
                            F.canonical_url("https://x.example/job/abc"))

    def test_strong_ats_identities(self):
        self.assertEqual(F.strong_identity("https://boards.greenhouse.io/acme/jobs/123"),
                         "greenhouse:123")
        self.assertEqual(F.strong_identity("https://job-boards.greenhouse.io/acme/jobs/123?x=1"),
                         "greenhouse:123")
        self.assertEqual(F.strong_identity("https://acme.com/careers?gh_jid=123"),
                         "greenhouse:123")
        uuid = "11111111-2222-4333-8444-555555555555"
        self.assertEqual(F.strong_identity(f"https://jobs.ashbyhq.com/acme/{uuid}/application"),
                         f"ashby:{uuid}")
        self.assertEqual(F.strong_identity(f"https://jobs.lever.co/acme/{uuid.upper()}"),
                         f"lever:{uuid}")
        self.assertIsNone(F.strong_identity("https://acme.com/careers/123"))


class DeduplicationTests(unittest.TestCase):
    def test_identical_url_across_sources_keeps_full_ats_record(self):
        ats = rec()
        simplify = rec(source="Simplify", description="", complete=False,
                       url="https://job-boards.greenhouse.io/acme/jobs/1?utm_source=Simplify")
        jooble = rec(source="Jooble", description="snippet", complete=False,
                     url="https://acme.com/careers?gh_jid=1")
        result = F.deduplicate([simplify, jooble, ats])
        self.assertEqual(len(result.kept), 1)
        self.assertIs(result.kept[0], ats)
        self.assertEqual(len(result.duplicates), 2)
        self.assertEqual(len(ats.also_seen_at), 2)

    def test_full_description_never_replaced_by_snippet(self):
        snippet_ats = rec(description="short", complete=False)
        full_aggregator = rec(source="Jobright", url="https://jobs.example/acme/1",
                              description="Complete " * 100, complete=True)
        result = F.deduplicate([snippet_ats, full_aggregator])
        self.assertEqual(len(result.kept), 1)
        self.assertIs(result.kept[0], full_aggregator)

    def test_distinct_job_ids_stay_separate(self):
        a = rec(url="https://job-boards.greenhouse.io/acme/jobs/1")
        b = rec(url="https://job-boards.greenhouse.io/acme/jobs/2", job_id="2")
        result = F.deduplicate([a, b])
        self.assertEqual(len(result.kept), 2)
        self.assertEqual(len(result.possible), 1)  # same title: reported, not merged

    def test_same_title_different_companies(self):
        result = F.deduplicate([rec(), rec(company="Other", url="https://other.example/1",
                                           source="Jooble", complete=False)])
        self.assertEqual(len(result.kept), 2)
        self.assertEqual(result.possible, [])

    def test_similar_titles_same_company_not_merged(self):
        result = F.deduplicate([rec(), rec(title="Senior DevOps Engineer", source="Jooble",
                                           url="https://jooble.org/desc/9", complete=False)])
        self.assertEqual(len(result.kept), 2)

    def test_conservative_fallback_identity(self):
        ats = rec()
        aggregator = rec(source="Jooble", url="https://jooble.org/desc/55", complete=False,
                         description="snippet", location="Toronto")
        result = F.deduplicate([aggregator, ats])
        self.assertEqual(len(result.kept), 1)
        self.assertIs(result.kept[0], ats)
        self.assertIn("different sources", result.duplicates[0]["reason"])

    def test_fallback_refuses_two_ats_postings(self):
        one = rec(url="https://job-boards.greenhouse.io/acme/jobs/1")
        two = rec(source="Ashby", url="https://jobs.ashbyhq.com/acme/"
                                      "11111111-2222-4333-8444-555555555555")
        result = F.deduplicate([one, two])
        self.assertEqual(len(result.kept), 2)
        self.assertEqual(len(result.possible), 1)

    def test_same_source_same_title_kept(self):
        a = rec(source="Jooble", url="https://jooble.org/desc/1", complete=False)
        b = rec(source="Jooble", url="https://jooble.org/desc/2", complete=False)
        self.assertEqual(len(F.deduplicate([a, b]).kept), 2)

    def test_deterministic_order(self):
        records = [rec(source="Jooble", company="B", url="https://b.example/1", complete=False),
                   rec(company="A", url="https://job-boards.greenhouse.io/a/jobs/9")]
        first = [r.url for r in F.deduplicate(records).kept]
        second = [r.url for r in F.deduplicate(list(reversed(records))).kept]
        self.assertEqual(first, second)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "target_jobs"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, records, dry=False, today=TODAY, meta=True):
        return F.write_records(records, self.dir, today, dry, meta)

    def test_filename_format_and_safety(self):
        record = rec(company="Ex-ample Corp.", title="../../Site Reliability Engineer (SRE) / II")
        out = self.write([record])
        self.assertEqual(out.created,
                         ["greenhouse_examplecorp_site_reliability_engineer_sre_ii.md"])
        self.assertTrue((self.dir / out.created[0]).is_file())
        long = rec(title="Platform " * 40, url="https://job-boards.greenhouse.io/acme/jobs/7")
        name = F.base_filename(long)
        self.assertLessEqual(len(name), 12 + F.MAX_COMPANY_SLUG + F.MAX_ROLE_SLUG)
        for bad in ("../x.md", "a/b.md", ".hidden.md", "x.txt"):
            with self.assertRaises(F.ConfigError):
                F.safe_target(self.dir, bad)

    def test_idempotent_runs(self):
        records = [rec()]
        first = self.write(records)
        second = self.write([rec()], today=TODAY + dt.timedelta(days=1))
        self.assertEqual(len(first.created), 1)
        self.assertEqual(second.created, [])
        self.assertEqual(second.unchanged, first.created)
        text = (self.dir / first.created[0]).read_text()
        self.assertIn("**Date Ingested**: 2026-10-26", text)  # not rewritten for a date change
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         sorted([first.created[0], F.MANIFEST_NAME]))

    def test_updated_description_refreshes_owned_file(self):
        self.write([rec(description="Old text " * 10)])
        out = self.write([rec(description="New text " * 10)])
        self.assertEqual(len(out.updated), 1)
        self.assertIn("New text", (self.dir / out.updated[0]).read_text())

    def test_collision_gets_stable_suffix(self):
        a = rec(url="https://job-boards.greenhouse.io/acme/jobs/1")
        b = rec(url="https://job-boards.greenhouse.io/acme/jobs/2", job_id="2")
        out = self.write([a, b])
        self.assertEqual(len(out.created), 2)
        self.assertEqual(out.created[0], "greenhouse_acme_devops_engineer.md")
        self.assertRegex(out.created[1], r"^greenhouse_acme_devops_engineer_[0-9a-f]{8}\.md$")
        again = self.write([b, a])
        self.assertEqual(sorted(again.unchanged), sorted(out.created))

    def test_manual_file_never_overwritten(self):
        self.dir.mkdir(parents=True)
        manual = self.dir / "greenhouse_acme_devops_engineer.md"
        manual.write_text("# My notes\n", encoding="utf-8")
        out = self.write([rec()])
        self.assertEqual(out.skipped_manual, ["greenhouse_acme_devops_engineer.md"])
        self.assertEqual(manual.read_text(), "# My notes\n")
        other = self.dir / "my_own_job.md"
        other.write_text("hand written", encoding="utf-8")
        self.write([rec(url="https://job-boards.greenhouse.io/acme/jobs/5", job_id="5",
                        title="Platform Engineer")])
        self.assertEqual(other.read_text(), "hand written")

    def test_hand_edited_generated_file_protected(self):
        out = self.write([rec(description="Old " * 10)])
        path = self.dir / out.created[0]
        path.write_text(path.read_text() + "\nMy edit\n", encoding="utf-8")
        again = self.write([rec(description="New " * 10)])
        self.assertEqual(again.skipped_modified, out.created)
        self.assertIn("My edit", path.read_text())

    def test_stale_generated_files_not_deleted(self):
        out = self.write([rec()])
        self.write([rec(url="https://job-boards.greenhouse.io/acme/jobs/9", job_id="9",
                        title="Backend Engineer")])
        self.assertTrue((self.dir / out.created[0]).exists())

    def test_dry_run_writes_nothing(self):
        out = self.write([rec()], dry=True)
        self.assertEqual(out.created, ["greenhouse_acme_devops_engineer.md"])
        self.assertFalse(self.dir.exists())

    def test_metadata_only_can_be_suppressed(self):
        simplify = rec(source="Simplify", description="", complete=False)
        out = self.write([simplify], meta=False)
        self.assertEqual((out.created, out.not_written), ([], 1))

    def test_manifest_records_identity_and_urls(self):
        record = rec()
        record.also_seen_at.append("https://simplify.example/x")
        out = self.write([record])
        manifest = json.loads((self.dir / F.MANIFEST_NAME).read_text())
        entry = manifest["files"][out.created[0]]
        self.assertEqual(entry["identity"], "greenhouse:1")
        self.assertEqual(entry["also_seen_at"], ["https://simplify.example/x"])



class CrossRunDeduplicationTests(unittest.TestCase):
    """The manifest's dedup_key stops a job written from one source being re-ingested."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "target_jobs"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, records, dry=False):
        return F.write_records(records, self.dir, TODAY, dry, True)

    def manifest(self) -> dict:
        return json.loads((self.dir / F.MANIFEST_NAME).read_text())["files"]

    def test_key_normalizes_company_title_and_location(self):
        key = F.dedup_key("Acme Inc.", "DevOps Engineer", "Toronto, ON")
        self.assertEqual(key, F.dedup_key("ACME", "devops  engineer", "Toronto"))
        self.assertEqual(key, hashlib.sha256(b"acme|devops engineer|toronto").hexdigest())
        self.assertNotEqual(key, F.dedup_key("Acme", "DevOps Engineer", "Montreal, QC"))
        self.assertIsNone(F.dedup_key("Acme", "DevOps Engineer", None))

    def test_same_job_from_another_source_in_a_later_run_is_skipped(self):
        first = self.write([rec()])
        jooble = rec(source="Jooble", url="https://jooble.org/desc/123", job_id=None,
                     description="Snippet", complete=False, location="Toronto")
        second = self.write([jooble])
        self.assertEqual((second.created, second.updated), ([], []))
        self.assertEqual(second.skipped_duplicate,
                         [f"Jooble: https://jooble.org/desc/123 (already ingested as "
                          f"{first.created[0]})"])
        entry = self.manifest()[first.created[0]]
        self.assertEqual(entry["also_seen_at"], ["https://jooble.org/desc/123"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         sorted([first.created[0], F.MANIFEST_NAME]))

    def test_dry_run_reports_the_duplicate_without_touching_the_manifest(self):
        self.write([rec()])
        before = (self.dir / F.MANIFEST_NAME).read_text()
        out = self.write([rec(source="Jobright", url="https://jobright.ai/jobs/9", job_id="9")],
                         dry=True)
        self.assertEqual(len(out.skipped_duplicate), 1)
        self.assertEqual((self.dir / F.MANIFEST_NAME).read_text(), before)

    def test_same_source_postings_are_kept(self):
        self.write([rec()])
        out = self.write([rec(url="https://job-boards.greenhouse.io/acme/jobs/2", job_id="2")])
        self.assertEqual(len(out.created), 1)
        self.assertEqual(out.skipped_duplicate, [])

    def test_two_ats_job_ids_are_never_merged(self):
        self.write([rec()])
        ashby = rec(source="Ashby", job_id=None,
                    url="https://jobs.ashbyhq.com/acme/4c385433-be83-4338-ab0b-11e9fae5402a")
        out = self.write([ashby])
        self.assertEqual(len(out.created), 1)

    def test_full_description_is_not_dropped_for_a_partial_file(self):
        self.write([rec(source="Jooble", url="https://jooble.org/desc/1", job_id=None,
                        description="Snippet", complete=False)])
        out = self.write([rec()])
        self.assertEqual(len(out.created), 1)
        self.assertEqual(out.skipped_duplicate, [])

    def test_manifest_stores_the_key_and_backfills_old_entries(self):
        out = self.write([rec()])
        name = out.created[0]
        manifest_path = self.dir / F.MANIFEST_NAME
        data = json.loads(manifest_path.read_text())
        expected = F.dedup_key("Acme", "DevOps Engineer", "Toronto, ON")
        self.assertEqual(data["files"][name]["dedup_key"], expected)
        del data["files"][name]["dedup_key"]  # a manifest written before dedup keys
        manifest_path.write_text(json.dumps(data))
        again = self.write([rec(source="Jooble", url="https://jooble.org/desc/7", job_id=None,
                                description="Snippet", complete=False)])
        self.assertEqual(len(again.skipped_duplicate), 1)
        self.assertEqual(self.manifest()[name]["dedup_key"], expected)


if __name__ == "__main__":
    unittest.main()
