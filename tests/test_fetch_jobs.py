"""Configuration, HTTP behavior, source adapters, CLI and pipeline compatibility.

Every HTTP call goes to FakeOpener. Nothing here reaches the network, uses a real
Jooble key or starts an Apify run.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path

from fetch_helpers import FakeOpener, fixture, fixture_bytes, load_fetch_jobs

F = load_fetch_jobs()
TODAY = dt.date(2026, 10, 26)
JOOBLE_KEY = "test-jooble-key-0000"
APIFY_TOKEN = "apify_api_TESTTOKEN0000"


def config_with(**companies_and_sources):
    cfg = F.default_config()
    cfg["companies"] = companies_and_sources.get("companies", [])
    for name, override in companies_and_sources.get("sources", {}).items():
        cfg["sources"][name].update(override)
    return cfg


def context(cfg, opener, env=None, **kwargs):
    redact = F.Redactor([(env or {}).get("JOOBLE_API_KEY", ""), (env or {}).get("APIFY_TOKEN", "")])
    client = F.HttpClient(redactor=redact, opener=opener, sleep=lambda s: None, jitter=0,
                          budgets={n: c["max_requests_per_run"] for n, c in cfg["sources"].items()
                                   if "max_requests_per_run" in c})
    f = cfg["filters"]
    return F.RunContext(config=cfg, client=client, redact=redact, today=TODAY, env=env or {},
                        filters=F.Filters(f["title_keywords"], f["exclude_title_keywords"],
                                          f["locations"], f["max_age_days"],
                                          f["include_unspecified_remote"]), **kwargs)


GH = {"name": "Example Corp", "enabled": True, "greenhouse_board_token": "examplecorp"}
ASHBY = {"name": "Sample Co", "enabled": True, "ashby_board_name": "sampleco"}
NEW_GRAD = {"name": "New-Grad-Positions", "url": "https://github.com/SimplifyJobs/New-Grad-Positions",
            "branch": "dev", "listings_path": ".github/scripts/listings.json", "enabled": True,
            "category": "new_grad"}
INTERN = {"name": "Summer2027-Internships",
          "url": "https://github.com/SimplifyJobs/Summer2027-Internships", "branch": "dev",
          "enabled": True, "category": "internship"}


# ---------------------------------------------------------------------------
# 17.1 Configuration
# ---------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "companies.json"

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))
        return F.load_config(self.path)

    def test_missing_file_uses_defaults(self):
        cfg, notes = F.load_config(self.path)
        self.assertEqual(cfg["companies"], [])
        self.assertIn("not found", notes[0])

    def test_repository_config_is_valid(self):
        cfg, notes = F.load_config(Path(__file__).resolve().parent.parent / "config" /
                                   "companies.json")
        self.assertEqual(notes, [])
        self.assertTrue(all(isinstance(c["enabled"], bool) for c in cfg["companies"]))
        self.assertIn("United States", cfg["filters"]["locations"])
        self.assertIsInstance(cfg["filters"]["exclude_title_keywords"], list)
        repos = {r["name"]: r for r in cfg["sources"]["simplify"]["repositories"]}
        self.assertFalse(repos["Summer2027-Internships"]["enabled"])  # opt-in only
        text = json.dumps(cfg).lower()
        self.assertNotIn("api_key\":", text)
        self.assertNotIn("token\": \"apify", text)

    def test_valid_and_merged_with_defaults(self):
        cfg, _ = self.load({"companies": [GH], "sources": {"jooble": {"max_requests_per_run": 3}}})
        self.assertEqual(cfg["sources"]["jooble"]["max_requests_per_run"], 3)
        self.assertEqual(cfg["sources"]["jooble"]["locations"], F.DEFAULT_LOCATIONS)

    def test_malformed_json(self):
        with self.assertRaises(F.ConfigError):
            self.load("{not json")

    def test_invalid_values(self):
        bad = {"companies": [{"name": "", "greenhouse_board_token": "../evil"},
                             {"name": "X", "ashby_board_name": 5, "surprise": 1}],
               "sources": {"jooble": {"max_requests_per_run": -1, "api_host": "evil.com"},
                           "simplify": {"repositories": [{"url": "https://gitlab.com/x/y",
                                                          "category": "weird"}]},
                           "nosuch": {}},
               "filters": {"include_unspecified_remote": "yes"},
               "extra": True}
        with self.assertRaises(F.ConfigError) as ctx:
            self.load(bad)
        message = str(ctx.exception)
        for fragment in ("'name' must be a non-empty string", "greenhouse_board_token",
                         "unknown key 'surprise'", "max_requests_per_run must be a positive",
                         "api_host", "unknown source 'nosuch'", "unknown top-level key 'extra'",
                         "github.com", "'category'", "include_unspecified_remote"):
            self.assertIn(fragment, message)

    def test_disabled_company_makes_no_requests(self):
        opener = FakeOpener()
        cfg = config_with(companies=[{**GH, "enabled": False}])
        result = F.fetch_greenhouse(context(cfg, opener))
        self.assertEqual((result.status, opener.requests), ("skipped", []))

    def test_no_companies(self):
        result = F.fetch_ashby(context(config_with(), FakeOpener()))
        self.assertEqual(result.status, "skipped")
        self.assertIn("ashby_board_name", result.reason)

    def test_cli_limit_validation(self):
        lines: list[str] = []
        code = F.run(["--source", "jobright", "--limit", "0", "--workspace", self._tmp.name],
                     env={}, opener=FakeOpener(), out=lines.append, today=TODAY)
        self.assertEqual(code, F.EXIT_FATAL)
        self.assertIn("--limit", lines[0])


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class HttpClientTests(unittest.TestCase):
    def client(self, opener, **kwargs):
        return F.HttpClient(redactor=F.Redactor(["SECRETKEY123"]), opener=opener,
                            sleep=self.sleeps.append, jitter=0, **kwargs)

    def setUp(self):
        self.sleeps: list[float] = []

    def test_retries_transient_then_succeeds(self):
        opener = FakeOpener({"x.example": [(503, b""), {"ok": True}]})
        self.assertEqual(self.client(opener).get_json("https://x.example/a", source="t"),
                         {"ok": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_respects_retry_after(self):
        opener = FakeOpener({"x.example": [(429, b"", {"Retry-After": "7"}), {"ok": 1}]})
        self.client(opener).get_json("https://x.example/a", source="t")
        self.assertEqual(self.sleeps, [7.0])

    def test_gives_up_on_long_retry_after(self):
        opener = FakeOpener({"x.example": [(429, b"", {"Retry-After": "3600"})]})
        with self.assertRaises(F.HttpError) as ctx:
            self.client(opener).get_json("https://x.example/a", source="t")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(self.sleeps, [])

    def test_no_retry_on_client_errors(self):
        opener = FakeOpener({"x.example": [(404, {"status": 404})]})
        with self.assertRaises(F.HttpError):
            self.client(opener).get_json("https://x.example/a", source="t")
        self.assertEqual(len(opener.requests), 1)

    def test_timeout_and_network_errors(self):
        opener = FakeOpener({"x.example": [socket.timeout("timed out")]})
        with self.assertRaises(F.FetchError) as ctx:
            self.client(opener, max_retries=1).get_json("https://x.example/a", source="t")
        self.assertIn("network error", str(ctx.exception))
        self.assertEqual(len(opener.requests), 2)

    def test_malformed_json(self):
        with self.assertRaises(F.FetchError) as ctx:
            self.client(FakeOpener({"x.example": b"<html>"})).get_json("https://x.example/a",
                                                                       source="t")
        self.assertIn("malformed JSON", str(ctx.exception))

    def test_response_size_limit(self):
        opener = FakeOpener({"x.example": b"[" + b"1," * 600 + b"1]"})
        with self.assertRaises(F.FetchError) as ctx:
            self.client(opener).get_json("https://x.example/a", source="t", max_bytes=100)
        self.assertIn("exceeds", str(ctx.exception))

    def test_budget(self):
        opener = FakeOpener({"x.example": {"a": 1}})
        client = self.client(opener, budgets={"t": 1})
        client.get_json("https://x.example/1", source="t")
        with self.assertRaises(F.BudgetExceeded):
            client.get_json("https://x.example/2", source="t")

    def test_errors_and_logs_are_redacted(self):
        opener = FakeOpener({"x.example": [(403, {"error": {"type": "SECRETKEY123-bad"}})]})
        client = self.client(opener)
        with self.assertRaises(F.HttpError) as ctx:
            client.post_json("https://x.example/api/SECRETKEY123", {}, source="t")
        self.assertNotIn("SECRETKEY123", str(ctx.exception))
        self.assertNotIn("SECRETKEY123", " ".join(client.log))
        self.assertIn("***", " ".join(client.log))

    def test_cache_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = FakeOpener({"x.example": {"n": 1}})
            now = [1000.0]
            client = F.HttpClient(redactor=F.Redactor(), opener=opener, cache_dir=Path(tmp),
                                  cache_ttl=60, clock=lambda: now[0])
            client.get_json("https://x.example/a", source="t", cacheable=True)
            client.get_json("https://x.example/a", source="t", cacheable=True)
            self.assertEqual((len(opener.requests), client.stats["t"].cache_hits), (1, 1))
            now[0] += 120  # expired
            client.get_json("https://x.example/a", source="t", cacheable=True)
            self.assertEqual(len(opener.requests), 2)
            fresh = F.HttpClient(redactor=F.Redactor(), opener=opener, cache_dir=Path(tmp),
                                 cache_ttl=600, refresh=True, clock=lambda: now[0])
            fresh.get_json("https://x.example/a", source="t", cacheable=True)
            self.assertEqual(len(opener.requests), 3)


# ---------------------------------------------------------------------------
# 17.2 Greenhouse
# ---------------------------------------------------------------------------
class GreenhouseTests(unittest.TestCase):
    def run_gh(self, response, companies=(GH,)):
        opener = FakeOpener({"boards-api.greenhouse.io": response})
        result = F.fetch_greenhouse(context(config_with(companies=list(companies)), opener))
        return result, opener

    def test_valid_response(self):
        result, opener = self.run_gh(fixture("greenhouse_jobs.json"))
        url = opener.requests[0].full_url
        self.assertEqual(url, "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"
                              "?content=true")
        self.assertEqual(result.raw, 4)
        titles = [r.title for r in result.records]
        self.assertEqual(titles, ["DevOps Engineer", "Site Reliability Engineer"])
        devops = result.records[0]
        self.assertEqual(devops.company, "Example Corp")
        self.assertEqual(devops.location, "Toronto, Ontario, Canada")
        self.assertEqual(devops.posted_date, "2026-10-01")
        self.assertEqual(devops.department, "Infrastructure")
        self.assertIn("- Must have Python and Docker", devops.description)
        self.assertTrue(devops.description_complete)
        self.assertEqual(result.filtered, 1)   # Account Executive
        self.assertEqual(result.rejected, 1)   # no absolute_url

    def test_missing_optional_fields(self):
        result, _ = self.run_gh(fixture("greenhouse_jobs.json"))
        sre = result.records[1]
        self.assertEqual(sre.company, "Example Corp")  # configured name when API has none
        self.assertIsNone(sre.location)
        self.assertFalse(sre.description_complete)
        self.assertIn("no description", sre.completeness_note)

    def test_empty_list(self):
        result, _ = self.run_gh({"jobs": [], "meta": {"total": 0}})
        self.assertEqual((result.status, result.raw, result.records), ("ok", 0, []))

    def test_invalid_board_404_does_not_stop_other_boards(self):
        opener = FakeOpener({"boards/badboard/": [(404, {"status": 404, "error": "Job not found"})],
                             "boards/examplecorp/": fixture("greenhouse_jobs.json")})
        companies = [{"name": "Bad", "greenhouse_board_token": "badboard"}, GH]
        result = F.fetch_greenhouse(context(config_with(companies=companies), opener))
        self.assertEqual(result.status, "partial")
        self.assertIn("HTTP 404", result.errors[0])
        self.assertIn("unknown board token", result.errors[0])
        self.assertEqual(len(result.records), 2)

    def test_errors_never_look_like_empty_success(self):
        for response in ((500, b""), (429, b"", {"Retry-After": "999"}), b"not json",
                         {"unexpected": True}, socket.timeout("slow")):
            result, _ = self.run_gh([response])
            self.assertEqual(result.status, "failed", response)
            self.assertTrue(result.errors)


# ---------------------------------------------------------------------------
# 17.3 Ashby
# ---------------------------------------------------------------------------
class AshbyTests(unittest.TestCase):
    def run_ashby(self, response):
        opener = FakeOpener({"api.ashbyhq.com": response})
        return F.fetch_ashby(context(config_with(companies=[ASHBY]), opener)), opener

    def test_valid_board(self):
        result, opener = self.run_ashby(fixture("ashby_board.json"))
        self.assertEqual(opener.requests[0].full_url,
                         "https://api.ashbyhq.com/posting-api/job-board/sampleco")
        self.assertEqual(result.raw, 3)
        self.assertEqual(result.stale, 1)  # isListed false
        platform, sre = result.records
        self.assertEqual(platform.company, "Sample Co")
        self.assertEqual(platform.workplace_type, "Hybrid")  # not "Remote" despite isRemote
        self.assertEqual(platform.location, "Toronto; also Montreal")
        self.assertEqual(platform.employment_type, "Full-time")
        self.assertEqual(platform.department, "Engineering / Infrastructure")
        self.assertIn("**About the role**", platform.description)
        self.assertIn("1. Linux", platform.description)
        self.assertTrue(platform.application_url.endswith("/application"))
        self.assertEqual(sre.description, "Keep services up.\n\nBe on call.")  # plain fallback
        self.assertEqual(sre.workplace_type, "Remote eligible (remote vs hybrid not specified)")

    def test_error_and_malformed(self):
        for response in ((404, b"Not Found"), {"jobs": "nope"}, b"{"):
            result, _ = self.run_ashby([response])
            self.assertEqual(result.status, "failed")


# ---------------------------------------------------------------------------
# 17.4 Simplify
# ---------------------------------------------------------------------------
class SimplifyTests(unittest.TestCase):
    def run_simplify(self, repos, response):
        opener = FakeOpener({"raw.githubusercontent.com": response})
        cfg = config_with(sources={"simplify": {"repositories": repos}})
        return F.fetch_simplify(context(cfg, opener)), opener

    def test_new_grad_listing(self):
        result, opener = self.run_simplify([NEW_GRAD], fixture_bytes("simplify_listings.json"))
        self.assertEqual(opener.requests[0].full_url,
                         "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
                         ".github/scripts/listings.json")
        self.assertEqual(result.raw, 8)
        self.assertEqual([r.title for r in result.records],
                         ["DevOps Engineer", "Backend Engineer, New Grad", "Backend Engineer"])
        self.assertEqual(result.records[2].location, "Austin, TX")  # US roles are in scope
        self.assertEqual(result.stale, 2)       # inactive, older than max_age_days
        self.assertEqual(result.rejected, 1)    # no URL
        self.assertEqual(result.filtered, 2)    # "Pressure Systems" (no SRE match), Berlin
        record = result.records[0]
        self.assertFalse(record.description_complete)
        self.assertEqual(record.description, "")
        self.assertIn("does not include the job description", record.completeness_note)
        self.assertNotIn("simplify.jobs/c/", record.url)  # company_url is never the job URL
        self.assertEqual(record.posted_date, "2026-10-22")

    def test_keyword_matching_is_phrase_aware(self):
        self.assertTrue(F.matches_any("Senior SRE", ["SRE"]))
        self.assertTrue(F.matches_any("Back-End Developer", ["Backend"]))
        self.assertTrue(F.matches_any("site reliability engineer", ["Site Reliability"]))
        self.assertTrue(F.matches_any("Platforms Engineer", ["Platform"]))
        self.assertFalse(F.matches_any("Pressure Engineer", ["SRE"]))
        self.assertFalse(F.matches_any("Backendless Consultant", ["Backend"]))
        self.assertFalse(F.matches_any("Infrastructural Planner", ["Infrastructure"]))

    def test_location_regions(self):
        regions = ["Toronto", "Canada", "United States"]
        inside = ["San Francisco, CA", "New York, NY", "Remote - USA", "Remote in US",
                  "SEA, SF, NY, Toronto", "Austin, TX", "Toronto, ON", "Vancouver, BC",
                  "Montr\u00e9al, Qu\u00e9bec", "Remote (Canada)", "London, ON", "Remote"]
        outside = ["Remote - EU", "London, UK", "Berlin, Germany", "Bangalore, India",
                   "Remote, United Kingdom", "Sydney, Australia"]
        for location in inside:
            self.assertTrue(F.location_matches(location, regions), location)
        for location in outside:
            self.assertFalse(F.location_matches(location, regions), location)
        self.assertFalse(F.location_matches("Remote", regions, include_unspecified_remote=False))
        self.assertFalse(F.location_matches("Seattle, WA", ["Toronto", "Canada"]))
        self.assertTrue(F.location_matches("Remote - EU", ["Remote"]))  # plain phrase term

    def test_senior_titles_excluded_by_default(self):
        cfg = config_with()
        filters = context(cfg, FakeOpener()).filters
        record = lambda t: F.JobRecord("Greenhouse", "1", "X", t, "Austin, TX",
                                       "https://x.example/1", "d", True)
        for title in ("Senior Backend Engineer", "Sr. Platform Engineer", "Staff SRE",
                      "Engineering Manager, Infrastructure", "Tech Lead, Platform",
                      "Principal Site Reliability Engineer"):
            self.assertIsNotNone(filters.check(record(title)), title)
        for title in ("Backend Engineer II", "Software Engineer, Platform",
                      "Site Reliability Engineer", "Leadership Platform Engineer"):
            self.assertIsNone(filters.check(record(title)), title)

    def test_internship_past_terms_excluded(self):
        result, _ = self.run_simplify([INTERN], fixture_bytes("simplify_internships.json"))
        self.assertEqual([r.title for r in result.records], ["Platform Intern", "DevOps Intern"])
        self.assertEqual(result.stale, 1)  # Summer 2026 already started
        self.assertEqual(result.records[0].terms, ["Summer 2027"])
        everything, _ = self.run_simplify([{**INTERN, "include_past_terms": True}],
                                          fixture_bytes("simplify_internships.json"))
        self.assertEqual(len(everything.records), 3)

    def test_term_dates(self):
        self.assertTrue(F.term_has_started("Summer 2026", TODAY))
        self.assertTrue(F.term_has_started("Fall 2026", TODAY))
        self.assertFalse(F.term_has_started("Winter 2027", TODAY))
        self.assertIsNone(F.term_has_started("N/A", TODAY))

    def test_large_or_bad_files(self):
        result, _ = self.run_simplify([NEW_GRAD], [{"not": "a list"}])
        self.assertEqual(result.status, "failed")
        opener = FakeOpener({"raw.githubusercontent.com": b"[" + b"0," * 2_000_000 + b"0]"})
        cfg = config_with(sources={"simplify": {"repositories": [NEW_GRAD], "max_response_mb": 1}})
        big = F.fetch_simplify(context(cfg, opener))
        self.assertEqual(big.status, "failed")
        self.assertIn("exceeds", big.errors[0])


# ---------------------------------------------------------------------------
# 17.5 Jooble
# ---------------------------------------------------------------------------
class JoobleTests(unittest.TestCase):
    def run_jooble(self, response, env=None, **cfg_sources):
        opener = FakeOpener({"jooble.org/api/": response})
        cfg = config_with(sources={"jooble": cfg_sources} if cfg_sources else {})
        ctx = context(cfg, opener, env={"JOOBLE_API_KEY": JOOBLE_KEY} if env is None else env)
        return F.fetch_jooble(ctx), opener, ctx

    def test_missing_key_skips(self):
        result, opener, _ = self.run_jooble({}, env={})
        self.assertEqual((result.status, opener.requests), ("skipped", []))
        self.assertIn("JOOBLE_API_KEY", result.reason)

    def test_post_with_json_body_and_budget(self):
        result, opener, ctx = self.run_jooble(fixture("jooble_response.json"),
                                              max_requests_per_run=2)
        self.assertEqual(len(opener.requests), 2)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, f"https://jooble.org/api/{JOOBLE_KEY}")
        self.assertEqual(opener.bodies()[0], {"keywords": "DevOps Engineer", "location": "Toronto",
                                              "page": 1, "ResultOnPage": 20})
        self.assertEqual(opener.bodies()[1]["location"], "Canada")
        self.assertTrue(any("budget" in w for w in result.warnings))
        self.assertNotIn(JOOBLE_KEY, " ".join(ctx.client.log))

    def test_snippets_are_partial_and_filtered(self):
        result, _, _ = self.run_jooble(fixture("jooble_response.json"), max_requests_per_run=1)
        self.assertEqual(result.raw, 3)
        self.assertEqual(result.filtered, 1)  # Barista
        first = result.records[0]
        self.assertFalse(first.description_complete)
        self.assertIn("**Kubernetes**", first.description)
        self.assertIn("snippet", first.completeness_note)
        self.assertEqual(first.salary, "$90k - $110k")
        self.assertEqual(first.updated_date, "2026-10-20")

    def test_cli_overrides_search_plan(self):
        cfg = config_with()
        ctx = context(cfg, FakeOpener(), keyword="SRE", location="Toronto")
        self.assertEqual(F.jooble_searches(ctx), [("SRE", "Toronto", 1)])

    def test_regional_host_and_errors(self):
        result, opener, _ = self.run_jooble([(403, b"")], api_host="ca.jooble.org")
        self.assertTrue(opener.requests[0].full_url.startswith("https://ca.jooble.org/api/"))
        self.assertEqual(result.status, "failed")
        self.assertIn("different Jooble country domain", result.errors[0])
        self.assertEqual(len(opener.requests), 1)  # a bad key stops further searches
        self.assertNotIn(JOOBLE_KEY, result.errors[0])
        for response in ((429, b"", {"Retry-After": "999"}), b"[1,", {"jobs": None}):
            failed, _, _ = self.run_jooble([response], max_requests_per_run=1)
            self.assertEqual(failed.status, "failed")

    def test_empty_results(self):
        result, _, _ = self.run_jooble({"totalCount": 0, "jobs": []}, max_requests_per_run=1)
        self.assertEqual((result.status, result.records), ("ok", []))


# ---------------------------------------------------------------------------
# 17.6 Jobright via Apify
# ---------------------------------------------------------------------------
class JobrightTests(unittest.TestCase):
    def run_jobright(self, response, allow=True, env=None, **kwargs):
        opener = FakeOpener({"api.apify.com": response})
        ctx = context(config_with(), opener,
                      env={"APIFY_TOKEN": APIFY_TOKEN} if env is None else env,
                      allow_apify_spend=allow, **kwargs)
        return F.fetch_jobright(ctx), opener, ctx

    def test_requires_spend_opt_in(self):
        result, opener, _ = self.run_jobright([], allow=False)
        self.assertEqual((result.status, opener.requests), ("skipped", []))
        self.assertIn("--allow-apify-spend", result.reason)

    def test_requires_token(self):
        result, opener, _ = self.run_jobright([], env={})
        self.assertEqual((result.status, opener.requests), ("skipped", []))

    def test_request_shape_and_auth_header(self):
        result, opener, ctx = self.run_jobright(fixture_bytes("apify_dataset.json"), keyword="SRE",
                                                location="Toronto, Canada", limit=2)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.startswith(
            "https://api.apify.com/v2/acts/jobscrawler~jobright-scraper/"
            "run-sync-get-dataset-items?"))
        self.assertIn("maxItems=2", request.full_url)
        self.assertIn("maxTotalChargeUsd=0.5", request.full_url)
        self.assertNotIn(APIFY_TOKEN, request.full_url)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {APIFY_TOKEN}")
        self.assertEqual(opener.bodies()[0], {"keyword": "SRE", "location": "Toronto, Canada",
                                              "country": "CA", "maxItems": 2,
                                              "includeCompanyDetails": False,
                                              "includeSalary": True})
        self.assertEqual(result.raw, 2)  # dataset of 3 trimmed to the limit
        self.assertTrue(any("kept the first 2" in w for w in result.warnings))
        self.assertNotIn(APIFY_TOKEN, json.dumps(ctx.client.log))

    def test_normalization(self):
        result, _, _ = self.run_jobright(fixture_bytes("apify_dataset.json"), limit=3)
        nested, short, _extra = result.records
        self.assertEqual(nested.company, "Nested Co")
        self.assertEqual(nested.location, "Toronto, ON; Remote")
        self.assertEqual(nested.url, "https://nested.example/jobs/5/apply")  # not companyUrl
        self.assertNotIn("does many things", nested.description)  # company text excluded
        self.assertIn("- 3+ years with Linux", nested.description)
        self.assertTrue(nested.description_complete)
        self.assertEqual(nested.salary, "$120,000/yr")
        self.assertFalse(short.description_complete)
        self.assertIn("may be a summary", short.completeness_note)

    def test_actor_errors(self):
        for response, fragment in (((402, {"error": {"type": "x402-payment-required"}}), "credit"),
                                   ((408, b""), "synchronous wait"), ((401, b""), "invalid"),
                                   ({"error": {"type": "run-failed"}}, "run-failed"),
                                   ({"items": []}, "not a JSON list")):
            result, _, _ = self.run_jobright([response])
            self.assertEqual(result.status, "failed")
            self.assertIn(fragment, result.errors[0])

    def test_timeout(self):
        result, _, _ = self.run_jobright([socket.timeout("slow")])
        self.assertEqual(result.status, "failed")


# ---------------------------------------------------------------------------
# CLI, summary and end-to-end compatibility with pipeline.py
# ---------------------------------------------------------------------------
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        cfg = {"companies": [GH],
               "sources": {"simplify": {"repositories": [NEW_GRAD]},
                           "jooble": {"max_requests_per_run": 1}}}
        (self.root / "config" / "companies.json").write_text(json.dumps(cfg))
        self.opener = FakeOpener({"boards-api.greenhouse.io": fixture("greenhouse_jobs.json"),
                                  "raw.githubusercontent.com": fixture_bytes("simplify_listings.json"),
                                  "jooble.org/api/": fixture("jooble_response.json")})

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args, env=None):
        lines: list[str] = []
        code = F.run([*args, "--workspace", str(self.root)],
                     env={"JOOBLE_API_KEY": JOOBLE_KEY, "APIFY_TOKEN": APIFY_TOKEN}
                     if env is None else env,
                     opener=self.opener, sleep=lambda s: None, today=TODAY, out=lines.append)
        return code, lines

    def test_multi_source_run(self):
        code, lines = self.cli()
        self.assertEqual(code, F.EXIT_OK)
        target = self.root / "target_jobs"
        names = sorted(p.name for p in target.glob("*.md"))
        self.assertIn("greenhouse_examplecorp_devops_engineer.md", names)
        self.assertNotIn("simplify_examplecorp_devops_engineer.md", names)  # deduplicated
        self.assertIn("simplify_otherinc_backend_engineer_new_grad.md", names)
        self.assertIn("jooble_sampleco_platform_engineer.md", names)
        summary = json.loads((self.root / "outputs" / "fetch_summary.json").read_text())
        self.assertGreaterEqual(summary["totals"]["duplicates_removed"], 1)
        jobright = next(s for s in summary["sources"] if s["source"] == "Jobright")
        self.assertEqual(jobright["status"], "skipped")  # never billed without the flag
        self.assertFalse(any("api.apify.com" in r.full_url for r in self.opener.requests))
        manifest = json.loads((target / F.MANIFEST_NAME).read_text())
        gh_entry = manifest["files"]["greenhouse_examplecorp_devops_engineer.md"]
        self.assertTrue(any("utm_source=Simplify" in u for u in gh_entry["also_seen_at"]))
        for path in [*target.iterdir(), *(self.root / "outputs").iterdir()]:
            text = path.read_text()
            self.assertNotIn(JOOBLE_KEY, text, path.name)
            self.assertNotIn(APIFY_TOKEN, text, path.name)
        again, _ = self.cli()
        self.assertEqual(again, F.EXIT_OK)
        self.assertEqual(sorted(p.name for p in target.glob("*.md")), names)
        summary = json.loads((self.root / "outputs" / "fetch_summary.json").read_text())
        self.assertEqual(summary["totals"]["files_created"], 0)

    def test_generated_file_is_compatible_with_pipeline(self):
        self.cli()
        pipeline = __import__("fixtures").load_pipeline()
        path = self.root / "target_jobs" / "greenhouse_examplecorp_devops_engineer.md"
        jd = pipeline.read_job_description(path, self.root)
        self.assertEqual((jd.company, jd.company_source), ("Example Corp", "metadata"))
        self.assertEqual((jd.role, jd.role_source), ("DevOps Engineer", "metadata"))
        self.assertFalse(jd.partial_description)
        meta = [l for l in jd.lines if l.section_title == "metadata"]
        self.assertTrue(meta)
        self.assertTrue(all(l.section_type == "info" for l in meta))
        with tempfile.TemporaryDirectory() as tmp:
            from fixtures import MASTER
            master_path = Path(tmp) / "master.md"
            master_path.write_text(MASTER)
            analysis = pipeline.analyze(jd, pipeline.parse_master(master_path))
        names = {r.name for r in analysis.requirements}
        self.assertIn("Python", names)
        self.assertEqual(next(r for r in analysis.requirements if r.name == "Python").category,
                         "mandatory")
        self.assertEqual(next(r for r in analysis.requirements if r.name == "AWS").category,
                         "preferred")
        metadata_lines = {l.number for l in meta}  # no requirement may come only from these
        for req in analysis.requirements:
            self.assertFalse(set(req.jd_lines) <= metadata_lines, req.name)

    def test_partial_file_is_flagged_by_pipeline(self):
        self.cli()
        pipeline = __import__("fixtures").load_pipeline()
        path = self.root / "target_jobs" / "jooble_sampleco_platform_engineer.md"
        jd = pipeline.read_job_description(path, self.root)
        self.assertTrue(jd.partial_description)
        self.assertTrue(any("partial" in w for w in jd.warnings))

    def test_dry_run_writes_nothing(self):
        code, lines = self.cli("--dry-run")
        self.assertEqual(code, F.EXIT_OK)
        self.assertFalse((self.root / "target_jobs").exists())
        self.assertFalse((self.root / "outputs").exists())
        self.assertIn("DRY RUN", lines[0])

    def test_failed_source_gives_partial_exit_code(self):
        self.opener.routes["boards-api.greenhouse.io"] = [(500, b"")]
        code, lines = self.cli()
        self.assertEqual(code, F.EXIT_SOURCE_FAILED)
        self.assertTrue((self.root / "target_jobs" /
                         "simplify_examplecorp_devops_engineer.md").exists())
        self.assertTrue(any("error:" in line for line in lines))

    def test_single_source_without_credentials(self):
        code, lines = self.cli("--source", "jooble", env={})
        self.assertEqual(code, F.EXIT_FATAL)
        self.assertTrue(any("JOOBLE_API_KEY is not set" in line for line in lines))
        code, _ = self.cli("--source", "jobright")
        self.assertEqual(code, F.EXIT_FATAL)  # skipped: no --allow-apify-spend

    def test_jobright_cli_with_spend_flag(self):
        self.opener.routes["api.apify.com"] = fixture_bytes("apify_dataset.json")
        code, _ = self.cli("--source", "jobright", "--keyword", "Platform Engineer",
                           "--location", "Toronto", "--limit", "2", "--allow-apify-spend")
        self.assertEqual(code, F.EXIT_OK)
        body = json.loads(self.opener.requests[-1].data)
        self.assertEqual((body["keyword"], body["location"], body["maxItems"]),
                         ("Platform Engineer", "Toronto", 2))

    def test_manual_file_untouched_by_cli(self):
        target = self.root / "target_jobs"
        target.mkdir()
        manual = target / "greenhouse_examplecorp_devops_engineer.md"
        manual.write_text("my own version\n")
        self.cli()
        self.assertEqual(manual.read_text(), "my own version\n")
        summary = json.loads((self.root / "outputs" / "fetch_summary.json").read_text())
        self.assertEqual(summary["files"]["skipped_manual"],
                         ["greenhouse_examplecorp_devops_engineer.md"])

    def test_unexpected_adapter_error_fails_only_that_source(self):
        original = F.ADAPTERS["greenhouse"]
        F.ADAPTERS["greenhouse"] = lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            code, lines = self.cli()
        finally:
            F.ADAPTERS["greenhouse"] = original
        self.assertEqual(code, F.EXIT_SOURCE_FAILED)
        self.assertTrue(any("internal error while processing Greenhouse" in l for l in lines))
        self.assertTrue((self.root / "target_jobs" /
                         "simplify_examplecorp_devops_engineer.md").exists())

    def test_malformed_config_is_fatal(self):
        (self.root / "config" / "companies.json").write_text("{")
        code, lines = self.cli()
        self.assertEqual(code, F.EXIT_FATAL)
        self.assertIn("not valid JSON", lines[0])


if __name__ == "__main__":
    unittest.main()
