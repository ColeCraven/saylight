"""The PDF reader on the fixtures in fixtures/documents (see make_fixtures.py and make_word_fixtures.ps1)."""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from saylight.academic_text import normalize
from saylight.documents import LoadError
from saylight.documents import pdf as P
from saylight.text_model import BuildOptions, Document

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "documents")
PASSWORD = "reading"


def fixture(name):
    return os.path.join(FIXTURES, name)


def kind_of(name, **kw):
    try:
        P.extract_pdf(fixture(name), **kw)
    except LoadError as e:
        return e.kind
    return "ok"


class PaperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ld = P.extract_pdf(fixture("paper.pdf"))
        cls.norm = normalize(cls.ld.text)
        cls.paras = cls.norm.split("\n\n")

    def test_title_authors_and_abstract_are_separate(self):
        self.assertEqual(self.paras[:3], ["Reading Aloud Helps Readers with Dyslexia", "Ada Lovelace and Grace Hopper",
                                          "Abstract"])
        self.assertTrue(self.paras[3].startswith("We test whether hearing a text"))
        self.assertEqual((self.ld.kind, self.ld.title, self.ld.pages), ("pdf", "paper", 3))

    def test_figure_labels_stamp_and_furniture_are_gone(self):
        for clutter in ("Accuracy\n", "Time (s)", "arXiv:2601", "Journal of Reading Research", "Downloaded from",
                        "0.6"):
            self.assertNotIn(clutter, self.ld.text)
            self.assertFalse(any(clutter in a.text for a in self.ld.asides), clutter)
        self.assertEqual(re.findall(r"\d(?:https?://|www\.)", self.norm), [])

    def test_sentences_rejoin_across_pages_footnote_marks_and_floats(self):
        self.assertIn("looked at the comprehension of long texts", self.norm)
        self.assertIn("as it is spoken, which may help", self.norm)
        self.assertIn("2 Method", self.paras)
        self.assertIn("Participants read twelve passages", self.norm)

    def test_small_print_and_captions_become_asides(self):
        asides = [(a.kind, a.page, a.text.split("\n")[0]) for a in self.ld.asides]
        self.assertEqual(asides, [
            ("footnote", 1, "1 See the project page for the programs we tested and their settings."),
            ("caption", 1, "Figure 1: Accuracy by condition. Bars show the mean of each group."),
            ("table", 2, "Group Accuracy Time n"),
            ("table", 2, "Table 1: Mean accuracy and reading time by group."),
        ])

    def test_subscripts_are_kept(self):
        self.assertIn("BERTBASE", self.norm)

    def test_references_and_pages(self):
        self.assertTrue(self.ld.text[self.ld.references_at:].startswith("References"))
        self.assertGreater(self.ld.references_at / len(self.ld.text), 0.5)
        starts = self.ld.page_starts
        self.assertEqual(starts[0], 0)
        self.assertTrue(self.ld.text[starts[1]:].startswith("hension of long texts"))
        self.assertTrue(self.ld.text[starts[2]:].startswith("3 Results"))
        self.assertEqual(self.ld.scanned_pages, [])

    def test_document_from_the_paper(self):
        doc = Document.build(self.ld, BuildOptions())
        shown = [doc.text[a:b] for a, b in doc.skipped()]
        self.assertIn("(Snowling et al., 2020; Peters, 2018a)", shown)
        self.assertEqual(list(doc.sections), ["notes", "refs"])
        pages = {s.say: doc.page_of(s.index) for s in doc.sentences}
        self.assertEqual(pages["Abstract"], 1)
        self.assertEqual(pages["Code and data are at link for anyone."], 2)
        self.assertEqual(pages["3 Results"], 3)
        self.assertEqual(pages["The twelve passages came from first-year textbooks, four from each of three subjects."],
                         3)   # the appendix, read before the notes
        self.assertEqual(len(doc.page_starts), 3)

    def test_progress_and_cancel(self):
        seen = []
        P.extract_pdf(fixture("paper.pdf"), progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])
        calls = []

        def cancelled():
            calls.append(1)
            return len(calls) > 1
        with self.assertRaises(P.Cancelled):
            P.extract_pdf(fixture("paper.pdf"), cancelled=cancelled)

    def test_same_text_without_pillow(self):
        # The release build leaves Pillow out; pypdf's text extraction doesn't need it.
        code = ("import sys; sys.modules['PIL'] = None; sys.path.insert(0, sys.argv[1]);"
                "from saylight.documents.pdf import extract_pdf;"
                "sys.stdout.buffer.write(extract_pdf(sys.argv[2]).text.encode('utf-8'))")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run([sys.executable, "-I", "-c", code, root, fixture("paper.pdf")], capture_output=True,
                             check=True, timeout=60)
        self.assertEqual(out.stdout.decode("utf-8").replace("\r\n", "\n"), self.ld.text)


