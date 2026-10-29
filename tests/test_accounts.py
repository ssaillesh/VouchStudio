"""accounts.py on SQLite, and on Postgres (Neon) when VOUCH_TEST_POSTGRES_URL is set."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import ROOT

sys.path.insert(0, str(ROOT))
import accounts as A  # noqa: E402

PG_URL = os.environ.get("VOUCH_TEST_POSTGRES_URL")


class AccountsContract:
    """The same behaviour on every backend."""

    def make(self) -> A.Accounts:
        raise NotImplementedError

    def test_users_sessions_profiles_requests_and_builds(self):
        acc = self.make()
        dana = acc.create_user("Dana@Example.com", "password-123", "Dana")
        boss = acc.create_user("boss@example.com", "password-123")
        self.assertTrue(boss["is_admin"])
        self.assertEqual((dana["plan"], dana["is_pro"], dana["is_admin"]), ("basic", False, False))
        with self.assertRaises(A.AccountError) as dup:
            acc.create_user("dana@example.com", "password-123")
        self.assertEqual(dup.exception.status, 409)
        self.assertIsNone(acc.authenticate("dana@example.com", "wrong-password"))
        token = acc.create_session(dana["id"])
        self.assertEqual(acc.user_for_session(token)["email"], "dana@example.com")
        acc.save_profile(dana["id"], "r.txt", b"bytes \x00 kept exactly")
        self.assertEqual(acc.profile(dana["id"])["content"], b"bytes \x00 kept exactly")
        acc.save_header(dana["id"], {"name": "Dana"})
        self.assertEqual(acc.header(dana["id"]), {"name": "Dana"})
        req = acc.request_pro(dana["id"], "please")
        self.assertEqual(acc.request_pro(dana["id"])["id"], req["id"])
        acc.decide_request(req["id"], True, boss["id"])
        self.assertTrue(acc.user_by_id(dana["id"])["is_pro"])
        with self.assertRaises(A.AccountError):
            acc.request_pro(dana["id"])  # already Pro
        acc.save_build(dana["id"], "job", "Role", "Co", "verified", {"fit": 7}, b"%PDF-1", "# r")
        acc.save_build(dana["id"], "job", "Role", "Co", "verified", {"fit": 8}, b"%PDF-2", "# r2")
        builds = acc.builds(dana["id"])
        self.assertEqual((len(builds), builds[0]["summary"], builds[0]["has_pdf"]), (1, {"fit": 8}, True))
        self.assertEqual(acc.build(dana["id"], "job")["pdf"], b"%PDF-2")
        listed = {u["email"]: u for u in acc.list_users()}
        self.assertEqual((listed["dana@example.com"]["builds"], listed["dana@example.com"]["has_profile"]),
                         (1, True))
        acc.delete_user(dana["id"])
        self.assertIsNone(acc.profile(dana["id"]))
        self.assertEqual(acc.builds(dana["id"]), [])
        self.assertIsNone(acc.user_for_session(token))


class SqliteAccountsTests(AccountsContract, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self):
        return A.Accounts(Path(self._tmp.name) / "a.db", admin_emails=["boss@example.com"])

    def test_old_databases_gain_the_plan_column(self):
        import sqlite3
        path = Path(self._tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
                     "name TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL, "
                     "is_admin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
        conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES ('a@b.co', 'x', 1)")
        conn.commit()
        conn.close()
        self.assertEqual(A.Accounts(path).user_by_email("a@b.co")["plan"], "basic")

    def test_admin_emails_from_env_and_file(self):
        path = Path(self._tmp.name) / "admins.txt"
        path.write_text("# comment\nOne@Example.com\n")
        self.assertEqual(A.read_admin_emails(path, {"VOUCH_ADMIN_EMAILS": "two@example.com, "}),
                         frozenset({"one@example.com", "two@example.com"}))


@unittest.skipUnless(PG_URL, "set VOUCH_TEST_POSTGRES_URL to run the Postgres (Neon) checks")
class PostgresAccountsTests(AccountsContract, unittest.TestCase):
    def make(self):
        acc = A.Accounts(PG_URL, admin_emails=["boss@example.com"])
        for table in ("builds", "pro_requests", "resume_headers", "profiles", "sessions", "users"):
            acc._run(f"DELETE FROM {table}")
        return acc


if __name__ == "__main__":
    unittest.main()
