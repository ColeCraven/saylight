"""Tests for the types and interfaces reading from the screen, scanned PDFs and the reading ruler share: the OCR
model, the document model's additions, and the shape of each module's public interface (names, signals and
parameters), so the pieces keep fitting together. A subprocess checks that none of them imports winrt on import.
"""

import dataclasses
import inspect
import os
import subprocess
import sys
import typing
import unittest

from PySide6.QtCore import QRect, QRectF

from saylight.documents.model import LOAD_ERROR_KINDS, DocSource, LoadedDocument, LoadError, RawInput
from saylight.ocr import model as M
from saylight.ocr.model import OcrLine, OcrResult, OcrText, OcrWord, ScreenText

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULES = ("saylight.winrt_support", "saylight.ocr", "saylight.ocr.engine", "saylight.ocr.layout",
           "saylight.screen_capture", "saylight.win32_overlay", "saylight.screen_reader",
           "saylight.ui.region_overlay", "saylight.ui.screen_highlight", "saylight.ui.reading_ruler",
           "saylight.ui.page_range", "saylight.documents.pdf_ocr", "saylight.documents.loader",
           "saylight.input_thread", "saylight.engine")

# module, attribute ("Class.method" or a function), the parameters it starts with (after self)
INTERFACES = (
    ("saylight.winrt_support", "available", ()),
    ("saylight.winrt_support", "load", ("namespace",)),
    ("saylight.ocr.engine", "OcrWorker.start", ()),
    ("saylight.ocr.engine", "OcrWorker.recognize", ("token", "image", "origin", "dpr", "upscale")),
    ("saylight.ocr.engine", "OcrWorker.languages", ()),
    ("saylight.ocr.engine", "OcrWorker.refresh_languages", ()),
    ("saylight.ocr.engine", "OcrWorker.shutdown", ("timeout",)),
    ("saylight.ocr.engine", "recognize_blocking", ("image", "upscale")),
    ("saylight.ocr.layout", "to_text", ("result",)),
    ("saylight.ocr.layout", "map_boxes", ("doc", "text")),
    ("saylight.screen_capture", "grab", ("screen", "rect")),
    ("saylight.screen_capture", "thumb", ("screen", "rect")),
    ("saylight.win32_overlay", "exclude_from_capture", ("hwnd",)),
    ("saylight.win32_overlay", "cursor_pos", ()),
    ("saylight.ui.region_overlay", "OverlayManager.open", ("pill_detail",)),
    ("saylight.ui.region_overlay", "OverlayManager.close", ()),
    ("saylight.ui.screen_highlight", "ScreenHighlight.__init__", ("theme", "settings", "parent")),
    ("saylight.ui.screen_highlight", "ScreenHighlight.show_for", ("source",)),
    ("saylight.ui.screen_highlight", "ScreenHighlight.set_word", ("sentence", "lo", "hi")),
    ("saylight.ui.screen_highlight", "ScreenHighlight.hide", ()),
    ("saylight.ui.screen_highlight", "ScreenHighlight.show_frame", ("screen", "rect")),
    ("saylight.ui.screen_highlight", "ScreenHighlight.hide_frame", ("animate",)),
    ("saylight.ui.screen_highlight", "ScreenHighlight.set_paused", ("on",)),
    ("saylight.screen_reader", "ScreenReader.begin", ("return_to",)),
    ("saylight.screen_reader", "ScreenReader.cancel", ("reason",)),
    ("saylight.screen_reader", "ScreenReader.selecting", ()),
    ("saylight.documents.pdf_ocr", "ocr_pages", ("data", "pages", "progress", "cancelled", "target_dpi")),
    ("saylight.documents.loader", "DocumentLoader.continue_scan", ("token", "first", "last")),
    ("saylight.ui.page_range", "PageRangeWindow.open_at", ("rest", "pages", "scanned_in", "return_to")),
    ("saylight.ui.page_range", "PageRangeWindow.close_animated", ("reason",)),
    ("saylight.ui.page_range", "PageRangeWindow.reanchor", ("rest",)),
    ("saylight.ui.page_range", "clamp_range", ("a", "b", "n")),
    ("saylight.ui.page_range", "range_note", ("n_pages", "n_scanned")),
    ("saylight.ui.reading_ruler", "ReadingRuler.__init__", ("theme", "settings", "own_rects", "parent")),
    ("saylight.ui.reading_ruler", "ReadingRuler.set_enabled", ("on",)),
    ("saylight.ui.reading_ruler", "ReadingRuler.apply_settings", ()),
    ("saylight.ui.reading_ruler", "ReadingRuler.suppress", ("reason", "on")),
    ("saylight.input_thread", "InputThread.set_escape_hotkey", ("on",)),
    ("saylight.input_thread", "InputThread.set_overlay_open", ("on",)),
    ("saylight.engine", "Engine.begin_opening", ("title", "kind")),
    ("saylight.engine", "Engine.opening_progress", ("token", "done", "total", "phase")),
    ("saylight.engine", "Engine.opening_choice", ("token", "scanned", "pages", "est_s")),
    ("saylight.engine", "Engine.opening_failed", ("token", "kind")),
    ("saylight.engine", "Engine.ocr_notice", ("kind",)),
    ("saylight.engine", "Engine.hold_overlay", ("on",)),
)