class WordMadePdfTests(unittest.TestCase):
    def test_word_pdf(self):
        ld = P.extract_pdf(fixture("word_made.pdf"))
        doc = Document.build(ld, BuildOptions())
        said = [s.say for s in doc.sentences]
        self.assertEqual(said[:3], ["Listening While Reading", "Introduction",
                                    "Many students with dyslexia listen to a text while they read it."])
        self.assertIn("This sentence is long enough to wrap onto a second line in the PDF, so that the reader has to "
                      "join the lines again.", said)
        self.assertIn("1. Recruit the participants", said)
        self.assertEqual([a.kind for a in ld.asides], ["footnote"])
        self.assertEqual(list(doc.sections), ["notes", "refs"])
        self.assertEqual(doc.page_of(0), 1)


class SpecialPdfTests(unittest.TestCase):
    def test_no_text(self):
        self.assertEqual(kind_of("scan.pdf"), "scanned")      # a picture of a page
        self.assertEqual(kind_of("unmapped.pdf"), "scanned")  # a font with no Unicode mapping
        self.assertEqual(kind_of("blank.pdf"), "empty")

    def test_passwords(self):
        self.assertEqual(kind_of("rc4_owner.pdf"), "ok")       # restrictions only: opens
        self.assertEqual(kind_of("rc4_user.pdf"), "password")
        self.assertEqual(kind_of("rc4_user.pdf", password="wrong"), "password")
        ld = P.extract_pdf(fixture("rc4_user.pdf"), password=PASSWORD)
        self.assertIn("We test whether hearing a text", ld.text)

    def test_aes(self):
        if P.aes_available():
            self.assertEqual(kind_of("aes_owner.pdf"), "ok")
            self.assertEqual(kind_of("aes_user.pdf"), "password")
            self.assertEqual(kind_of("aes_user.pdf", password=PASSWORD), "ok")
        else:
            # Without the cryptography package no password can help, so none is asked for.
            for kw in ({}, {"password": PASSWORD}):
                self.assertEqual(kind_of("aes_owner.pdf", **kw), "protected")
                self.assertEqual(kind_of("aes_user.pdf", **kw), "protected")

    def test_aes_without_cryptography(self):
        with mock.patch.object(P, "aes_available", return_value=False):
            self.assertEqual(kind_of("aes_user.pdf"), "protected")
            self.assertEqual(kind_of("rc4_user.pdf"), "password")

    def test_broken_and_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.pdf")
            with open(bad, "wb") as f:
                f.write(b"%PDF-1.4\nnot really a PDF at all\n")
            with self.assertRaises(LoadError) as cm:
                P.extract_pdf(bad)
            self.assertEqual(cm.exception.kind, "corrupt")
            with self.assertRaises(LoadError) as cm:
                P.extract_pdf(os.path.join(d, "missing.pdf"))
            self.assertEqual(cm.exception.kind, "io")

    def test_too_large(self):
        with mock.patch.object(P, "MAX_PAGES", 2):
            self.assertEqual(kind_of("paper.pdf"), "too_large")
        with mock.patch.object(P, "MAX_CHARS", 100):
            self.assertEqual(kind_of("paper.pdf"), "too_large")

    def test_logs_hold_no_text_or_names(self):
        with self.assertLogs("saylight.documents", "INFO") as logs:
            P.extract_pdf(fixture("paper.pdf"))
        line = " ".join(logs.output)
        for word in ("paper", "Reading", "Dyslexia", ".pdf", FIXTURES):
            self.assertNotIn(word, line)


if __name__ == "__main__":
    unittest.main()
