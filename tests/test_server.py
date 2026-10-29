"""Vouch Studio API (server.py): offline, against a synthetic workspace.

Runs when FastAPI is installed (``.venv/bin/python -m unittest discover -s tests``)
and is skipped otherwise, so the standard-library suite stays green on its own.
The toolchain is FakeTex and HTTP is FakeOpener: no compiler runs, no request
leaves the machine, and the real master profile and template are never touched.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fetch_helpers import FakeOpener, fixture, load_fetch_jobs
from fixtures import JD_PROCEED, MASTER, MASTER_KIT, ROOT, TEMPLATE, FakeTex, fake_which, \
    load_pipeline, make_workspace

P = load_pipeline()
F = load_fetch_jobs()
sys.path.insert(0, str(ROOT))
try:
    from fastapi.testclient import TestClient

    import server as S
except ImportError:  # FastAPI lives in .venv only
    S = None

HEADERS = {"X-Vouch-Studio": "1"}
PASSWORD = "correct horse battery"
OWNER = "owner@example.com"
JOOBLE_KEY = "studio-test-jooble-key-0000"


def events_of(body: str) -> list[tuple[str, object]]:
    events = []
    for block in body.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


class BlockingTex(FakeTex):
    """Holds every compile until released, so a task stays 'running'."""

    def __init__(self, release: threading.Event):
        super().__init__()
        self.release = release

    def __call__(self, command, **kwargs):
        self.release.wait(10)
        return super().__call__(command, **kwargs)


@unittest.skipUnless(S, "FastAPI is not installed; run the suite with .venv/bin/python")
class StudioCase(unittest.TestCase):
    runner_factory = FakeTex

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name), master=MASTER_KIT)
        self.opener = FakeOpener({"boards-api.greenhouse.io": fixture("greenhouse_jobs.json"),
                                  "jooble.org": fixture("jooble_response.json")})
        self.runner = self.runner_factory()
        self.studio = S.Studio(self.root, which=fake_which(), runner=self.runner,
                               opener=self.opener, env={}, sleep=lambda s: None)
        self.app = S.create_app(self.studio)
        self.client = self.make_client(OWNER, admin=True)  # the workspace owner

    def make_client(self, email: str | None = None, admin: bool = False) -> "TestClient":
        """A browser: signed up (and signed in) as `email`, or anonymous."""
        client = TestClient(self.app, base_url="http://127.0.0.1:8000", follow_redirects=False)
        if email:
            res = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD},
                              headers=HEADERS)
            self.assertEqual(res.status_code, 201, res.text)
            if admin:
                self.studio.accounts.set_admin(email)
        return client

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def post(self, path: str, body: dict | None = None, headers: dict | None = None):
        return self.client.post(path, json=body or {}, headers=HEADERS if headers is None else headers)

    def follow(self, task: dict) -> tuple[list[str], dict]:
        """Read a task's SSE stream to the end: (log lines, finished task)."""
        with self.client.stream("GET", f"/api/tasks/{task['id']}/events") as res:
            self.assertEqual(res.headers["content-type"].split(";")[0], "text/event-stream")
            body = "".join(res.iter_text())
        events = events_of(body)
        self.assertEqual(events[-1][0], "done")
        return [data for name, data in events if name == "log"], events[-1][1]

    def tailor(self, job_id: str, **options) -> tuple[list[str], dict]:
        res = self.post(f"/api/tailor/{job_id}", options)
        self.assertEqual(res.status_code, 202, res.text)
        return self.follow(res.json())


