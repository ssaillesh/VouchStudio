"""Per-account resume templates (user_template.py): built from one account's own kit and header."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import MASTER_KIT, ROOT, load_pipeline

P = load_pipeline()
sys.path.insert(0, str(ROOT))
import user_template as U  # noqa: E402

HEADER = {"name": "Test Person", "email": "test.person@example.com", "phone": "+1 416 555 0100",
          "location": "Toronto, ON", "links": ["github.com/test-person"],
          "education": [{"school": "Example University", "degree": "BSc Physics", "dates": "2024",
                         "location": "Waterloo, ON"}]}


class UserTemplateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "master.md").write_text(MASTER_KIT, encoding="utf-8")
        self.kit = P.load_kit(P.parse_master(self.root / "master.md"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_base_carries_no_personal_details(self):
        base = U.BASE.read_text(encoding="utf-8")
        self.assertTrue(base.rstrip().endswith("\\begin{document}"))
        self.assertNotRegex(base, r"[\w.+-]+@[\w-]+\.[\w.-]+")  # no email address
        self.assertNotRegex(base, r"\d{3}[ .-]\d{4}")  # no phone number
        self.assertNotIn("linkedin.com/in", base)

    def test_template_holds_the_accounts_header_kit_and_education(self):
        tex = U.build_template(U.clean_header(HEADER), self.kit)
        self.assertIn("\\textbf{\\Huge \\scshape Test Person}", tex)
        self.assertIn("\\href{https://github.com/test-person}{\\underline{github.com/test-person}}", tex)
        self.assertIn("{Acme Corp}{Jan 2024 -- Dec 2024}", tex)
        self.assertIn("\\textbf{Gamma App} $|$ \\emph{FastAPI, PostgreSQL, Grafana}", tex)
        self.assertIn("{Example University}{2024}", tex)
        path = self.root / "template.tex"
        path.write_text(tex, encoding="utf-8")
        titles = [s.title for s in P.parse_template(path).sections]
        self.assertEqual(titles, ["Summary", "Technical Skills", "Experience", "Projects",
                                  "Education", "Additional"])

    def test_header_text_is_escaped_and_links_are_checked(self):
        sneaky = U.clean_header({**HEADER, "name": "A \\input{/etc/passwd} & B_%"})
        tex = U.header_latex(sneaky)
        self.assertNotIn("\\input{", tex)
        self.assertIn("\\&", tex)
        for bad in ("javascript:alert(1)", "https://x.example/a?b=c", "https://x.example/#frag",
                    "https://x.example/a}{\\input"):
            with self.assertRaises(U.TemplateInputError, msg=bad):
                U.clean_header({**HEADER, "links": [bad]})
        with self.assertRaises(U.TemplateInputError):
            U.clean_header({**HEADER, "name": " "})
        with self.assertRaises(U.TemplateInputError):
            U.clean_header({**HEADER, "email": "a}@b.c"})
        self.assertEqual(U.clean_header({**HEADER, "links": ["example.com/me"]})["links"],
                         ["https://example.com/me"])


if __name__ == "__main__":
    unittest.main()