SIGNALS = (
    ("saylight.ocr.engine", "OcrWorker", ("done", "failed")),
    ("saylight.ui.region_overlay", "OverlayManager", ("selected", "cancelled")),
    ("saylight.ui.screen_highlight", "ScreenHighlight", ("pageMoved",)),
    ("saylight.screen_reader", "ScreenReader", ("stateChanged", "textReady", "failed")),
    ("saylight.ui.page_range", "PageRangeWindow", ("rangeChosen", "closed")),
    ("saylight.ui.reading_ruler", "ReadingRuler", ("toggled",)),
    ("saylight.documents.loader", "DocumentLoader", ("scanChoice",)),
    ("saylight.input_thread", "InputThread", ("overlayCancelPressed",)),
)


def resolve(module: str, path: str):
    obj = __import__(module, fromlist=["_"])
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


class OcrModelTests(unittest.TestCase):
    def test_types_are_frozen(self) -> None:
        word = OcrWord("Hello", QRectF(1, 2, 30, 12))
        line = OcrLine((word,))
        result = OcrResult((line,), 0.0, "en-US")
        text = OcrText("Hello", (word,))
        for value in (word, line, result, text):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                value.__setattr__(dataclasses.fields(value)[0].name, None)
        self.assertEqual(result.lines[0].words[0].text, "Hello")

    def test_screen_text_keeps_no_pixels_or_text(self) -> None:
        boxes = {0: [(0, 5, QRectF(10, 20, 40, 14))]}
        source = ScreenText("DISPLAY1", QRect(0, 0, 200, 100), boxes, b"\x01\x02")
        self.assertTrue(source.light)  # a light page unless the capture said otherwise
        self.assertEqual([f.name for f in dataclasses.fields(ScreenText)],
                         ["screen", "region", "boxes", "thumb", "light"])
        self.assertFalse(dataclasses.replace(source, light=False).light)

    def test_failure_and_notice_kinds(self) -> None:
        self.assertEqual(M.OCR_FAILURES, ("no_language", "too_large", "error"))
        self.assertEqual(M.OCR_NOTICE_KINDS, ("screen_nothing", "screen_too_large", "screen_language",
                                              "screen_error", "scan_nothing", "scan_language", "scan_error"))


class DocumentModelTests(unittest.TestCase):
    def test_scan_dpi_defaults_to_no_ocr(self) -> None:
        self.assertEqual(LoadedDocument("pdf", "t", "x").scan_dpi, 0)
        self.assertEqual(LoadedDocument("pdf", "t", "x", scan_dpi=140).scan_dpi, 140)

    def test_ocr_text_is_a_raw_input(self) -> None:
        self.assertEqual(set(typing.get_args(RawInput)), {str, LoadedDocument, OcrText})

    def test_a_screen_source(self) -> None:
        screen = ScreenText("DISPLAY1", QRect(0, 0, 10, 10), {}, b"")
        self.assertIs(DocSource("screen", screen=screen).screen, screen)

    def test_scan_failures_are_load_errors(self) -> None:
        for kind in M.SCAN_NOTICES:
            self.assertIn(kind, LOAD_ERROR_KINDS)
            self.assertEqual(LoadError(kind).kind, kind)
        self.assertIn("scanned", LOAD_ERROR_KINDS)  # still raised by the PDF reader itself
        with self.assertRaises(ValueError):
            LoadError("screen_error")  # screen notices never come from a file


class InterfaceTests(unittest.TestCase):
    def test_parameters(self) -> None:
        for module, path, names in INTERFACES:
            with self.subTest(f"{module}.{path}"):
                params = [p for p in inspect.signature(resolve(module, path)).parameters if p != "self"]
                self.assertEqual(tuple(params[:len(names)]), names)

    def test_signals(self) -> None:
        from PySide6.QtCore import Signal
        for module, cls, names in SIGNALS:
            for name in names:
                with self.subTest(f"{cls}.{name}"):
                    self.assertIsInstance(resolve(module, f"{cls}.{name}"), Signal)

    def test_hotkey_ids(self) -> None:
        from saylight import input_thread as I
        self.assertEqual((I.HK_READ_SCREEN, I.HK_RULER, I.HK_ESCAPE), (5, 6, 20))
        ids = (I.HK_READ, I.HK_PANEL, I.HK_PAUSE, I.HK_COMPOSE, I.HK_READ_SCREEN, I.HK_RULER, I.HK_ESCAPE,
               *I.MEDIA_KEYS)
        self.assertEqual(len(set(ids)), len(ids))

    def test_nothing_imports_winrt_on_import(self) -> None:
        """winrt must load after PySide6, and only winrt_support.py loads it, on first use."""
        code = ("import sys\n"
                f"for name in {MODULES!r}:\n"
                "    __import__(name)\n"
                "print(sorted(m for m in sys.modules if m == 'winrt' or m.startswith('winrt.')))\n")
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
