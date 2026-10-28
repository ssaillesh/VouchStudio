"""HTML-to-Markdown conversion and record validation for fetch_jobs.py."""

from __future__ import annotations

import html
import unittest

from fetch_helpers import fixture, load_fetch_jobs

F = load_fetch_jobs()


class HtmlToMarkdownTests(unittest.TestCase):
    def test_paragraphs_and_headings(self):
        md = F.html_to_markdown("<h2>About</h2><p>One.</p><p>Two.</p><h4>Deep</h4>")
        self.assertEqual(md, "#### About\n\nOne.\n\nTwo.\n\n###### Deep")

    def test_bold_paragraph_becomes_pipeline_heading(self):
        md = F.html_to_markdown("<p><strong> Requirements </strong></p><ul><li>Python</li></ul>")
        self.assertEqual(md, "**Requirements**\n\n- Python")

    def test_nested_and_ordered_lists(self):
        md = F.html_to_markdown("<ul><li>A<ul><li>A1</li><li>A2</li></ul></li><li>B</li></ul>"
                                "<ol><li>First</li><li>Second</li></ol>")
        self.assertEqual(md, "- A\n  - A1\n  - A2\n- B\n\n1. First\n2. Second")

    def test_unbalanced_bold_markers(self):
        md = F.html_to_markdown("<p><b>Bold<br>still bold</b> plain</p>")  # must not raise
        self.assertIn("Bold", md)
        self.assertIn("still bold", md)
        for line in md.splitlines():
            self.assertEqual(line.count("**") % 2, 0, line)
        self.assertEqual(F._clean_inline("a ** b"), "a b")
        self.assertEqual(F._clean_inline("**"), "")

    def test_entities_and_whitespace(self):
        md = F.html_to_markdown("<p>R&amp;D&nbsp;&nbsp;team \n\n  &lt;fast&gt;   &#8212; ok</p>")
        self.assertEqual(md, "R&D team <fast> — ok")

    def test_script_style_and_navigation_removed(self):
        md = F.html_to_markdown("<nav>Home | Jobs</nav><style>p{}</style><p>Keep</p>"
                                "<script>alert('x')</script><footer>Legal</footer>")
        self.assertEqual(md, "Keep")

    def test_links_are_safe(self):
        md = F.html_to_markdown('<p><a href="https://ok.example/a">ok</a> '
                                '<a href="javascript:alert(1)">bad</a> '
                                '<a href="data:text/html,x">data</a> <a href="/rel">rel</a></p>')
        self.assertEqual(md, "[ok](https://ok.example/a) bad data rel")
        self.assertNotIn("javascript", md)

    def test_malicious_markup_never_survives(self):
        md = F.html_to_markdown('<img src=x onerror="steal()"><p onclick="x()">Text</p>'
                                '<iframe src="https://evil.example"></iframe>')
        self.assertEqual(md, "Text")

    def test_empty_inputs(self):
        self.assertEqual(F.html_to_markdown(""), "")
        self.assertEqual(F.html_to_markdown("<div> </div>"), "")
        self.assertEqual(F.description_from(None), "")

    def test_greenhouse_escaped_content_preserved_verbatim(self):
        job = fixture("greenhouse_jobs.json")["jobs"][0]
        md = F.html_to_markdown(html.unescape(job["content"]))
        for phrase in ("Example Corp builds payment infrastructure.", "#### What you will do",
                       "- Run **Kubernetes** clusters", "- Must have Python and Docker",
                       "**Nice to have**", "[here](https://example.com/about?utm_source=x)"):
            self.assertIn(phrase, md)
        self.assertNotIn("track()", md)

    def test_conversion_is_stable(self):
        job = fixture("greenhouse_jobs.json")["jobs"][0]
        source = html.unescape(job["content"])
        self.assertEqual(F.html_to_markdown(source), F.html_to_markdown(source))

    def test_plain_text_keeps_paragraphs(self):
        self.assertEqual(F.plain_text("Line one\r\n\r\n\r\n  Line   two\x07\n"),
                         "Line one\n\nLine two")


