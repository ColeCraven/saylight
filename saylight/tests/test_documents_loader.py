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

from saylight import winrt_support
from saylight.documents import LoadedDocument, LoadError
from saylight.documents import loader as L
from saylight.documents import pdf_ocr as P
from saylight.ocr.engine import OcrError
from saylight.ocr.model import OcrText
from saylight.tests.test_pdf_ocr import SCAN_LINES, make_pdf, page_image
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


TEXT_1 = "The first page has real text on it, enough of it to read without any help from OCR at all."
TEXT_3 = "The third page has real text as well, so only the pictures in between need finding."


class FakeScanPdf:
    made = 0

    def __init__(self, data: bytes, password=None) -> None:
        FakeScanPdf.made += 1
        self.dpi: dict[int, int] = {}


class ScanTests(unittest.TestCase):
    """Scanned pages, with OCR faked: each page's text says which page it is."""

    @classmethod
    def setUpClass(cls):
        app()
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.mixed = make_pdf(os.path.join(d, "Mixed Secret.pdf"),
                             [("text", TEXT_1), ("scan", None), ("text", TEXT_3), ("scan", None)])
        cls.scans = make_pdf(os.path.join(d, "Scans Secret.pdf"), [("scan", None)] * 3)
        cls.long = make_pdf(os.path.join(d, "Long Secret.pdf"), [("text", TEXT_1), ("text", TEXT_3)]
                            + [("scan", None)] * 58)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.loader = L.DocumentLoader()
        self.addCleanup(self.loader.shutdown)
        self.events = []
        for name in ("progress", "built", "failed", "scanChoice", "scanProgress"):
            getattr(self.loader, name).connect(lambda *a, n=name: self.events.append((n,) + a))
        self.found = lambda page: f"Text found on page {page}."
        self.gate = None
        self.ocr_error = None
        FakeScanPdf.made = 0
        self.ok = mock.patch.object(winrt_support, "available", return_value=True)
        self.langs = mock.patch("saylight.ocr.engine.user_languages", return_value=["en-US"])
        for patcher in (self.ok, self.langs, mock.patch.object(P, "ScanPdf", FakeScanPdf),
                        mock.patch.object(P, "ocr_pages", side_effect=self.fake_ocr),
                        mock.patch.object(P, "page_dpi", return_value=300)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_ocr(self, data, pages, *, progress, cancelled, target_dpi=220, password=None):
        out = []
        for i, page in enumerate(pages):
            if self.gate is not None:
                self.gate.wait(5)
            if cancelled():
                raise L.Cancelled()
            if self.ocr_error is not None:
                raise self.ocr_error
            data.dpi[page] = target_dpi
            out.append(OcrText(self.found(page), ()))
            progress(i + 1, len(pages))
        return out

    def finished(self, token):
        return any(e[0] in ("built", "failed") and e[1] == token for e in self.events)

    def of(self, kind):
        return [e[1:] for e in self.events if e[0] == kind]

    def run_open(self, path, token=1):
        self.loader.open(token, path, BuildOptions())
        self.assertTrue(spin_until(lambda: self.finished(token)))
        return [e for e in self.events if e[0] in ("built", "failed") and e[1] == token]

    def test_scanned_pages_are_read_in_place(self):
        with self.assertLogs("saylight.documents", "INFO") as logs:
            (kind, token, doc, raw), = self.run_open(self.mixed)
        self.assertEqual((kind, token), ("built", 1))
        pages = raw.text.split("\n\n")
        self.assertEqual(pages, [TEXT_1, "Text found on page 2.", TEXT_3, "Text found on page 4."])
        self.assertEqual(raw.page_starts, [0] + [raw.text.index(p) for p in pages[1:]])
        self.assertEqual((raw.scanned_pages, raw.scan_dpi), ([], 220))  # rendered at 220, scanned at 300
        self.assertEqual(self.of("scanProgress"), [(1, 0, 2), (1, 1, 2), (1, 2, 2)])
        self.assertEqual(self.of("scanChoice"), [])
        self.assertIn("Text found on page 4.", doc.text)
        self.assertEqual(doc.source.title, "Mixed Secret")
        self.assertIn("INFO:saylight.documents:Scan: 2 of 4 pages need OCR", logs.output)
        self.assertNotIn("Secret", " ".join(logs.output))

    def test_a_pdf_that_is_all_scans(self):
        with mock.patch.object(P, "page_dpi", return_value=140), self.assertLogs("saylight.documents") as logs:
            (kind, _, doc, raw), = self.run_open(self.scans)
        self.assertEqual(kind, "built")
        self.assertEqual((raw.title, raw.pages, len(raw.page_starts)), ("Scans Secret", 3, 3))
        self.assertEqual(raw.scan_dpi, 140)
        self.assertIn("INFO:saylight.documents:Scan: low resolution (about 140 dpi)", logs.output)
        self.assertEqual([s.text for s in doc.sentences][:2], ["Text found on page 1.", "Text found on page 2."])

    def test_no_ocr_language_is_known_before_rendering(self):
        with mock.patch("saylight.ocr.engine.user_languages", return_value=[]):
            self.assertEqual(self.run_open(self.scans, 1), [("failed", 1, "scan_language")])
            (kind, _, doc, raw), = self.run_open(self.mixed, 2)
        self.assertEqual(FakeScanPdf.made, 0)
        self.assertEqual((kind, raw.scanned_pages, raw.scan_dpi), ("built", [2, 4], 0))  # read without its scans
        self.assertNotIn("Text found", raw.text)

    def test_without_winrt_scans_are_as_before(self):
        with mock.patch.object(winrt_support, "available", return_value=False):
            self.assertEqual(self.run_open(self.scans, 1), [("failed", 1, "scanned")])
            (_, _, _, raw), = self.run_open(self.mixed, 2)
        self.assertEqual(raw.scanned_pages, [2, 4])

    def test_more_than_50_scanned_pages_wait_for_a_choice(self):
        with self.assertLogs("saylight.documents") as logs:
            self.loader.open(4, self.long, BuildOptions())
            self.assertTrue(spin_until(lambda: self.of("scanChoice")))
            spin_until(lambda: False, 0.1)
        self.assertEqual(self.of("scanChoice"), [(4, 58, 60)])
        self.assertFalse(self.finished(4))
        self.assertTrue(self.loader.busy)
        self.assertEqual(self.of("scanProgress"), [])  # nothing is read until a chip is clicked
        self.assertIn("INFO:saylight.documents:Scan: choice shown (58 scanned of 60)", logs.output)
        self.assertEqual(self.loader.scanned_in(4, 1, 10), 8)
        self.assertEqual(self.loader.scanned_in(4, 10, 1), 8)
        self.assertEqual(self.loader.scanned_in(5, 1, 10), 0)
        with self.assertLogs("saylight.documents") as logs:
            self.loader.continue_scan(4, 12, 2)  # swapped: pages 2 to 12
            self.assertTrue(spin_until(lambda: self.finished(4)))
        (kind, _, doc, raw), = [e for e in self.events if e[0] == "built"]
        self.assertIn("INFO:saylight.documents:Scan: pages 2-12 chosen", logs.output)
        self.assertEqual(self.of("scanProgress")[-1], (4, 10, 10))
        pages = raw.text.split("\n\n")
        self.assertEqual(len(raw.page_starts), 60)
        self.assertEqual(pages[0], "")                     # page 1 is outside the range, though it has text
        self.assertEqual(pages[1], TEXT_3)                 # a text page inside it is read as it is
        self.assertEqual(pages[2:12], [f"Text found on page {p}." for p in range(3, 13)])
        self.assertEqual(set(pages[12:]), {""})
        self.assertEqual(raw.text[raw.page_starts[2]:].split("\n\n")[0], "Text found on page 3.")
        self.assertEqual(doc.source.title, "Long Secret")
        self.assertEqual(self.loader.scanned_in(4, 1, 60), 0)

    def test_cancel_drops_the_waiting_pdf(self):
        with self.assertLogs("saylight.documents"):
            self.loader.open(4, self.long, BuildOptions())
            self.assertTrue(spin_until(lambda: self.of("scanChoice")))
        self.loader.cancel(4)
        self.assertEqual(self.loader.scanned_in(4, 1, 60), 0)
        self.loader.continue_scan(4, 1, 50)
        spin_until(lambda: False, 0.2)
        self.assertFalse(self.finished(4))
        self.assertEqual(self.of("scanProgress"), [])

    def test_another_file_replaces_the_waiting_pdf(self):
        with self.assertLogs("saylight.documents"):
            self.loader.open(4, self.long, BuildOptions())
            self.assertTrue(spin_until(lambda: self.of("scanChoice")))
            self.run_open(self.mixed, 5)
        self.assertEqual(self.loader.scanned_in(4, 1, 60), 0)

    def test_nothing_found(self):
        self.found = lambda page: "   "
        with self.assertLogs("saylight.documents") as logs:
            self.assertEqual(self.run_open(self.scans), [("failed", 1, "scan_nothing")])
        self.assertIn("INFO:saylight.documents:Scan: 3 empty pages skipped", logs.output)

    def test_ocr_failures(self):
        cases = ((OcrError("error"), "scan_error"), (OcrError("no_language"), "scan_language"),
                 (RuntimeError("secret words"), "scan_error"))
        for token, (error, kind) in enumerate(cases, 1):
            self.ocr_error = error
            with self.assertLogs("saylight.documents") as logs:
                self.assertEqual(self.run_open(self.scans, token), [("failed", token, kind)])
            self.assertNotIn("secret", " ".join(logs.output))

    def test_stop_while_finding_the_text(self):
        self.gate = threading.Event()
        with self.assertLogs("saylight.documents") as logs:
            self.loader.open(6, self.mixed, BuildOptions())
            self.assertTrue(spin_until(lambda: self.of("scanProgress")))
            self.loader.cancel(6)
            self.gate.set()
            spin_until(lambda: any("cancelled" in line for line in logs.output), 5)
        self.assertFalse(self.finished(6))
        self.assertIn("INFO:saylight.documents:Scan: cancelled at page 1 of 2", logs.output)


@unittest.skipUnless(winrt_support.available(), "Windows OCR (winrt) is not installed here")
class WindowsScanTests(unittest.TestCase):
    def test_a_real_scan_is_read(self):
        from saylight.ocr.engine import user_languages
        if not user_languages():
            self.skipTest("no OCR language on this PC")
        app()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = make_pdf(os.path.join(tmp.name, "scan.pdf"), [("scan", page_image(SCAN_LINES, 200))] * 2)
        loader = L.DocumentLoader()
        self.addCleanup(loader.shutdown)
        got = []
        loader.built.connect(lambda *a: got.append(("built",) + a))
        loader.failed.connect(lambda *a: got.append(("failed",) + a))
        loader.open(1, path, BuildOptions())
        self.assertTrue(spin_until(lambda: got, 60))
        kind, _, doc, raw = got[0]
        self.assertEqual(kind, "built", got)
        self.assertEqual(raw.scan_dpi, 200)
        self.assertEqual(doc.text.count("helps many readers keep their place."), 2)


if __name__ == "__main__":
    unittest.main()
