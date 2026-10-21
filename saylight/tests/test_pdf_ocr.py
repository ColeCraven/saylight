"""Tests for documents/pdf_ocr.py: the time estimate, how finely a page was scanned, and reading pages in order
with progress and Cancel. With winrt installed, a real scanned PDF is rendered at 220 dpi despite Windows' display
scale (the scale fix) and read. Test PDFs are made in a temporary folder; nothing is shown.

make_pdf() is shared with the loader's tests."""

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QMarginsF, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from saylight import winrt_support
from saylight.documents import pdf_ocr as P
from saylight.documents.pdf import Cancelled
from saylight.ocr.model import OcrLine, OcrResult, OcrText, OcrWord

_app = None

SCAN_LINES = ["Reading aloud while the words are marked",
              "helps many readers keep their place.",
              "This page is only a picture of text."]


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


def page_image(lines: list[str], dpi: int, pt: float = 14) -> QImage:
    """A Letter-width picture of `lines` in `pt` type, as a scanner at `dpi` would make it."""
    px = round(pt * dpi / 72)
    img = QImage(round(8.5 * dpi), round(px * 1.8 * (len(lines) + 2)), QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    font = QFont("Times New Roman")
    font.setPixelSize(px)
    p.setFont(font)
    p.setPen(QColor("black"))
    for i, line in enumerate(lines):
        p.drawText(QPointF(dpi, px * 1.8 * (i + 1.5)), line)
    p.end()
    return img


def make_pdf(path: str, pages: list[tuple[str, object]], dpi: int = 200) -> str:
    """A Letter PDF: ("text", str) pages have real text, ("scan", QImage | None) pages are only a picture (a
    small grey one when None), ("blank", None) pages are empty."""
    w = QPdfWriter(path)
    w.setResolution(dpi)
    w.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.Letter), QPageLayout.Orientation.Portrait,
                                QMarginsF(0, 0, 0, 0)))
    p = QPainter(w)
    for i, (kind, value) in enumerate(pages):
        if i:
            w.newPage()
        if kind == "text":
            font = QFont("Arial")
            font.setPixelSize(round(11 * dpi / 72))
            p.setFont(font)
            p.drawText(QRectF(dpi, dpi, 6.5 * dpi, 9 * dpi), 0x1000 | 0x0001, value)  # word wrap, left
        elif kind == "scan":
            img = value if value is not None else _grey(dpi)
            p.drawImage(QRectF(0, 0, img.width(), img.height()), img)
    p.end()
    return path


def _grey(dpi: int) -> QImage:
    img = QImage(round(dpi * 2), round(dpi * 2), QImage.Format.Format_RGB32)
    img.fill(QColor(200, 200, 200))
    return img


class EstimateTests(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual([P.estimate_s(n) for n in (0, 1, 50, 160)], [0, 1, 40, 128])
        self.assertEqual((P.TARGET_DPI, P.LOW_DPI), (220, 150))


class PageDpiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_scan_resolution(self) -> None:
        import pypdf
        for dpi in (150, 300):
            path = make_pdf(os.path.join(self.tmp.name, f"s{dpi}.pdf"),
                            [("scan", page_image(["Hello"], dpi)), ("text", "Real text " * 10)], dpi=dpi)
            reader = pypdf.PdfReader(path)
            self.assertLess(abs(P.page_dpi(reader.pages[0]) - dpi), 3)
            self.assertEqual(P.page_dpi(reader.pages[1]), 0)

    def test_an_odd_page_has_no_estimate(self) -> None:
        self.assertEqual(P.page_dpi(object()), 0)


class FakeScan(P.ScanPdf):
    """Pages as numbered fake bitmaps; nothing from Windows."""

    def __init__(self) -> None:  # noqa: D107 (no WinRT document)
        self.pages, self.dpi, self.closed = 9, {}, []

    def render(self, number: int, dpi: int = P.TARGET_DPI):
        self.dpi[number] = dpi
        scan = self
        return type("Bitmap", (), {"number": number, "close": lambda _self: scan.closed.append(number)})()


def fake_ocr(bitmap, **_kw) -> OcrResult:
    word = OcrWord(f"page{bitmap.number}", QRectF(0, 0, 10, 10))
    return OcrResult((OcrLine((word,)),), 0.0, "en-US")


class OcrPagesTests(unittest.TestCase):
    def test_pages_in_order_with_progress(self) -> None:
        scan, steps = FakeScan(), []
        with mock.patch("saylight.ocr.engine.recognize_bitmap", side_effect=fake_ocr):
            out = P.ocr_pages(scan, [2, 5, 7], progress=lambda d, n: steps.append((d, n)), cancelled=lambda: False)
        self.assertEqual(out, [OcrText("page2", out[0].words), OcrText("page5", out[1].words),
                               OcrText("page7", out[2].words)])
        self.assertEqual(steps, [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(scan.closed, [2, 5, 7])  # every page's bitmap is let go
        self.assertEqual(scan.dpi, {2: 220, 5: 220, 7: 220})

    def test_cancel_stops_between_pages(self) -> None:
        scan, steps = FakeScan(), []
        with mock.patch("saylight.ocr.engine.recognize_bitmap", side_effect=fake_ocr), self.assertRaises(Cancelled):
            P.ocr_pages(scan, [1, 2, 3], progress=lambda d, n: steps.append(d), cancelled=lambda: len(steps) >= 1)
        self.assertEqual(steps, [1])
        self.assertEqual(scan.closed, [1])


@unittest.skipUnless(winrt_support.available(), "Windows OCR (winrt) is not installed here")
class WindowsPdfTests(unittest.TestCase):
    def test_a_scanned_page_is_read_at_220_dpi(self) -> None:
        from saylight.ocr.engine import user_languages
        if not user_languages():
            self.skipTest("no OCR language on this PC")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = make_pdf(os.path.join(tmp.name, "scan.pdf"), [("scan", page_image(SCAN_LINES, 200))])
        out = {}

        def work():
            try:
                with open(path, "rb") as f:
                    scan = P.ScanPdf(f.read())
                out["texts"] = P.ocr_pages(scan, [1], progress=lambda d, n: None, cancelled=lambda: False)
                out["dpi"], out["pages"] = scan.dpi, scan.pages
            except Exception as e:  # noqa: BLE001
                out["error"] = e
        th = threading.Thread(target=work)
        th.start()
        th.join(60)
        self.assertNotIn("error", out)
        self.assertEqual(out["pages"], 1)
        self.assertLess(abs(out["dpi"][1] - 220), 3)  # not 275 on a 125% display
        text = " ".join(out["texts"][0].text.split())
        self.assertIn("Reading aloud while the words are marked", text)
        self.assertIn("only a picture of text.", text)


if __name__ == "__main__":
    unittest.main()