class RecordValidationTests(unittest.TestCase):
    def record(self, **changes):
        base = dict(source="Greenhouse", source_job_id="1", company="Acme", title="SRE",
                    location="Toronto", url="https://job-boards.greenhouse.io/acme/jobs/1",
                    description="Text", description_complete=True)
        base.update(changes)
        return F.JobRecord(**base)

    def test_valid(self):
        self.assertEqual(F.validate_record(self.record()), [])

    def test_rejections(self):
        self.assertIn("missing company", F.validate_record(self.record(company=" ")))
        self.assertIn("missing title", F.validate_record(self.record(title="")))
        self.assertIn("missing or invalid posting URL",
                      F.validate_record(self.record(url="javascript:alert(1)")))
        self.assertIn("missing or invalid posting URL", F.validate_record(self.record(url="")))
        self.assertIn("missing description", F.validate_record(self.record(description="")))
        self.assertIn("unsupported source 'Indeed'",
                      F.validate_record(self.record(source="Indeed")))

    def test_partial_needs_a_note(self):
        self.assertTrue(F.validate_record(self.record(description_complete=False)))
        self.assertEqual(F.validate_record(self.record(description_complete=False,
                                                       completeness_note="Snippet only.")), [])

    def test_partial_markdown_is_labelled(self):
        record = self.record(source="Jooble", description="Snippet text.",
                             description_complete=False, completeness_note="Partial snippet.")
        text = F.render_markdown(record, "2026-10-26")
        self.assertIn("**Description Completeness**: Partial - Partial snippet.", text)
        self.assertIn("Snippet text.", text)
        metadata_only = self.record(source="Simplify", description="",
                                    description_complete=False,
                                    completeness_note="Listing metadata only.")
        self.assertIn("_Listing metadata only._", F.render_markdown(metadata_only, "2026-10-26"))

    def test_markdown_schema(self):
        text = F.render_markdown(self.record(posted_date="2026-10-01"), "2026-10-26")
        lines = text.splitlines()
        self.assertEqual(lines[0], "# SRE - Acme")
        for expected in ("**Source**: Greenhouse", "**Location**: Toronto",
                         "**URL**: https://job-boards.greenhouse.io/acme/jobs/1",
                         "**Date Ingested**: 2026-10-26", "**Posted**: 2026-10-01",
                         "**Description Completeness**: Full", "## Description"):
            self.assertIn(expected, lines)
        generated = next(i for i, l in enumerate(lines) if l.startswith("**Generated By**"))
        self.assertGreater(lines.index("## Description"), generated)  # metadata block first
        hostile = self.record(title="# Injected\n## Requirements", company="Acme")
        self.assertTrue(F.render_markdown(hostile, "2026-10-26").startswith(
            "# Injected ## Requirements - Acme"))



class SanitizeDescriptionTests(unittest.TestCase):
    def test_invisible_characters_that_split_keywords_are_removed(self):
        self.assertEqual(F.sanitize_description("Py\u200bthon, Ku\u00adber\u2060netes\ufeff"),
                         "Python, Kubernetes")
        self.assertEqual(F.sanitize_description("a\u00a0b\x07c"), "a bc")

    def test_residual_and_double_escaped_markup(self):
        self.assertEqual(F.sanitize_description("R&amp;amp;D &amp;lt;p&amp;gt;team&amp;lt;/p&amp;gt;"),
                         "R&D\nteam")
        self.assertEqual(F.sanitize_description("One<br>Two<li>Three</li> <b>Four</b>"),
                         "One\nTwo\nThree\nFour")
        self.assertEqual(F.sanitize_description("Use <b>Go</b> daily"), "Use Go daily")

    def test_clean_text_is_unchanged(self):
        clean = ("#### About\n\n**Requirements**\n\n- C++ and Go\n- x < 5 and <T> generics\n"
                 "- [Apply](https://example.com/?a=1&b=2)")
        self.assertEqual(F.sanitize_description(clean), clean)
        once = F.sanitize_description("A&amp;amp;B\u200b\n\n\n\nC")
        self.assertEqual(F.sanitize_description(once), once)  # idempotent


if __name__ == "__main__":
    unittest.main()
