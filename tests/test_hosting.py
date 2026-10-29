"""Hosting layer: the R2 client (storage.py), accounts kept in R2, the Neon schema and the
Vercel engine function (api/engine.py). Offline: R2 and the index URL are fakes."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fixtures import MASTER, MASTER_KIT, ROOT, make_workspace

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))
import accounts as A  # noqa: E402
import engine as E  # noqa: E402
import jobboard  # noqa: E402
import storage  # noqa: E402

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_DATE = dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc)
SECRET = "engine-test-secret"
RESUME = "\n".join(["Test Person", "Experience",
                    "- Built Python services on AWS with Docker and Terraform for platform teams",
                    "- Wrote CI/CD pipelines in GitHub Actions and monitored them with Prometheus",
                    "- Automated Kubernetes deployments and wrote SQL reports for operations staff",
                    "Skills", "Python, SQL, Docker, Kubernetes, AWS, Terraform, Linux, Git"])


class FakeStore:
    """An in-memory bucket with storage.R2's methods."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (bytes(data), content_type)

    def get(self, key):
        if key not in self.objects:
            raise storage.StorageError(f"R2 GET {key}: HTTP 404", 404)
        return self.objects[key][0]

    def delete(self, key):
        self.objects.pop(key, None)

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_prefix(self, prefix):
        keys = self.list(prefix)
        for key in keys:
            self.delete(key)
        return len(keys)


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RecordingOpener:
    """urlopen stand-in: answers from `routes` (method, path) and records each request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        path = urllib.request.urlparse(request.full_url).path
        answer = self.routes.get((request.get_method(), path))
        if answer is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {},
                                         io.BytesIO(b"<Error>secret detail</Error>"))
        return FakeResponse(answer)


class SignatureTests(unittest.TestCase):
    """The examples published in the AWS Signature Version 4 documentation for S3."""

    def test_header_signature_matches_the_aws_get_object_example(self):
        headers = storage.sign_headers(
            "GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
            hashlib.sha256(b"").hexdigest(), AWS_KEY, AWS_SECRET, AWS_DATE, region="us-east-1")
        self.assertEqual(headers["Authorization"],
                         "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/"
                         "aws4_request, SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                         "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_presigned_url_matches_the_aws_example(self):
        url = storage.presign_url("GET", "https://examplebucket.s3.amazonaws.com/test.txt",
                                  AWS_KEY, AWS_SECRET, AWS_DATE, 86400, region="us-east-1")
        self.assertTrue(url.endswith("&X-Amz-Signature="
                                     "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))
        self.assertIn("X-Amz-Expires=86400", url)
        with self.assertRaises(ValueError):
            storage.presign_url("GET", "https://x/y", AWS_KEY, AWS_SECRET, AWS_DATE, 8 * 24 * 3600)


class R2ClientTests(unittest.TestCase):
    def make(self, routes):
        opener = RecordingOpener(routes)
        r2 = storage.R2("acct", "key-id", "very-secret-key", "vouch", opener=opener,
                        clock=lambda: AWS_DATE)
        return r2, opener

    def test_requests_are_path_style_signed_and_carry_the_body(self):
        r2, opener = self.make({("PUT", "/vouch/users/1/profile/a.md"): b"",
                                ("GET", "/vouch/users/1/profile/a.md"): b"hello"})
        r2.put("users/1/profile/a.md", b"hello", "text/markdown")
        self.assertEqual(r2.get("users/1/profile/a.md"), b"hello")
        put = opener.requests[0]
        self.assertEqual(put.full_url, "https://acct.r2.cloudflarestorage.com/vouch/users/1/profile/a.md")
        self.assertEqual(put.data, b"hello")
        self.assertEqual(put.get_header("X-amz-content-sha256"), hashlib.sha256(b"hello").hexdigest())
        self.assertIn("/auto/s3/aws4_request", put.get_header("Authorization"))
        self.assertNotIn("very-secret-key", repr(r2) + put.get_header("Authorization"))

    def test_errors_name_the_status_but_never_the_body(self):
        r2, _ = self.make({})
        with self.assertRaises(storage.StorageError) as caught:
            r2.get("users/1/missing")
        self.assertEqual(caught.exception.status, 404)
        self.assertNotIn("secret detail", str(caught.exception))
        r2.delete("users/1/missing")  # already gone is fine

    def test_list_follows_continuation_and_delete_prefix_removes_each_key(self):
        page = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                '<IsTruncated>false</IsTruncated><Contents><Key>users/7/a</Key></Contents>'
                '<Contents><Key>users/7/b c</Key></Contents></ListBucketResult>').encode()
        r2, opener = self.make({("GET", "/vouch/"): page, ("DELETE", "/vouch/users/7/a"): b"",
                                ("DELETE", "/vouch/users/7/b c"): b""})
        self.assertEqual(r2.delete_prefix("users/7/"), 2)
        self.assertIn("list-type=2&prefix=users%2F7%2F", opener.requests[0].full_url)
        with self.assertRaises(ValueError):
            r2.delete_prefix("users/7")  # never a bare prefix that could match users/70

    def test_from_env_needs_all_four_settings_or_none(self):
        self.assertIsNone(storage.from_env({}))
        with self.assertRaises(storage.StorageError) as caught:
            storage.from_env({"R2_ACCOUNT_ID": "a", "R2_BUCKET": "b"})
        self.assertIn("R2_ACCESS_KEY_ID", str(caught.exception))
        full = {name: "x" for name in storage.ENV_KEYS}
        self.assertIsInstance(storage.from_env(full), storage.R2)


class AccountsInR2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = FakeStore()
        self.acc = A.Accounts(Path(self._tmp.name) / "a.db", store=self.store)
        self.user = self.acc.create_user("dana@example.com", "password-123")

    def tearDown(self):
        self._tmp.cleanup()

    def test_profiles_and_pdfs_live_in_r2_and_leave_with_the_account(self):
        uid = self.user["id"]
        self.acc.save_profile(uid, "../r.md", b"first")
        self.acc.save_profile(uid, "r.pdf", b"%PDF-second")
        sha = hashlib.sha256(b"%PDF-second").hexdigest()
        self.assertEqual(list(self.store.objects), [f"users/{uid}/profile/{sha}.pdf"])
        self.assertEqual(self.store.objects[f"users/{uid}/profile/{sha}.pdf"][1], "application/pdf")
        row = self.acc._run("SELECT content, object_key FROM profiles WHERE user_id = ?", (uid,))[0]
        self.assertEqual(bytes(row["content"]), b"")
        self.assertEqual(self.acc.profile(uid)["content"], b"%PDF-second")
        self.acc.save_build(uid, "job-1", "Role", "Co", "verified", {}, b"%PDF-build", "# r")
        self.assertIsNone(self.acc._run("SELECT pdf FROM builds")[0]["pdf"])
        light = self.acc.build(uid, "job-1", with_pdf=False)
        self.assertEqual((light["pdf"], light["has_pdf"]), (None, True))
        self.assertEqual(self.acc.build(uid, "job-1")["pdf"], b"%PDF-build")
        self.assertTrue(self.acc.builds(uid)[0]["has_pdf"])
        self.acc.save_build(uid, "job-1", "Role", "Co", "failed", {}, None, None)
        self.assertNotIn(f"users/{uid}/builds/job-1.pdf", self.store.objects)
        self.acc.delete_profile(uid)
        self.assertEqual(self.store.objects, {})
        self.acc.save_profile(uid, "r.md", b"again")
        self.acc.delete_user(uid)
        self.assertEqual(self.store.objects, {})

    def test_an_r2_profile_without_r2_configured_is_a_clear_error(self):
        self.acc.save_profile(self.user["id"], "r.md", b"kept in r2")
        local = A.Accounts(self.acc.db_path)
        with self.assertRaises(A.AccountError) as caught:
            local.profile(self.user["id"])
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("R2_BUCKET", str(caught.exception))

    def test_queued_builds_are_listed_and_a_failure_keeps_the_earlier_pdf(self):
        uid = self.user["id"]
        self.acc.save_build(uid, "job-1", "Role", "Co", "verified", {}, b"%PDF-old", None)
        self.acc._run("UPDATE builds SET status = ?, summary = ? WHERE user_id = ?",
                      (A.BUILD_QUEUED, json.dumps({"override_low_fit": True}), uid))
        self.assertEqual(self.acc.queued_builds(),
                         [{"user_id": uid, "job_id": "job-1", "summary": {"override_low_fit": True}}])
        self.acc.fail_build(uid, "job-1", "No job file")
        build = self.acc.build(uid, "job-1")
        self.assertEqual((build["status"], build["summary"]["reason"], build["pdf"]),
                         ("failed", "No job file", b"%PDF-old"))
        self.assertEqual(self.acc.queued_builds(), [])


class SchemaTests(unittest.TestCase):
    """db/schema.sql (Neon) must define every table and column accounts.py uses."""

    def test_every_accounts_table_and_column_is_in_the_neon_schema(self):
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        tables = dict(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);",
                                 A._TABLES.format(**A.POSTGRES_TYPES), re.S))
        self.assertEqual(set(tables), {"users", "sessions", "profiles", "resume_headers",
                                       "pro_requests", "builds"})
        for table, body in tables.items():
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", schema)
            block = schema.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split("\n);", 1)[0]
            for line in body.strip().splitlines():
                self.assertIn(line.strip().rstrip(","), block, f"{table}: {line.strip()}")
        for table, column, definition in A.MIGRATIONS:
            self.assertIn(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition};",
                          schema)


def build_index(root: Path) -> dict:
    full = jobboard.build_index(root)
    return {"version": full["version"], "generated": full["generated"],
            "jobs": [{"id": j["id"], "requirements": j["requirements"]} for j in full["jobs"]]}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name), master=MASTER)
        self.index = build_index(self.root)
        self.index_bytes = json.dumps(self.index).encode()
        self.sha = hashlib.sha256(self.index_bytes).hexdigest()
        self.opener = RecordingOpener({("GET", "/vouch/board/index.json"): self.index_bytes})
        E._index.update(sha256=None, jobs=None)

    def tearDown(self):
        self._tmp.cleanup()

    def score(self, filename: str, data: bytes = b"", text: str | None = None, sha: str | None = None):
        body = {"action": "score", "filename": filename,
                "content_base64": base64.b64encode(data).decode(),
                "index": {"url": "https://acct.r2.cloudflarestorage.com/vouch/board/index.json",
                          "sha256": sha or self.sha}}
        if text is not None:
            body["text"] = text
        return E.score(body, self.opener)

    def test_scores_equal_the_local_board_and_the_index_is_cached(self):
        result = self.score("master.md", MASTER.encode())
        profile = jobboard.load_profile("master.md", MASTER.encode())
        full = jobboard.build_index(self.root)["jobs"]
        self.assertEqual(result["scores"], jobboard.score_jobs(profile, full))
        self.assertEqual((result["profile"]["mode"], result["profile"]["is_master"],
                          result["profile"]["has_kit"]), ("master", True, False))
        self.assertEqual(result["threshold"], 65)
        self.score("resume.txt", RESUME.encode())
        self.assertEqual(len(self.opener.requests), 1)  # fetched once per index version

    def test_pdf_text_from_the_caller_scores_like_a_text_upload(self):
        as_text = self.score("resume.txt", RESUME.encode())
        as_pdf = self.score("resume.pdf", text=RESUME)
        self.assertEqual(as_pdf["scores"], as_text["scores"])
        self.assertEqual(as_pdf["profile"]["filename"], "resume.pdf")

    def test_a_kit_is_detected_and_bad_input_is_refused(self):
        self.assertTrue(self.score("master.md", MASTER_KIT.encode())["profile"]["has_kit"])
        for filename, data in (("cv.docx", RESUME.encode()), ("cv.txt", b"too short")):
            with self.assertRaises(E.EngineError) as caught:
                self.score(filename, data)
            self.assertEqual(caught.exception.status, 422)
        E._index.update(sha256=None, jobs=None)
        with self.assertRaises(E.EngineError) as caught:
            self.score("resume.txt", RESUME.encode(), sha="0" * 64)
        self.assertEqual(caught.exception.status, 502)  # the index was changed or corrupted

    def test_headers_are_validated_by_user_template(self):
        self.assertEqual(E.handle({"action": "header", "header": {"name": " Dana "}})["header"]["name"],
                         "Dana")
        with self.assertRaises(E.EngineError) as caught:
            E.handle({"action": "header", "header": {"name": "Dana", "links": ["javascript:x"]}})
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(E.handle({"action": "constants"})["levels"], list(jobboard.LEVELS))

    def test_the_http_handler_requires_the_secret(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), E.handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api/engine"

        def post(body: dict, token: str | None) -> tuple[int, dict]:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=10) as res:
                    return res.status, json.loads(res.read())
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())

        try:
            with mock.patch.dict(os.environ, {"ENGINE_SECRET": SECRET}):
                self.assertEqual(post({"action": "constants"}, None)[0], 401)
                self.assertEqual(post({"action": "constants"}, "wrong")[0], 401)
                status, data = post({"action": "constants"}, SECRET)
                self.assertEqual((status, data["threshold"]), (200, 65))
                self.assertEqual(post({"action": "nope"}, SECRET)[0], 400)
            with mock.patch.dict(os.environ, {"ENGINE_SECRET": ""}):
                self.assertEqual(post({"action": "constants"}, "")[0], 401)  # unset: refuse all
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