class SecurityTests(StudioCase):
    def test_foreign_host_is_refused(self):
        res = self.client.get("/api/meta", headers={"host": "evil.example:8000"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.client.get("/api/meta").status_code, 200)

    def test_state_changes_need_the_studio_header_and_a_local_origin(self):
        body = {"text": "# Role at Co\n\n- Python required"}
        self.assertEqual(self.post("/api/jobs/upload", body, headers={}).status_code, 403)
        cross = dict(HEADERS, Origin="https://evil.example")
        self.assertEqual(self.post("/api/jobs/upload", body, headers=cross).status_code, 403)
        local = dict(HEADERS, Origin="http://127.0.0.1:8000")
        self.assertEqual(self.post("/api/jobs/upload", body, headers=local).status_code, 201)

    def test_security_headers_and_static_shell(self):
        res = self.client.get("/app")
        self.assertEqual(res.status_code, 200)
        self.assertIn("/static/studio.js", res.text)
        self.assertIn("script-src 'self'", res.headers["content-security-policy"])
        self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/api/meta").headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/static/studio.js").status_code, 200)

    def test_page_and_assets_are_revalidated_so_ui_edits_show(self):
        for path in ("/", "/app", "/static/studio.css", "/static/studio.js"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertEqual(res.headers["cache-control"], "no-cache", path)
        self.assertEqual(self.client.get("/api/meta").headers["cache-control"], "no-store")

    def test_oversized_body_is_refused(self):
        res = self.client.post("/api/jobs/upload", content=b"x" * (S.MAX_BODY_BYTES + 1),
                               headers=dict(HEADERS, **{"Content-Type": "application/json"}))
        self.assertEqual(res.status_code, 413)


RESUME = ("Platform engineer\n\nExperience\n- Built Python services with Docker and "
          "PostgreSQL for internal teams across several product groups\n- Wrote tests and "
          "deployment scripts that kept releases steady during busy quarters\n")


def b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


class AccountTests(StudioCase):
    """Sign-up, sessions, roles and each account's own profile."""

    def test_signup_login_logout_and_me(self):
        anon = self.make_client()
        self.assertEqual(anon.get("/api/me").status_code, 401)
        self.assertEqual(anon.get("/api/jobs").status_code, 401)
        app = anon.get("/app")
        self.assertEqual((app.status_code, app.headers["location"]), (303, "/login"))
        self.assertEqual(anon.get("/").status_code, 200)  # the home page is public
        self.assertEqual(anon.get("/signup").status_code, 200)
        res = anon.post("/api/auth/signup", json={"email": "Dana@Example.com", "password": PASSWORD,
                                                  "name": "Dana"}, headers=HEADERS)
        self.assertEqual(res.status_code, 201, res.text)
        cookie = res.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        me = anon.get("/api/me").json()["user"]
        self.assertEqual((me["email"], me["name"], me["is_admin"]), ("dana@example.com", "Dana", False))
        self.assertEqual(anon.get("/login").status_code, 303)  # already signed in
        self.assertEqual(anon.post("/api/auth/logout", headers=HEADERS).status_code, 200)
        self.assertEqual(anon.get("/api/me").status_code, 401)
        wrong = anon.post("/api/auth/login", json={"email": "dana@example.com", "password": "nope-nope"},
                          headers=HEADERS)
        self.assertEqual(wrong.status_code, 401)
        right = anon.post("/api/auth/login", json={"email": "DANA@example.com", "password": PASSWORD},
                          headers=HEADERS)
        self.assertEqual(right.status_code, 200)
        self.assertEqual(anon.get("/api/me").status_code, 200)

    def test_passwords_and_session_tokens_are_stored_hashed(self):
        import hashlib
        import sqlite3
        conn = sqlite3.connect(self.studio.accounts.db_path)
        stored = conn.execute("SELECT password_hash FROM users WHERE email = ?", (OWNER,)).fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn(PASSWORD, stored)
        token = self.client.cookies.get(S.SESSION_COOKIE)
        hashes = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
        conn.close()
        self.assertNotIn(token, hashes)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), hashes)
        self.assertEqual(self.studio.accounts.db_path.stat().st_mode & 0o777, 0o600)

    def test_signup_rules_and_login_throttle(self):
        anon = self.make_client()
        for body, code in (({"email": "not-an-email", "password": PASSWORD}, 422),
                           ({"email": "a@example.com", "password": "short"}, 422),
                           ({"email": OWNER, "password": PASSWORD}, 409)):
            self.assertEqual(anon.post("/api/auth/signup", json=body, headers=HEADERS).status_code,
                             code, body)
        for _ in range(10):
            anon.post("/api/auth/login", json={"email": OWNER, "password": "wrong-guess"},
                      headers=HEADERS)
        locked = anon.post("/api/auth/login", json={"email": OWNER, "password": PASSWORD},
                           headers=HEADERS)
        self.assertEqual(locked.status_code, 429)  # even the right password waits
        self.assertEqual(anon.post("/api/auth/login", json={"email": OWNER, "password": PASSWORD}).status_code,
                         403)  # and every POST still needs the Studio header

    def test_other_accounts_never_see_the_owner_files(self):
        user = self.make_client("dana@example.com")
        jobs = user.get("/api/jobs").json()
        self.assertIsNone(jobs["profile"])
        self.assertTrue(all(j["score"] is None and "tailored" not in j for j in jobs["jobs"]))
        meta = user.get("/api/meta").json()
        self.assertNotIn("tools", meta)
        self.assertNotIn("master", meta)
        detail = user.get("/api/jobs/proceed").json()
        self.assertNotIn("results", detail)
        self.assertNotIn("board_score", detail)
        self.assertIsNone(detail["my_score"])
        master_lines = [l for l in MASTER_KIT.splitlines() if len(l) > 30][:20]
        for path in ("/api/jobs", "/api/meta", "/api/jobs/proceed", "/api/me/profile"):
            body = user.get(path).text
            for line in master_lines:
                self.assertNotIn(line.strip("- `[V]R"), body, path)
        for method, path in (("post", "/api/jobs/fetch"), ("post", "/api/jobs/upload"),
                             ("post", "/api/tailor/proceed"), ("get", "/api/tailor/proceed"),
                             ("get", "/api/provenance/proceed"), ("get", "/api/fit/proceed"),
                             ("get", "/api/briefing/proceed"), ("get", "/api/files/proceed/resume.pdf"),
                             ("get", "/api/tasks"), ("get", "/api/profile"), ("get", "/api/template"),
                             ("post", "/api/profile"), ("post", "/api/template/validate")):
            res = getattr(user, method)(path, **({"json": {}, "headers": HEADERS} if method == "post" else {}))
            self.assertEqual(res.status_code, 403, (method, path, res.text))

    def test_each_account_is_scored_against_its_own_profile_and_keeps_it(self):
        dana = self.make_client("dana@example.com")
        lee = self.make_client("lee@example.com")
        self.client.get("/api/jobs")  # builds the job index
        before = {p.relative_to(self.root) for p in self.root.rglob("*") if ".studio" not in p.parts}
        res = dana.post("/api/me/profile", json={"filename": "resume.txt", "content_base64": b64(RESUME)},
                        headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["summary"]["mode"], "resume")
        self.assertEqual(res.json()["stored"]["text"].strip(), RESUME.strip())
        board = dana.get("/api/jobs").json()
        self.assertEqual((board["profile"]["filename"], board["profile"]["source"]),
                         ("resume.txt", "account"))
        self.assertTrue(any(j["score"] is not None for j in board["jobs"]))
        self.assertIsNone(lee.get("/api/jobs").json()["profile"])  # Dana's file is Dana's
        owner = self.client.get("/api/jobs").json()["profile"]
        self.assertEqual(owner["source"], "workspace")  # the admin falls back to master.md
        # stored with the account (in .studio/), never in the workspace files
        self.assertEqual({p.relative_to(self.root) for p in self.root.rglob("*")
                          if ".studio" not in p.parts}, before)
        restarted = S.Studio(self.root, which=fake_which(), runner=self.runner, opener=self.opener,
                             env={}, sleep=lambda s: None)
        again = TestClient(S.create_app(restarted), base_url="http://127.0.0.1:8000")
        again.post("/api/auth/login", json={"email": "dana@example.com", "password": PASSWORD},
                   headers=HEADERS)
        self.assertEqual(again.get("/api/me/profile").json()["stored"]["filename"], "resume.txt")
        bad = dana.post("/api/me/profile", json={"filename": "x.txt", "content_base64": "%%%"},
                        headers=HEADERS)
        self.assertEqual(bad.status_code, 422)

    def test_admin_job_detail_keeps_the_workspace_skill_gate(self):
        self.client.post("/api/me/profile", json={"filename": "resume.txt",
                                                  "content_base64": b64(RESUME)}, headers=HEADERS)
        detail = self.client.get("/api/jobs/proceed").json()
        jd = P.read_job_description(self.root / "target_jobs" / "proceed.md", self.root)
        self.assertEqual(detail["board_score"]["score"],
                         P.analyze(jd, P.parse_master(self.root / "master.md")).score)
        self.assertEqual(detail["profile"]["source"], "account")  # the board uses the upload

    def test_edit_remove_profile_and_delete_account(self):
        dana = self.make_client("dana@example.com")
        dana.post("/api/me/profile", json={"filename": "resume.txt", "content_base64": b64(RESUME)},
                  headers=HEADERS)
        edited = RESUME + "- Ran Kubernetes clusters for staging environments across regions\n"
        res = dana.post("/api/me/profile", json={"filename": "resume.txt", "content_base64": b64(edited)},
                        headers=HEADERS).json()
        self.assertIn("Kubernetes", res["stored"]["text"])
        self.assertIsNone(dana.post("/api/me/profile/delete", headers=HEADERS).json()["stored"])
        self.assertEqual(dana.post("/api/me/delete", json={"password": "wrong-one"},
                                   headers=HEADERS).status_code, 403)
        self.assertEqual(dana.post("/api/me/delete", json={"password": PASSWORD},
                                   headers=HEADERS).json(), {"deleted": True})
        self.assertEqual(dana.get("/api/me").status_code, 401)
        self.assertIsNone(self.studio.accounts.user_by_email("dana@example.com"))

    def test_public_stats_hold_no_personal_data(self):
        stats = self.make_client().get("/api/stats").json()
        self.assertEqual(set(stats), {"jobs", "companies", "scorable", "threshold"})
        self.assertEqual(stats["jobs"], 2)

    def test_make_admin_command(self):
        self.make_client("dana@example.com")
        self.assertEqual(S.main(["--workspace", str(self.root), "--make-admin", "dana@example.com"]), 0)
        self.assertTrue(self.studio.accounts.user_by_email("dana@example.com")["is_admin"])
        self.assertEqual(S.main(["--workspace", str(self.root), "--make-admin", "x@example.com"]), 1)


