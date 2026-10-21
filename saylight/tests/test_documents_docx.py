"""The Word reader: a hand-written fixture with what Word and citation managers produce, and a file Word made."""

import os
import tempfile
import unittest
import zipfile
from unittest import mock

from saylight.academic_text import normalize
from saylight.documents import LoadError
from saylight.documents import docx as D
from saylight.text_model import BuildOptions, Document

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "documents")


def kind_of(path):
    try:
        D.read_docx(path)
    except LoadError as e:
        return e.kind, e.detail
    return "ok", ""


class HandmadeDocxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = D.read_docx(os.path.join(FIXTURES, "handmade.docx"))
        cls.paras = cls.doc.text.split("\n\n")

    def test_headings_and_split_runs(self):
        self.assertEqual(self.paras[0], "Reading in Dyslexia")
        self.assertEqual(self.paras[1], "1. Introduction")
        self.assertIn("reading comprehension in many children", self.paras[2])
        self.assertEqual((self.doc.kind, self.doc.title), ("docx", "handmade"))

    def test_citation_fields_are_exact_spans(self):
        self.assertEqual([self.doc.text[a:b] for a, b in self.doc.citation_spans],
                         [" (Snowling et al., 2020)", " [3]"])

    def test_tracked_changes_hidden_text_and_marks(self):
        self.assertEqual(self.paras[3], "Readers were not slower on nonwords [3].")
        self.assertNotIn("always", self.doc.text)
        self.assertNotIn("hidden", self.doc.text)
        self.assertIn("differences in the left hemisphere", self.paras[2])   # the footnote mark is dropped
        self.assertEqual([(a.text, a.kind, a.page) for a in self.doc.asides],
                         [("Functional MRI measures blood flow.", "footnote", None)])

    def test_inline_specials(self):
        self.assertEqual(self.paras[4], "Page 4 shows the well-known effect of phonology± noise.")

    def test_lists(self):
        self.assertEqual(self.paras[5:11], ["1. Recruit participants", "a) Screen for vision problems",
                                            "b) Screen for hearing problems", "2. Run the reading task",
                                            "Bullet one", "Bullet two"])

    def test_hyperlink_table_box_math_and_toc(self):
        self.assertEqual(self.paras[11], "Data are at the OSF page.")
        self.assertEqual(self.paras[12:15], ["Group; Accuracy; Speed.", "Dyslexic; 0.81; 1.2 s.",
                                             "Dyslexic; 0.79; inner A; inner B. after inner."])
        self.assertEqual(self.paras[15:17], ["Figure text follows.", "Box: sidebar note."])   # once, not twice
        self.assertEqual(self.paras[17], "We fit y=ax+b to the data.")
        self.assertNotIn("Introduction 1", self.doc.text)   # the table of contents is skipped

    def test_references(self):
        self.assertTrue(self.doc.text[self.doc.references_at:].startswith("References\n\nSnowling"))

    def test_normalize_reads_it(self):
        out = normalize(self.doc.text)
        self.assertIn("reading comprehension in many children. fMRI studies", out)
        self.assertIn("plus or minus noise", out)

    def test_document(self):
        doc = Document.build(self.doc, BuildOptions())
        self.assertEqual(list(doc.sections), ["notes", "refs"])
        self.assertIn("Notes\n\nFunctional MRI measures blood flow.", doc.text)
        self.assertIn("(Snowling et al., 2020)", [doc.text[a:b] for a, b in doc.skipped()])
        self.assertIsNone(doc.page_of(0))


class WordMadeDocxTests(unittest.TestCase):
    def test_file_from_word(self):
        ld = D.read_docx(os.path.join(FIXTURES, "word_made.docx"))
        paras = ld.text.split("\n\n")
        self.assertEqual(paras[:2], ["Listening While Reading", "Introduction"])
        self.assertIn("1. Recruit the participants", paras)          # numbered by its list style
        self.assertIn("2. Run the reading task", paras)
        self.assertIn("Dyslexic; 0.81; 1.2.", paras)
        self.assertEqual([a.text for a in ld.asides], ["A footnote written in Word."])
        self.assertTrue(ld.text[ld.references_at:].startswith("References"))
        self.assertIn("keep their place and understand", ld.text)


class BadDocxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name, data=None, parts=None):
        p = os.path.join(self.tmp.name, name)
        if parts is not None:
            with zipfile.ZipFile(p, "w") as z:
                for part, body in parts.items():
                    z.writestr(part, body)
        else:
            with open(p, "wb") as f:
                f.write(data)
        return p

    def test_old_word_and_encrypted_files(self):
        ole = D.OLE_MAGIC + b"\0" * 504
        self.assertEqual(kind_of(self.path("old.docx", ole + "WordDocument".encode("utf-16-le"))),
                         ("unsupported", ".doc"))
        self.assertEqual(kind_of(self.path("locked.docx", ole + "EncryptedPackage".encode("utf-16-le"))),
                         ("protected", ".docx"))

    def test_not_a_zip_or_not_word(self):
        self.assertEqual(kind_of(self.path("junk.docx", b"hello"))[0], "corrupt")
        self.assertEqual(kind_of(self.path("empty.docx", parts={"a.txt": "x"}))[0], "corrupt")
        self.assertEqual(kind_of(self.path("bad.docx", parts={"word/document.xml": "<w:document"}))[0], "corrupt")
        self.assertEqual(kind_of(os.path.join(self.tmp.name, "missing.docx"))[0], "io")

    def test_entities_and_huge_parts_are_refused(self):
        dtd = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
        self.assertEqual(kind_of(self.path("dtd.docx", parts={"word/document.xml": dtd}))[0], "corrupt")
        with mock.patch.object(D, "MAX_XML_BYTES", 10):
            p = self.path("big.docx", parts={"word/document.xml": "<x>" + "a" * 100 + "</x>"})
            self.assertEqual(kind_of(p)[0], "too_large")


if __name__ == "__main__":
    unittest.main()
