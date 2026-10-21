"""Tests for ocr/engine.py: the upscale plan and its cap, WinRT results as model types, the worker's queue,
tokens, failure kinds and cached languages, and the GUI-thread refusal. With winrt installed, real recognition
on a drawn image (nothing is shown or saved)."""

import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEventLoop, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

from saylight import winrt_support
from saylight.ocr import engine as E
from saylight.ocr.model import OcrLine, OcrResult, OcrWord

_app = None


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


def spin_until(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        if pred():
            return True
        time.sleep(0.002)
    return pred()


def text_image(lines: list[str], px: int = 18, width: int = 900, dpr: float = 1.0) -> QImage:
    """Black text on white, drawn at `dpr` like a screen capture of that scale."""
    line_h = round(px * 1.6)
    img = QImage(round(width * dpr), round((len(lines) * line_h + 2 * px) * dpr), QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.scale(dpr, dpr)
    font = QFont("Segoe UI")
    font.setPixelSize(px)
    p.setFont(font)
    p.setPen(QColor("black"))
    for i, line in enumerate(lines):
        p.drawText(QPointF(px, px + (i + 1) * line_h - px * 0.4), line)
    p.end()
    return img


def fake_res(lines, angle=None):
    def word(text, x, y, w, h):
        return SimpleNamespace(text=text, bounding_rect=SimpleNamespace(x=x, y=y, width=w, height=h))
    return SimpleNamespace(lines=[SimpleNamespace(words=[word(*w) for w in ws]) for ws in lines], text_angle=angle)


class PlanTests(unittest.TestCase):
    def test_twice_the_size_when_it_fits(self) -> None:
        self.assertEqual(E.upscale_for(840, 449, 2.0), 2.0)
        self.assertEqual(E.upscale_for(5000, 300, 2.0), 2.0)

    def test_capped_at_the_largest_side(self) -> None:
        self.assertAlmostEqual(E.upscale_for(6144, 1728, 2.0), 10_000 / 6144)
        self.assertEqual(E.upscale_for(10_000, 50, 2.0), 1.0)
        self.assertEqual(E.upscale_for(800, 600, 1.0), 1.0)

    def test_too_large_even_at_native_size(self) -> None:
        with self.assertRaises(E.OcrError) as cm:
            E.upscale_for(10_001, 400, 2.0)
        self.assertEqual(cm.exception.kind, "too_large")

    def test_failure_kinds(self) -> None:
        for kind in ("no_language", "too_large", "error"):
            self.assertEqual(E.OcrError(kind).kind, kind)
        with self.assertRaises(ValueError):
            E.OcrError("nothing_found")


class ResultTests(unittest.TestCase):
    def test_boxes_go_to_global_logical_px(self) -> None:
        # A 2x upscale of a capture at 1.25: image px / 2.5, from the area's top-left.
        res = fake_res([[("Hello", 50, 25, 100, 20), ("world", 175, 25, 90, 20)], []], angle=1.5)
        out = E._result(res, "en-US", QPointF(-1920, 833), 2.5)
        self.assertEqual(len(out.lines), 1)  # empty lines are dropped
        self.assertEqual([w.text for w in out.lines[0].words], ["Hello", "world"])
        self.assertEqual(out.lines[0].words[0].rect, QRectF(-1900, 843, 40, 8))
        self.assertEqual((out.angle, out.language), (1.5, "en-US"))
        self.assertEqual(E._result(fake_res([]), "en-GB", QPointF(), 1.0).angle, 0.0)

    def test_blocking_ocr_refuses_the_gui_thread(self) -> None:
        # .get() there fails and leaves the operation running, so it never starts.
        with self.assertRaises(RuntimeError):
            E.recognize_blocking(text_image(["Some text"]))

    def test_a_null_image_has_no_lines(self) -> None:
        self.assertEqual(E.recognize_blocking(QImage()), OcrResult((), 0.0, ""))


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.langs = mock.patch.object(E, "user_languages", return_value=["en-GB", "en-US"])
        self.query = self.langs.start()
        self.addCleanup(self.langs.stop)
        self.worker = E.OcrWorker()
        self.addCleanup(self.worker.shutdown)
        self.events = []
        self.worker.done.connect(lambda *a: self.events.append(("done",) + a))
        self.worker.failed.connect(lambda *a: self.events.append(("failed",) + a))

    def test_results_come_back_tagged_on_the_gui_thread(self) -> None:
        seen = []
        result = OcrResult((OcrLine((OcrWord("Hi", QRectF(1, 2, 3, 4)),)),), 0.0, "en-GB")

        def fake(image, origin, dpr, upscale):
            seen.append((threading.current_thread() is threading.main_thread(), origin, dpr, upscale))
            return result
        with mock.patch.object(E, "_recognize", side_effect=fake):
            self.worker.recognize(7, text_image(["x"]), origin=QPointF(10, 20), dpr=1.25)
            self.assertTrue(spin_until(lambda: self.events))
        self.assertEqual(self.events, [("done", 7, result)])
        self.assertEqual(seen, [(False, QPointF(10, 20), 1.25, 2.0)])

    def test_failures_report_the_kind_only(self) -> None:
        errors = iter([E.OcrError("too_large"), E.OcrError("no_language"), ValueError("secret text")])

        def fake(*_a):
            raise next(errors)
        with mock.patch.object(E, "_recognize", side_effect=fake), \
                self.assertLogs("saylight.ocr", "INFO") as logs:
            for token in (1, 2, 3):
                self.worker.recognize(token, text_image(["x"]), origin=QPointF(), dpr=1.0)
            self.assertTrue(spin_until(lambda: len(self.events) == 3))
        self.assertEqual(self.events, [("failed", 1, "too_large"), ("failed", 2, "no_language"),
                                       ("failed", 3, "error")])
        self.assertNotIn("secret", " ".join(logs.output))
        self.assertIn("WARNING:saylight.ocr:OCR failed: ValueError", logs.output)

    def test_languages_are_read_once_and_refreshed_on_request(self) -> None:
        self.worker.start()
        self.assertEqual(self.worker.languages(), ["en-GB", "en-US"])
        self.assertEqual(self.worker.languages(), ["en-GB", "en-US"])
        self.assertEqual(self.query.call_count, 1)
        generation = E._generation
        self.query.return_value = []
        self.assertEqual(self.worker.refresh_languages(), [])
        self.assertEqual(self.worker.languages(), [])
        self.assertEqual(self.query.call_count, 2)
        self.assertEqual(E._generation, generation + 1)  # every thread makes its engine again

    def test_no_winrt_counts_as_no_language(self) -> None:
        self.query.side_effect = ImportError("no winrt")
        with self.assertLogs("saylight.ocr", "WARNING") as logs:
            self.assertEqual(self.worker.languages(), [])
            self.assertEqual(self.worker.refresh_languages(), [])
        self.assertIn("WARNING:saylight.ocr:OCR languages unavailable (ImportError)", logs.output)

    def test_nothing_after_shutdown(self) -> None:
        with mock.patch.object(E, "_recognize", return_value=OcrResult((), 0.0, "")):
            self.worker.shutdown()
            self.worker.recognize(5, text_image(["x"]), origin=QPointF(), dpr=1.0)
            spin_until(lambda: False, 0.2)
        self.assertEqual(self.events, [])


@unittest.skipUnless(winrt_support.available(), "Windows OCR (winrt) is not installed here")
class WindowsOcrTests(unittest.TestCase):
    LINES = ["Phonological processing deficits are widely reported",
             "in adults with dyslexia, although the size varies."]

    def test_a_capture_at_125_percent(self) -> None:
        if not E.user_languages():
            self.skipTest("no OCR language on this PC")
        worker = E.OcrWorker()
        self.addCleanup(worker.shutdown)
        got = []
        worker.done.connect(lambda token, result: got.append((token, result)))
        worker.failed.connect(lambda token, kind: got.append((token, kind)))
        worker.recognize(3, text_image(self.LINES, px=14, dpr=1.25), origin=QPointF(100, 50), dpr=1.25)
        self.assertTrue(spin_until(lambda: got, 20))
        token, result = got[0]
        self.assertEqual(token, 3)
        self.assertIsInstance(result, OcrResult)
        words = [w for line in result.lines for w in line.words]
        self.assertIn("dyslexia,", [w.text for w in words])
        first = words[0]
        # Drawn at logical (14, 14..34) in the area, which starts at (100, 50).
        self.assertLess(abs(first.rect.left() - 114), 4)
        self.assertTrue(55 < first.rect.center().y() < 80, first.rect)

    def test_blocking_on_a_worker_thread(self) -> None:
        out = {}

        def work():
            try:
                out["result"] = E.recognize_blocking(text_image(self.LINES, px=16))
            except Exception as e:  # noqa: BLE001
                out["error"] = e
        th = threading.Thread(target=work)
        th.start()
        th.join(30)
        if isinstance(out.get("error"), E.OcrError) and out["error"].kind == "no_language":
            self.skipTest("no OCR language on this PC")
        text = " ".join(w.text for line in out["result"].lines for w in line.words)
        self.assertIn("Phonological processing", text)


if __name__ == "__main__":
    unittest.main()