HEADER = {"name": "Test Person", "email": "test.person@example.com", "phone": "+1 416 555 0100",
          "location": "Toronto, ON", "links": ["github.com/test-person"],
          "education": [{"school": "Example University", "degree": "BSc Physics", "dates": "2024",
                         "location": "Waterloo, ON"}]}


class PlanTests(StudioCase):
    """Basic and Pro plans, Pro requests to the admin, and Pro builds from the account's own files."""

    def pro_ready(self, client):
        client.post("/api/me/profile", json={"filename": "master.md", "content_base64": b64(MASTER_KIT)},
                    headers=HEADERS)
        res = client.post("/api/me/resume-header", json=HEADER, headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_admin_emails_are_admins_from_sign_up(self):
        studio = S.Studio(self.root, which=fake_which(), runner=self.runner, opener=self.opener, env={},
                          sleep=lambda s: None, admin_emails=frozenset({"boss@example.com"}))
        client = TestClient(S.create_app(studio), base_url="http://127.0.0.1:8000")
        res = client.post("/api/auth/signup", json={"email": "Boss@Example.com", "password": PASSWORD},
                          headers=HEADERS)
        self.assertTrue(res.json()["user"]["is_admin"])
        other = client.post("/api/auth/signup", json={"email": "x@example.com", "password": PASSWORD},
                            headers=HEADERS).json()["user"]
        self.assertEqual((other["is_admin"], other["plan"], other["is_pro"]), (False, "basic", False))

    def test_admin_emails_come_from_a_file_outside_git(self):
        (self.root / ".studio").mkdir(exist_ok=True)
        (self.root / ".studio" / "admins.txt").write_text("# admins\nlead@example.com\n")
        studio = S.Studio(self.root, which=fake_which(), runner=self.runner, opener=self.opener, env={},
                          sleep=lambda s: None)
        self.assertEqual(studio.accounts.admin_emails, frozenset({"lead@example.com"}))

    def test_request_approve_and_switch_plans(self):
        dana = self.make_client("dana@example.com")
        self.assertEqual(dana.post("/api/builds/proceed", headers=HEADERS).status_code, 403)  # Basic
        req = dana.post("/api/me/pro-request", json={"note": "Applying to platform roles"},
                        headers=HEADERS).json()["pro_request"]
        self.assertEqual(req["status"], "pending")
        again = dana.post("/api/me/pro-request", json={}, headers=HEADERS).json()["pro_request"]
        self.assertEqual(again["id"], req["id"])  # one pending request at a time
        self.assertEqual(dana.get("/api/admin/overview").status_code, 403)
        overview = self.client.get("/api/admin/overview").json()
        self.assertEqual(overview["pending"], 1)
        self.assertEqual(self.client.get("/api/meta").json()["pending_requests"], 1)
        self.assertEqual(overview["requests"][0]["note"], "Applying to platform roles")
        approved = self.post(f"/api/admin/requests/{req['id']}/approve").json()
        self.assertEqual(approved["pending"], 0)
        me = dana.get("/api/me").json()
        self.assertEqual((me["user"]["plan"], me["user"]["is_pro"]), ("pro", True))
        self.assertEqual(self.post(f"/api/admin/requests/{req['id']}/decline").status_code, 409)
        user_id = me["user"]["id"]
        self.post(f"/api/admin/users/{user_id}/plan", {"plan": "basic"})
        self.assertFalse(dana.get("/api/me").json()["user"]["is_pro"])
        self.assertEqual(self.post(f"/api/admin/users/{user_id}/plan", {"plan": "gold"}).status_code, 422)
        self.assertEqual(dana.post(f"/api/admin/users/{user_id}/plan", json={"plan": "pro"},
                                   headers=HEADERS).status_code, 403)

    def test_a_pro_build_uses_only_the_accounts_own_files(self):
        dana = self.make_client("dana@example.com")
        self.studio.accounts.set_plan(dana.get("/api/me").json()["user"]["id"], "pro")
        needs = dana.get("/api/me/resume-header").json()["build"]
        self.assertEqual(needs["missing"], ["profile", "header"])
        dana.post("/api/me/profile", json={"filename": "resume.txt", "content_base64": b64(RESUME)},
                  headers=HEADERS)
        self.assertIn("master", dana.get("/api/me/resume-header").json()["build"]["missing"])
        self.assertEqual(self.pro_ready(dana)["build"], {"pro": True, "missing": [], "ready": True})
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*") if ".studio" not in p.parts)
        res = dana.post("/api/builds/proceed", json={}, headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.text)
        build = res.json()["build"]
        self.assertEqual(build["status"], "verified")
        self.assertTrue(build["has_pdf"])
        self.assertEqual(build["summary"]["gates"]["gate_4_layout"], "passed")
        self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob("*")
                                if ".studio" not in p.parts), before)  # nothing in the workspace
        tex = next(c for c in self.runner.commands if Path(c[0]).name == "pdflatex")
        self.assertNotIn(str(self.root / "template.tex"), " ".join(tex))
        pdf = dana.get("/api/builds/proceed/resume.pdf")
        self.assertEqual((pdf.status_code, pdf.headers["content-type"]), (200, "application/pdf"))
        self.assertNotIn("content-security-policy", pdf.headers)
        self.assertIn("Requirements", dana.get("/api/builds/proceed/report").text)
        self.assertEqual(dana.get("/api/jobs/proceed").json()["my_build"]["status"], "verified")
        lee = self.make_client("lee@example.com")
        self.assertEqual(lee.get("/api/builds/proceed/resume.pdf").status_code, 404)  # Dana's only
        self.assertEqual(lee.get("/api/builds").json(), {"builds": []})

    def test_low_fit_builds_need_the_override(self):
        dana = self.make_client("dana@example.com")
        self.studio.accounts.set_plan(dana.get("/api/me").json()["user"]["id"], "pro")
        self.pro_ready(dana)
        low = dana.post("/api/builds/reject", json={}, headers=HEADERS).json()["build"]
        self.assertFalse(low["has_pdf"])
        self.assertEqual(low["summary"]["verdict"], P.VERDICT_REJECT)
        forced = dana.post("/api/builds/reject", json={"override_low_fit": True}, headers=HEADERS)
        self.assertEqual(forced.json()["build"]["summary"]["verdict"], P.VERDICT_REJECT)

    def test_builds_queued_on_the_hosted_app_run_here_and_land_in_r2(self):
        from test_hosting import FakeStore
        dana = self.make_client("dana@example.com")
        uid = dana.get("/api/me").json()["user"]["id"]
        self.studio.accounts.set_plan(uid, "pro")
        self.pro_ready(dana)
        store = self.studio.accounts.store = FakeStore()
        for job_id in ("proceed", "gone"):  # what the Vercel app writes for "Build my resume"
            self.studio.accounts._run(
                "INSERT INTO builds (user_id, job_id, status, summary, created_at) "
                "VALUES (?, ?, 'queued', '{}', 0)", (uid, job_id))
        lines: list[str] = []
        self.assertEqual(self.studio.process_builds(lines.append), 1)
        self.assertEqual(self.studio.accounts.queued_builds(), [])
        self.assertIn(f"users/{uid}/builds/proceed.pdf", store.objects)
        build = dana.get("/api/jobs/proceed").json()["my_build"]
        self.assertEqual((build["status"], build["has_pdf"]), ("verified", True))
        pdf = dana.get("/api/builds/proceed/resume.pdf")
        self.assertEqual(pdf.content, store.objects[f"users/{uid}/builds/proceed.pdf"][0])
        failed = self.studio.accounts.build(uid, "gone")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("target_jobs/gone.md", failed["summary"]["reason"])

    def test_bad_headers_are_refused(self):
        dana = self.make_client("dana@example.com")
        for bad in ({**HEADER, "name": ""}, {**HEADER, "links": ["javascript:alert(1)"]},
                    {**HEADER, "links": ["https://x.example/a?b=c"]}, {**HEADER, "email": "nope"}):
            self.assertEqual(dana.post("/api/me/resume-header", json=bad, headers=HEADERS).status_code,
                             422, bad)


