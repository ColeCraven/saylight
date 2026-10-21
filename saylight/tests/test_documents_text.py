"""Plain text and Markdown files: encodings, line endings, Markdown cleanup."""

import os
import tempfile
import unittest
from unittest import mock

from saylight.academic_text import normalize
from saylight.documents import LoadError
from saylight.documents import text as T


class EncodingTests(unittest.TestCase):
    TEXT = "Dyslexia ’quotes’ café – naïve €5"

    def test_byte_order_marks_and_utf8(self):
        for enc, label in (("utf-8", "utf-8"), ("utf-8-sig", "utf-8-bom"), ("utf-16", "utf-16-le-bom"),
                           ("utf-32", "utf-32-le-bom")):
            self.assertEqual(T.decode_text(self.TEXT.encode(enc)), (self.TEXT, label), enc)
        self.assertEqual(T.decode_text(b"\xfe\xff" + self.TEXT.encode("utf-16-be")), (self.TEXT, "utf-16-be-bom"))

    def test_utf16_without_a_mark(self):
        self.assertEqual(T.decode_text(self.TEXT.encode("utf-16-le")), (self.TEXT, "utf-16-le"))
        self.assertEqual(T.decode_text(self.TEXT.encode("utf-16-be")), (self.TEXT, "utf-16-be"))

    def test_code_page_then_latin1(self):
        with mock.patch("locale.getencoding", return_value="cp1252"):
            self.assertEqual(T.decode_text(self.TEXT.encode("cp1252")), (self.TEXT, "cp1252"))
            self.assertEqual(T.decode_text(b"a\x81b\xe9"), ("a\x81bé", "latin-1"))   # 0x81 isn't cp1252

    def test_line_endings(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "wb") as f:
                f.write("line one\r\nline two\rline three\r\n".encode("utf-16"))
            r = T.read_text_file(p)
            self.assertEqual((r.text, r.encoding, r.kind, r.title), ("line one\nline two\nline three\n",
                                                                     "utf-16-le-bom", "txt", "a"))


class MarkdownTests(unittest.TestCase):
    def test_markup_removed(self):
        md = ("---\ntitle: x\n---\n# Results\n\nWe found **large** effects in *children*[^1] "
              "([OSF](https://osf.io/x)) and `code`.\n\n![plot](a.png)\n\n```python\nprint(1)\n```\n\n"
              "| Group | Mean |\n|---|---|\n| A | 1.2 |\n\n> A quote.\n\n- item one\n- item two\n\n"
              "[^1]: A footnote.\n")
        text, notes = T.markdown_to_text(md)
        self.assertEqual(text, "Results\n\nWe found large effects in children (OSF) and code.\n\n"
                               "Group; Mean.\nA; 1.2.\n\nA quote.\n\n- item one\n- item two")
        self.assertEqual(notes, ["A footnote."])
        self.assertIn("We found large effects in children (OSF) and code.", normalize(text))

    def test_reference_definitions_and_setext_headings(self):
        text, notes = T.markdown_to_text("Intro\n=====\n\nSee [the site][1].\n\n[1]: https://example.org\n")
        self.assertEqual((text, notes), ("Intro\n\nSee the site.", []))


class TextFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data: bytes):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_markdown_file(self):
        body = "\n".join(f"Paragraph {i} is about reading." for i in range(20))
        p = self.write("notes.md", f"# Notes on reading\n\n{body}[^a]\n\n## References\n\nLee, A. (2001).\n\n"
                                   "[^a]: The footnote.\n".encode())
        r = T.read_text_file(p)
        self.assertEqual((r.kind, r.title, r.encoding), ("md", "notes", "utf-8"))
        self.assertEqual([(a.text, a.kind, a.page) for a in r.asides], [("The footnote.", "footnote", None)])
        self.assertTrue(r.text[r.references_at:].startswith("References"))

    def test_empty_and_too_large(self):
        for data in (b"", b"   \r\n\r\n", b"\xef\xbb\xbf"):
            with self.assertRaises(LoadError) as cm:
                T.read_text_file(self.write("e.txt", data))
            self.assertEqual(cm.exception.kind, "empty")
        with mock.patch.object(T, "MAX_TEXT_BYTES", 4):
            with self.assertRaises(LoadError) as cm:
                T.read_text_file(self.write("big.txt", b"hello world"))
            self.assertEqual(cm.exception.kind, "too_large")
        with self.assertRaises(LoadError) as cm:
            T.read_text_file(os.path.join(self.tmp.name, "missing.txt"))
        self.assertEqual(cm.exception.kind, "io")


if __name__ == "__main__":
    unittest.main()
