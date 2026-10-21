"""Picking a reader by extension, and the worker thread that opens files and builds readings."""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication

from saylight.documents import LoadedDocument, LoadError
from saylight.documents import loader as L
from saylight.text_model import BuildOptions, Document

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "documents")
_app = None


def app():
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])
    return _app


def spin_until(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app().processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        if pred():
            return True
        time.sleep(0.002)
    return pred()


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def kind(self, path):
        try:
            L.read_file(path)
        except LoadError as e:
            return e.kind, e.detail
        return "ok", ""

    def test_by_extension(self):
        for name, kind in (("paper.pdf", "pdf"), ("handmade.docx", "docx")):
            self.assertEqual(L.read_file(os.path.join(FIXTURES, name)).kind, kind)
        for name, kind in (("a.TXT", "txt"), ("b.md", "md"), ("c.markdown", "md")):
            p = os.path.join(self.tmp.name, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("Some text to read.")
            self.assertEqual(L.read_file(p).kind, kind)

    def test_unsupported_and_missing(self):
        old = os.path.join(self.tmp.name, "old.doc")
        open(old, "wb").close()
        self.assertEqual(self.kind(old), ("unsupported", ".doc"))   # the notice names older Word files
        self.assertEqual(self.kind(os.path.join(self.tmp.name, "x.xyz")), ("unsupported", ""))
        self.assertEqual(self.kind(os.path.join(self.tmp.name, "gone.pdf")), ("io", "missing"))


class LoaderTests(unittest.TestCase):
    def setUp(self):
        app()
        self.loader = L.DocumentLoader()
        self.addCleanup(self.loader.shutdown)
        self.events = []
        self.loader.progress.connect(lambda *a: self.events.append(("progress",) + a))
        self.loader.built.connect(lambda *a: self.events.append(("built",) + a))
        self.loader.failed.connect(lambda *a: self.events.append(("failed",) + a))

    def finished(self, token):
        return any(e[0] in ("built", "failed") and e[1] == token for e in self.events)

    def test_open_builds_on_the_worker(self):
        gui = threading.get_ident()
        seen = []
        real = Document.build

        def build(raw, options):
            seen.append(threading.get_ident())
            return real(raw, options)
        path = os.path.join(FIXTURES, "paper.pdf")
        with mock.patch.object(L.Document, "build", side_effect=build):
            self.loader.open(7, path, BuildOptions())
            self.assertTrue(spin_until(lambda: self.finished(7)))
        kind, token, doc, raw = self.events[-1]
        self.assertEqual((kind, token), ("built", 7))
        self.assertIsInstance(doc, Document)
        self.assertIsInstance(raw, LoadedDocument)
        self.assertEqual((doc.source.kind, doc.source.path, doc.source.title), ("file", path, "paper"))
        self.assertNotEqual(seen, [gui])
        self.assertIn(("progress", 7, 3, 3), self.events)
        self.assertFalse(self.loader.busy)

    def test_failures_report_the_kind(self):
        self.loader.open(1, os.path.join(FIXTURES, "rc4_user.pdf"), BuildOptions())
        self.loader.open(2, os.path.join(FIXTURES, "rc4_user.pdf"), BuildOptions(), password="reading")
        self.loader.open(3, os.path.join(FIXTURES, "blank.pdf"), BuildOptions())
        self.assertTrue(spin_until(lambda: all(self.finished(t) for t in (1, 2, 3))))
        outcome = {e[1]: e[0] if e[0] == "built" else e[2] for e in self.events if e[0] != "progress"}
        self.assertEqual(outcome, {1: "password", 2: "built", 3: "empty"})

    def test_cancel_reports_nothing(self):
        gate = threading.Event()
        real = L.read_file

        def slow(path, **kw):
            gate.wait(5)
            return real(path, **kw)
        with mock.patch.object(L, "read_file", side_effect=slow):
            self.loader.open(5, os.path.join(FIXTURES, "paper.pdf"), BuildOptions())
            self.loader.cancel(5)
            gate.set()
            self.loader.open(6, os.path.join(FIXTURES, "handmade.docx"), BuildOptions())
            self.assertTrue(spin_until(lambda: self.finished(6)))
        self.assertEqual([e[1] for e in self.events], [6])

    def test_build_text_and_rebuild_a_file(self):
        self.loader.build(9, "First sentence. Second one (Smith, 2019).", BuildOptions())
        self.assertTrue(spin_until(lambda: self.finished(9)))
        _, _, doc, raw = self.events[-1]
        self.assertEqual([s.say for s in doc.sentences], ["First sentence.", "Second one."])
        self.assertEqual(raw, "First sentence. Second one (Smith, 2019).")
        ld = L.read_file(os.path.join(FIXTURES, "paper.pdf"))
        self.loader.build(10, ld, BuildOptions(read_references=True))
        self.assertTrue(spin_until(lambda: self.finished(10)))
        doc = self.events[-1][2]
        self.assertIn("refs", doc.sections)
        self.assertNotIn("refs", [r.kind for r in doc.regions])   # read this time

    def test_worker_survives_a_bad_reader(self):
        with mock.patch.object(L, "read_file", side_effect=RuntimeError("boom")), \
                self.assertLogs("saylight.documents", "WARNING") as logs:
            self.loader.open(11, "x.pdf", BuildOptions())
            self.assertTrue(spin_until(lambda: self.finished(11)))
        self.assertEqual(logs.output, ["WARNING:saylight.documents:Opening failed: RuntimeError"])
        self.assertEqual(self.events[-1], ("failed", 11, "corrupt"))
        self.loader.build(12, "Still works.", BuildOptions())
        self.assertTrue(spin_until(lambda: self.finished(12)))

    def test_logs_hold_no_names_or_text(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        secret = os.path.join(tmp, "Private Diary.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("My secret plans for the week.")
        with self.assertLogs("saylight.documents", "INFO") as logs:
            self.loader.open(13, secret, BuildOptions())
            self.assertTrue(spin_until(lambda: self.finished(13)))
        line = " ".join(logs.output)
        for word in ("Private", "Diary", "secret", ".txt", tmp):
            self.assertNotIn(word, line)


if __name__ == "__main__":
    unittest.main()