class JobsApiTests(StudioCase):
    def test_list_scores_filters_and_pages(self):
        data = self.client.get("/api/jobs").json()
        self.assertEqual((data["total"], data["matching"]), (2, 2))
        by_id = {job["id"]: job for job in data["jobs"]}
        self.assertEqual(by_id["proceed"]["verdict"], P.VERDICT_PROCEED)
        self.assertEqual(by_id["reject"]["verdict"], P.VERDICT_REJECT)
        self.assertEqual([j["id"] for j in data["jobs"]], ["proceed", "reject"])  # best match first
        self.assertEqual(data["facets"]["verdict"], {"low_fit": 1, "proceed": 1})
        self.assertEqual(data["profile"]["mode"], "master")
        only = self.client.get("/api/jobs", params={"q": "data scientist"}).json()
        self.assertEqual([j["id"] for j in only["jobs"]], ["reject"])
        proceed = self.client.get("/api/jobs", params={"verdict": "proceed"}).json()
        self.assertEqual([j["id"] for j in proceed["jobs"]], ["proceed"])
        page = self.client.get("/api/jobs", params={"limit": 1, "offset": 1}).json()
        self.assertEqual([j["id"] for j in page["jobs"]], ["reject"])

    def test_rows_carry_matches_gaps_and_several_levels_filter(self):
        data = self.client.get("/api/jobs").json()
        job = next(j for j in data["jobs"] if j["id"] == "proceed")
        self.assertTrue(job["matched"])
        self.assertEqual(job["requirement_count"], len(job["matched"]) + len(job["gaps"]))
        levels = sorted(data["facets"]["level"])
        both = self.client.get("/api/jobs", params={"level": ",".join(levels)}).json()
        self.assertEqual(both["matching"], 2)
        none = self.client.get("/api/jobs", params={"level": "Director and above"}).json()
        self.assertEqual(none["matching"], 0)

    def test_board_score_equals_the_pipeline_score(self):
        jd = P.read_job_description(self.root / "target_jobs" / "proceed.md", self.root)
        expected = P.analyze(jd, P.parse_master(self.root / "master.md")).score
        job = next(j for j in self.client.get("/api/jobs").json()["jobs"] if j["id"] == "proceed")
        self.assertEqual(job["score"], expected)

    def test_job_detail_and_unknown_ids(self):
        data = self.client.get("/api/jobs/proceed").json()
        self.assertEqual(data["text"], JD_PROCEED)
        self.assertFalse(data["results"]["available"])
        for bad in ("nope", "..%2Fmaster", ".hidden", "a b"):
            self.assertEqual(self.client.get(f"/api/jobs/{bad}").status_code, 404, bad)

    def test_upload_saves_a_manual_job_and_indexes_it(self):
        res = self.post("/api/jobs/upload", {"text": "## Requirements\n- Must have Python and Docker",
                                             "company": "Acme", "role": "Platform Engineer"})
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json(), {"id": "manual_acme_platform_engineer",
                                      "file": "manual_acme_platform_engineer.md"})
        text = (self.root / "target_jobs" / "manual_acme_platform_engineer.md").read_text()
        self.assertTrue(text.startswith("# Platform Engineer at Acme\n"))
        ids = [j["id"] for j in self.client.get("/api/jobs").json()["jobs"]]
        self.assertIn("manual_acme_platform_engineer", ids)
        again = self.post("/api/jobs/upload", {"text": "## Requirements\n- Must have Python and Docker",
                                               "company": "Acme", "role": "Platform Engineer"})
        self.assertEqual(again.status_code, 409)
        other = self.post("/api/jobs/upload", {"text": "## Requirements\n- Go required",
                                               "company": "Acme", "role": "Platform Engineer"})
        self.assertRegex(other.json()["file"], r"^manual_acme_platform_engineer_[0-9a-f]{8}\.md$")

    def test_upload_validation(self):
        self.assertEqual(self.post("/api/jobs/upload", {"text": "   "}).status_code, 422)
        self.assertEqual(self.post("/api/jobs/upload", {"text": "a\x00b"}).status_code, 422)
        too_big = "x" * (P.MAX_JD_BYTES + 1)
        self.assertEqual(self.post("/api/jobs/upload", {"text": too_big}).status_code, 422)


class TailorApiTests(StudioCase):
    def test_edited_engine_code_is_reloaded_before_a_run(self):
        from unittest import mock
        logged: list[str] = []
        self.assertFalse(S.refresh_engine(logged.append))  # unchanged since Studio loaded it
        saved = dict(S.ENGINE_SOURCES)
        S.ENGINE_SOURCES["pipeline"] = "0" * 64  # as if pipeline.py changed on disk
        try:
            with mock.patch.object(S.importlib, "reload") as reload:
                self.assertTrue(S.refresh_engine(logged.append))
            reload.assert_called()
            self.assertEqual(logged, ["Reloaded pipeline.py: changed on disk since Studio "
                                      "loaded it"])
            self.assertEqual(S.ENGINE_SOURCES["pipeline"], saved["pipeline"])
        finally:
            S.ENGINE_SOURCES.update(saved)

    def test_proceed_streams_compiles_and_exposes_every_result(self):
        lines, done = self.tailor("proceed")
        self.assertEqual(lines[0], "$ python3 pipeline.py target_jobs/proceed.md --debug-proof")
        self.assertTrue(any(l.startswith("[pdflatex #") and l.endswith(
            "compiling example-co_platform-engineer_resume.tex") for l in lines), lines)
        self.assertTrue(any(line.startswith("[pdfinfo] 1 page") for line in lines))
        self.assertEqual((done["status"], done["exit_code"], done["outcome"]), ("done", 0, "verified"))
        results = self.client.get("/api/tailor/proceed").json()
        self.assertEqual(results["verdict"], P.VERDICT_PROCEED)
        self.assertEqual(done["result"]["fit_score"], results["fit_score"])
        gates = results["resume"]["build_gates"]
        self.assertEqual([g["status"] for g in gates.values()], ["passed"] * 4)
        self.assertEqual(results["pdf"]["state"], "verified")
        pdf = self.client.get(results["pdf"]["url"])
        self.assertEqual((pdf.status_code, pdf.headers["content-type"]), (200, "application/pdf"))
        self.assertNotIn("content-security-policy", pdf.headers)
        self.assertTrue(pdf.headers["content-disposition"].startswith("inline"))

        proof = self.client.get("/api/provenance/proceed").json()
        self.assertFalse(proof["stale"])
        self.assertEqual(len(proof["master_lines"]), len(MASTER_KIT.split("\n")))
        claims = proof["provenance"]["claims"]
        self.assertTrue(claims and all(c["traced"] for c in claims))
        bullet = next(c for c in claims if c["type"] == "bullet")
        for item in bullet["evidence"]:
            line = proof["master_lines"][item["line"] - 1]
            self.assertEqual(line["n"], item["line"])
            self.assertIn(line["tag"], ("V", "R", "TABLE"))

        self.assertIsNotNone(results["fit"]["profile_fit"])
        self.assertLessEqual(results["fit"]["resume_fit"], results["fit"]["profile_fit"])
        fit = self.client.get("/api/fit/proceed").json()
        self.assertEqual(fit["profile_fit"], results["fit"]["profile_fit"])
        self.assertTrue(fit["requirements"] and "terms" in fit["requirements"][0])
        self.assertEqual(self.client.get("/api/fit/reject").status_code, 404)

        briefing = self.client.get("/api/briefing/proceed").json()
        self.assertEqual(len(briefing["briefing"]["star_prompts"]), 3)
        self.assertTrue(briefing["markdown"].startswith("# Interview briefing"))
        self.assertIsNone(briefing["gap"])
        md = self.client.get("/api/files/proceed/briefing.md")
        self.assertEqual(md.text, briefing["markdown"])
        listed = next(j for j in self.client.get("/api/jobs").json()["jobs"] if j["id"] == "proceed")
        self.assertEqual(listed["tailored"]["resume_status"], "verified")

    def test_low_fit_writes_a_gap_report_and_no_pdf(self):
        _lines, done = self.tailor("reject")
        self.assertEqual((done["status"], done["exit_code"], done["outcome"]), ("done", 3, "low fit"))
        results = self.client.get("/api/tailor/reject").json()
        self.assertEqual(results["resume"]["status"], "skipped_low_fit")
        self.assertIsNone(results["pdf"])
        self.assertTrue(results["files"]["gap_analysis.json"])
        gap = self.client.get("/api/briefing/reject").json()["gap"]
        self.assertIn("Kubernetes", [m["skill"] for m in gap["missing_critical_skills"]])
        self.assertIsNone(self.client.get("/api/briefing/reject").json()["briefing"])
        self.assertEqual(self.client.get("/api/files/reject/resume.pdf").status_code, 404)
        self.assertEqual(self.client.get("/api/provenance/reject").status_code, 404)

    def test_options_reach_the_pipeline(self):
        lines, done = self.tailor("reject", override_low_fit=True, analyze_gap=True)
        self.assertIn("--override-low-fit", lines[0])
        self.assertEqual(done["exit_code"], P.EXIT_LOW_FIT)  # the verdict never changes
        resume = self.client.get("/api/tailor/reject").json()["resume"]
        self.assertTrue(resume["override_low_fit"])
        self.assertEqual(resume["status"], "verified")

    def test_compile_failure_reports_gate_1_and_the_log(self):
        self.studio.runner = FakeTex(fail=True)
        _lines, done = self.tailor("proceed")
        self.assertEqual((done["status"], done["exit_code"]), ("failed", P.EXIT_FAILED))
        results = self.client.get("/api/tailor/proceed").json()
        self.assertEqual(results["resume"]["build_gates"]["gate_1_syntax"]["status"], "failed")
        self.assertTrue(results["log"]["fatal"])
        self.assertIsNone(results["pdf"])

    def test_unknown_job_and_file(self):
        self.assertEqual(self.post("/api/tailor/nope").status_code, 404)
        self.tailor("proceed")
        self.assertEqual(self.client.get("/api/files/proceed/../../master.md").status_code, 404)
        self.assertEqual(self.client.get("/api/files/proceed/analysis.json").status_code, 404)

    def test_provenance_turns_stale_after_a_profile_edit(self):
        self.tailor("proceed")
        profile = self.client.get("/api/profile").json()
        edited = profile["text"].replace("## 2. Experience", "## 2. Experience\n\nIntro line.")
        self.assertEqual(self.post("/api/profile", {"text": edited,
                                                    "base_sha256": profile["sha256"]}).status_code, 200)
        self.assertTrue(self.client.get("/api/provenance/proceed").json()["stale"])


class BusyTests(StudioCase):
    def runner_factory(self):
        self.release = threading.Event()
        return BlockingTex(self.release)

    def test_one_task_at_a_time_and_no_saves_while_running(self):
        first = self.post("/api/tailor/proceed")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.client.get("/api/meta").json()["active_task"]["status"], "running")
        self.assertEqual(self.post("/api/tailor/reject").status_code, 409)
        self.assertEqual(self.post("/api/jobs/fetch", {"source": "greenhouse"}).status_code, 409)
        profile = self.client.get("/api/profile").json()
        save = self.post("/api/profile", {"text": profile["text"], "base_sha256": profile["sha256"]})
        self.assertEqual(save.status_code, 409)
        detail = self.client.get("/api/jobs/proceed").json()
        self.assertEqual(detail["active_task"]["id"], first.json()["id"])
        self.release.set()
        _lines, done = self.follow(first.json())
        self.assertEqual(done["exit_code"], 0)
        second = self.post("/api/tailor/reject")
        self.assertEqual(second.status_code, 202)
        self.follow(second.json())  # let it finish before the workspace is removed


class FetchApiTests(StudioCase):
    def setUp(self) -> None:
        super().setUp()
        config = F.default_config()
        config["companies"] = [{"name": "Example Corp", "enabled": True,
                                "greenhouse_board_token": "examplecorp"}]
        (self.root / "config").mkdir()
        (self.root / "config" / "companies.json").write_text(json.dumps(config))

    def test_fetch_streams_progress_writes_jobs_and_reindexes(self):
        res = self.post("/api/jobs/fetch", {"source": "greenhouse"})
        self.assertEqual(res.status_code, 202, res.text)
        lines, done = self.follow(res.json())
        self.assertEqual(lines[0], "$ python3 fetch_jobs.py --source greenhouse")
        self.assertIn("[http] GET boards-api.greenhouse.io", lines)
        self.assertEqual((done["status"], done["exit_code"]), ("done", 0))
        self.assertEqual(done["result"]["new_jobs"], 2)  # DevOps Engineer and a partial SRE
        ids = [j["id"] for j in self.client.get("/api/jobs").json()["jobs"]]
        self.assertIn("greenhouse_examplecorp_devops_engineer", ids)
        self.assertIn("greenhouse_examplecorp_site_reliability_engineer", ids)

    def test_dry_run_writes_nothing(self):
        before = sorted(p.name for p in (self.root / "target_jobs").iterdir())
        _lines, done = self.follow(self.post("/api/jobs/fetch", {"source": "greenhouse",
                                                                 "dry_run": True}).json())
        self.assertEqual(done["result"], {"dry_run": True})
        self.assertEqual(sorted(p.name for p in (self.root / "target_jobs").iterdir()), before)

    def test_query_and_location_map_to_fetch_flags_and_keys_never_leak(self):
        self.studio.env = {"JOOBLE_API_KEY": JOOBLE_KEY}
        lines, done = self.follow(self.post("/api/jobs/fetch", {
            "source": "jooble", "query": "Platform Engineer", "location": "Toronto",
            "dry_run": True}).json())
        self.assertEqual(lines[0], "$ python3 fetch_jobs.py --source jooble --keyword "
                                   "Platform Engineer --location Toronto --dry-run")
        self.assertIn("[http] POST jooble.org", lines)
        self.assertEqual(done["exit_code"], 0)
        task = self.client.get(f"/api/tasks/{done['id']}").json()
        self.assertNotIn(JOOBLE_KEY, json.dumps(task))

    def test_paid_and_unknown_sources_are_guarded(self):
        res = self.post("/api/jobs/fetch", {"source": "jobright"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("billable", res.json()["detail"])
        self.assertEqual(self.post("/api/jobs/fetch", {"source": "linkedin"}).status_code, 422)
        self.assertEqual(self.post("/api/jobs/fetch", {"source": "free", "limit": 0}).status_code, 422)


class ProfileApiTests(StudioCase):
    def test_read_reports_tags_entries_and_skills(self):
        data = self.client.get("/api/profile").json()
        self.assertEqual(data["file"], "master.md")
        self.assertEqual(data["text"], MASTER_KIT)
        self.assertTrue(data["ok"])
        summary = data["summary"]
        self.assertGreater(summary["tags"]["[V]"], 5)
        self.assertIn("Kubernetes", summary["no_skills"])
        self.assertIn("Python", summary["skills"])
        self.assertIn("2.1", [e["id"] for e in summary["entries"]])

    def test_validate_shows_what_an_edit_changes(self):
        text = MASTER_KIT.replace("| Kubernetes and Terraform | `[NO]` |",
                              "| Kubernetes and Terraform | `[NO]` |\n| Docker | `[NO]` |")
        check = self.post("/api/profile/validate", {"text": text}).json()
        self.assertTrue(check["ok"])
        self.assertEqual(check["summary"]["changes"]["newly_no"], ["Docker"])
        self.assertIn("Docker", check["summary"]["changes"]["skills_removed"])
        self.assertEqual((self.root / "master.md").read_text(), MASTER_KIT)  # validation never writes

    def test_save_is_guarded_backed_up_and_rescored(self):
        before = next(j for j in self.client.get("/api/jobs").json()["jobs"] if j["id"] == "proceed")
        profile = self.client.get("/api/profile").json()
        stale = self.post("/api/profile", {"text": MASTER_KIT, "base_sha256": "0" * 64})
        self.assertEqual(stale.status_code, 409)
        empty = self.post("/api/profile", {"text": "# Nothing tagged\n",
                                           "base_sha256": profile["sha256"]})
        self.assertEqual(empty.status_code, 422)
        self.assertEqual((self.root / "master.md").read_text(), MASTER_KIT)
        weaker = MASTER_KIT.replace("| Python | Production | Acme tooling, all projects |\n", "")
        weaker = weaker.replace("- `[V]` Built a Python tool", "- `[OLD]` Built a Python tool")
        saved = self.post("/api/profile", {"text": weaker, "base_sha256": profile["sha256"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((self.root / "master.md").read_text(), weaker)
        self.assertIn("Python", saved.json()["changes"]["skills_removed"])
        backups = list((self.root / ".studio" / "backups").glob("master.md.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), MASTER_KIT)
        after = next(j for j in self.client.get("/api/jobs").json()["jobs"] if j["id"] == "proceed")
        self.assertLess(after["score"], before["score"])

    def test_resume_kit_is_validated_before_saving(self):
        profile = self.client.get("/api/profile").json()
        self.assertEqual(profile["summary"]["resume_kit"]["bullets"], 5)
        broken = MASTER_KIT.replace("| A02 |", "| A01 |")
        res = self.post("/api/profile", {"text": broken, "base_sha256": profile["sha256"]})
        self.assertEqual(res.status_code, 422)
        self.assertIn("duplicate bullet ID A01", res.json()["detail"])
        self.assertEqual((self.root / "master.md").read_text(), MASTER_KIT)
        check = self.post("/api/profile/validate", {"text": MASTER}).json()
        self.assertTrue(check["ok"])  # saving without a kit is allowed, with a warning
        self.assertIn("No '## 9. Resume kit' section: tailoring refuses to run until one is "
                      "added", check["warnings"])

    def test_backups_are_bounded(self):
        for i in range(S.BACKUPS_KEPT + 3):
            current = self.client.get("/api/profile").json()
            res = self.post("/api/profile", {"text": MASTER + f"\nNote {i}\n",
                                             "base_sha256": current["sha256"]})
            self.assertEqual(res.status_code, 200)
        backups = list((self.root / ".studio" / "backups").glob("master.md.*.bak"))
        self.assertEqual(len(backups), S.BACKUPS_KEPT)

    def test_template_is_validated_before_saving(self):
        data = self.client.get("/api/template").json()
        self.assertEqual(data["summary"]["engine"], "pdflatex")
        self.assertIn("Experience", [s["title"] for s in data["summary"]["sections"]])
        broken = self.post("/api/template", {"text": "\\documentclass{article}\n",
                                             "base_sha256": data["sha256"]})
        self.assertEqual(broken.status_code, 422)
        self.assertEqual((self.root / "template.tex").read_text(), TEMPLATE)
        edited = TEMPLATE.replace("Synthetic test template", "Synthetic test template v2")
        ok = self.post("/api/template", {"text": edited, "base_sha256": data["sha256"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertIn("v2", (self.root / "template.tex").read_text())


if __name__ == "__main__":
    unittest.main()
