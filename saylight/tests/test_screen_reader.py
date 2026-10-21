"""Tests for reading from the screen: screen_reader.py (the flow, the language pre-check and the failure mapping),
screen_capture.py (the lightness flag and the page-moved check) and ui/screen_highlight.py (the marks' geometry,
the frame, and hiding when the page moves).

OCR, the overlay, the input thread and the screen grab are fakes: nothing is captured and no key is registered.
The mark window is real but never reaches the screen (WA_DontShowOnScreen). Reduced motion is on.
"""

import sys
import types
import unittest
from unittest import mock

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QApplication

from saylight import screen_capture as C
from saylight import screen_reader as SR
from saylight.documents.model import DocSource
from saylight.input_thread import HK_ESCAPE, HK_READ
from saylight.ocr.model import OcrLine, OcrResult, OcrText, OcrWord, ScreenText
from saylight.tests.test_region_overlay import Settings, make_theme
from saylight.ui import screen_highlight as SH

_app = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


# --- Fakes ---------------------------------------------------------------------------------------------------------

class FakeOcr(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, langs=("en-US",)):
        super().__init__()
        self.langs = list(langs)
        self.refreshed = 0
        self.calls = []

    def languages(self):
        return list(self.langs)

    def refresh_languages(self):
        self.refreshed += 1
        return list(self.langs)

    def recognize(self, token, image, *, origin, dpr, upscale=2.0):
        self.calls.append((token, image.size(), QPointF(origin), dpr, upscale))


class FakeInput(QObject):
    overlayCancelPressed = Signal(str)
    registered = Signal(int, bool)

    def __init__(self):
        super().__init__()
        self.log = []
        self.hwnds = []

    def set_overlay_open(self, on, hwnds=()):
        self.log.append(("overlay", on))
        self.hwnds = list(hwnds)

    def set_escape_hotkey(self, on):
        self.log.append(("escape", on))


class FakeOverlay(QObject):
    selected = Signal(object, QRect)
    cancelled = Signal(str)

    def __init__(self, excluded=True, can_activate=True):
        super().__init__()
        self.open_ = False
        self.excluded = excluded
        self.can_activate = can_activate
        self.activated = None
        self.activated_hwnd = 0
        self.log = []

    def open(self, *, pill_detail="esc"):
        self.open_ = True
        self.log.append(("open", pill_detail))

    def close(self):
        self.open_ = False
        self.activated = None
        self.log.append(("close",))

    def is_open(self):
        return self.open_

    def windows(self):
        return [types.SimpleNamespace(winId=lambda: 501)] if self.open_ else []

    def capture_excluded(self):
        return self.excluded

    def activate(self):
        self.log.append(("activate",))
        if self.can_activate:
            self.activated, self.activated_hwnd = object(), 4242
        return self.can_activate

    def set_pill_detail(self, detail):
        self.log.append(("detail", detail))


class FakeHighlight:
    def __init__(self):
        self.log = []

    def show_frame(self, screen, rect):
        self.log.append(("frame", QRect(rect)))

    def hide_frame(self, animate=True):
        self.log.append(("hide_frame", animate))


def ocr_result(*lines: list[tuple[str, QRectF]]) -> OcrResult:
    return OcrResult(tuple(OcrLine(tuple(OcrWord(t, r) for t, r in line)) for line in lines), 0.0, "en-US")


def white_image(w=250, h=125, color="#FFFFFF", dpr=1.25) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    img.setDevicePixelRatio(dpr)
    return img


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.theme = make_theme()
        self.ocr = FakeOcr()
        self.input = FakeInput()
        self.overlay = FakeOverlay()
        self.highlight = FakeHighlight()
        self.reader = SR.ScreenReader(self.theme, Settings(), self.ocr, input_thread=self.input,
                                      overlay=self.overlay, highlight=self.highlight)
        self.events = []
        self.reader.stateChanged.connect(lambda s: self.events.append(("state", s)))
        self.reader.failed.connect(lambda k: self.events.append(("failed", k)))
        self.reader.textReady.connect(lambda doc, text: self.events.append(("ready", doc, text)))
        self.screen = QGuiApplication.primaryScreen()
        self.image = white_image()
        for name, value in (("grab", lambda screen, rect: self.image), ("thumb", lambda screen, rect: b"\x02\x01ab")):
            patcher = mock.patch.object(SR.screen_capture, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def select(self, rect=QRect(40, 60, 200, 100)):
        self.reader.begin(return_to=77)
        self.overlay.selected.emit(self.screen, rect)

    def failures(self):
        return [e[1] for e in self.events if e[0] == "failed"]


class FlowTests(ReaderCase):
    def test_no_language_skips_the_overlay(self):
        self.ocr.langs = []
        with self.assertLogs("saylight.screen_reader", "INFO") as logs:
            self.reader.begin()
        self.assertEqual(self.failures(), ["no_language"])
        self.assertEqual((self.overlay.log, self.input.log), ([], []))
        self.assertEqual(self.reader.state, "idle")
        self.assertIn("Screen: no OCR language", "\n".join(logs.output))

    def test_language_ready_asks_again_on_retry(self):
        self.assertTrue(self.reader.language_ready())
        self.assertEqual(self.ocr.refreshed, 0)
        self.ocr.langs = []
        self.assertFalse(self.reader.language_ready(refresh=True))
        self.assertEqual(self.ocr.refreshed, 1)

    def test_begin_gates_the_shortcuts_registers_esc_and_opens(self):
        self.reader.begin(return_to=77)
        # gated before it opens, then its windows are handed over to be watched in case the app hangs
        self.assertEqual(self.input.log, [("overlay", True), ("escape", True), ("overlay", True)])
        self.assertEqual(self.input.hwnds, [501])
        self.assertEqual(self.overlay.log, [("open", "esc")])
        self.assertTrue(self.reader.selecting())
        self.assertEqual(self.events, [("state", "selecting")])
        self.reader.begin()  # already selecting: ignored
        self.assertEqual(self.overlay.log, [("open", "esc")])

    def test_any_shortcut_or_esc_cancels_and_releases_everything(self):
        for reason in ("hotkey", "esc"):
            self.events.clear()
            self.input.log.clear()
            self.reader.begin()
            with self.assertLogs("saylight.screen_reader", "INFO") as logs:
                self.input.overlayCancelPressed.emit(reason)
            self.assertFalse(self.overlay.open_)
            self.assertEqual(self.input.log[-2:], [("overlay", False), ("escape", False)])
            self.assertEqual(self.events[-2:], [("state", "idle"), ("failed", "cancelled")])
            self.assertIn(f"Screen: cancelled ({reason})", "\n".join(logs.output))

    def test_overlay_cancel_reasons_pass_through(self):
        self.reader.begin()
        with self.assertLogs("saylight.screen_reader", "INFO") as logs:
            self.overlay.cancelled.emit("timeout")
        self.assertEqual(self.failures(), ["cancelled"])
        self.assertIn("Screen: cancelled (timeout)", "\n".join(logs.output))

    def test_esc_held_by_another_app_says_right_click_and_takes_no_focus(self):
        self.reader.begin(return_to=77)
        self.input.registered.emit(HK_READ, False)  # another shortcut: nothing to do with the overlay
        self.assertEqual(self.overlay.log, [("open", "esc")])
        self.input.registered.emit(HK_ESCAPE, False)
        self.assertEqual(self.overlay.log[-1], ("detail", "right_click"))
        # that app gets every Esc press whatever is in front, so taking the foreground would gain nothing
        self.assertNotIn(("activate",), self.overlay.log)
        with mock.patch.object(SR.win32_overlay, "give_back_foreground") as give_back:
            self.reader.cancel("right_click")
        give_back.assert_not_called()

    def test_an_overlay_that_fails_to_open_releases_the_shortcuts_and_esc(self):
        def fail(*, pill_detail="esc"):
            raise RuntimeError("no window")
        self.overlay.open = fail
        with self.assertLogs("saylight.screen_reader", "ERROR"):
            self.reader.begin(return_to=77)
        self.assertEqual(self.input.log[-2:], [("overlay", False), ("escape", False)])  # Ctrl+` and Esc work again
        self.assertFalse(self.reader.busy())
        self.assertEqual(self.failures(), ["error"])  # the App uncovers the panel and says so

    def test_release_grabs_with_the_overlay_up_then_shows_the_frame_and_recognises(self):
        order = []
        self.overlay.close = lambda: (order.append("close"), setattr(self.overlay, "open_", False))
        with mock.patch.object(SR.screen_capture, "grab", lambda s, r: (order.append("grab"), self.image)[1]):
            self.select(QRect(40, 60, 200, 100))
        self.assertEqual(order, ["grab", "close"])
        self.assertEqual(self.highlight.log, [("frame", QRect(40, 60, 200, 100))])
        self.assertEqual(self.reader.state, "recognizing")
        self.assertEqual(self.events[-1], ("state", "recognizing"))
        token, size, origin, dpr, upscale = self.ocr.calls[0]
        self.assertEqual((size.width(), size.height(), origin, dpr, upscale), (250, 125, QPointF(40, 60), 1.25, 2.0))
        self.assertEqual(self.input.log[-2:], [("overlay", False), ("escape", False)])

    def test_text_becomes_a_reading_with_its_screen_source(self):
        result = ocr_result([("Hello", QRectF(45, 62, 40, 12)), ("there.", QRectF(90, 62, 44, 12))])
        boxes = {0: [(0, 5, QRectF(45, 62, 40, 12)), (6, 12, QRectF(90, 62, 44, 12))]}
        self.select()
        token = self.ocr.calls[0][0]
        with mock.patch.object(SR.layout, "to_text", return_value=OcrText("Hello there.", ())), \
                mock.patch.object(SR.layout, "map_boxes", return_value=boxes), \
                self.assertLogs("saylight.screen_reader", "INFO") as logs:
            self.ocr.done.emit(token, result)
        kind, doc, text = self.events[-1]
        self.assertEqual(kind, "ready")
        self.assertEqual(text.text, "Hello there.")
        self.assertEqual(doc.text, "Hello there.")
        self.assertIsInstance(doc.source, DocSource)
        src = doc.source.screen
        self.assertEqual((doc.source.kind, src.screen, src.region, src.boxes, src.thumb, src.light),
                         ("screen", self.screen.name(), QRect(40, 60, 200, 100), boxes, b"\x02\x01ab", True))
        self.assertEqual(self.reader.state, "idle")
        line = "\n".join(logs.output)
        self.assertIn("Screen: captured 250x125 px at 1.25, OCR ", line)
        self.assertIn("1 lines, 12 characters", line)
        self.assertNotIn("Hello", line)   # counts only, never text

    def test_a_dark_capture_is_flagged(self):
        self.image = white_image(color="#1E1E1E")
        self.select()
        with mock.patch.object(SR.layout, "to_text", return_value=OcrText("Hi.", ())), \
                mock.patch.object(SR.layout, "map_boxes", return_value={}):
            self.ocr.done.emit(self.ocr.calls[0][0], ocr_result([("Hi.", QRectF(0, 0, 9, 9))]))
        self.assertFalse(self.events[-1][1].source.screen.light)

    def test_no_words_is_nothing_found(self):
        self.select()
        self.ocr.done.emit(self.ocr.calls[0][0], ocr_result())
        self.assertEqual(self.failures(), ["nothing_found"])
        self.assertEqual(self.highlight.log[-1], ("hide_frame", True))
        self.assertEqual(self.reader.state, "idle")

    def test_nothing_to_say_is_nothing_speakable(self):
        self.select()
        with mock.patch.object(SR.layout, "to_text", return_value=OcrText("   ", ())):
            self.ocr.done.emit(self.ocr.calls[0][0], ocr_result([("|", QRectF(0, 0, 2, 9))]))
        self.assertEqual(self.failures(), ["nothing_speakable"])

    def test_worker_failures_map_to_notices(self):
        for kind, expected in (("no_language", "no_language"), ("too_large", "too_large"), ("error", "error"),
                               ("odd", "error")):
            self.events.clear()
            self.ocr.calls.clear()
            self.select()
            self.ocr.failed.emit(self.ocr.calls[0][0], kind)
            self.assertEqual(self.failures(), [expected])
        self.assertEqual({k: SR.notice_for(k) for k in SR.FAILURES},
                         {"no_language": "screen_language", "nothing_found": "screen_nothing",
                          "cancelled": None, "error": "screen_error", "too_large": "screen_too_large",
                          "nothing_speakable": "screen_nothing"})

    def test_cancel_while_recognising_drops_the_result(self):
        self.select()
        token = self.ocr.calls[0][0]
        self.reader.cancel()
        self.assertEqual(self.failures(), ["cancelled"])
        self.assertEqual(self.highlight.log[-1], ("hide_frame", False))   # the frame goes at once
        self.ocr.done.emit(token, ocr_result([("Late", QRectF(0, 0, 9, 9))]))
        self.ocr.failed.emit(token, "error")
        self.assertEqual(self.failures(), ["cancelled"])
        self.reader.cancel()   # idle: nothing
        self.assertEqual(self.failures(), ["cancelled"])

    def test_stale_tokens_are_ignored(self):
        self.select()
        self.ocr.done.emit(self.ocr.calls[0][0] + 5, ocr_result())
        self.assertEqual(self.reader.state, "recognizing")

    def test_without_capture_exclusion_the_overlay_hides_before_the_grab(self):
        self.overlay.excluded = False
        grabs = []
        def grab(_screen, _rect):
            grabs.append(self.overlay.open_)
            return self.image
        with mock.patch.object(SR.screen_capture, "grab", grab), \
                mock.patch.object(SR.QTimer, "singleShot", lambda ms, ctx, fn: fn()):
            self.select()
        self.assertEqual(grabs, [False])
        self.assertEqual(self.reader.state, "recognizing")
        self.assertEqual(len(self.ocr.calls), 1)

    def test_cancel_during_the_hide_before_the_grab_stops_it(self):
        self.overlay.excluded = False
        pending = []
        with mock.patch.object(SR.QTimer, "singleShot", lambda ms, ctx, fn: pending.append(fn)):
            self.select()
        self.reader.cancel()
        pending[0]()
        self.assertEqual(self.ocr.calls, [])
        self.assertEqual(self.failures(), ["cancelled"])


# --- Capture: lightness and the page-moved check ------------------------------------------------------------------

def page(shift: int = 0, caret: bool = False, w: int = 640, h: int = 400) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("#FFFFFF"))
    p = QPainter(img)
    p.setPen(QColor("#1A1A1A"))
    f = QFont("Georgia")
    f.setPixelSize(15)
    p.setFont(f)
    words = ("Working memory capacity predicts reading comprehension in adults with dyslexia and the effect "
             "was strongest for longer texts these findings suggest that bimodal presentation reduces load").split()
    for row in range(-2, 22):
        line = " ".join(words[(row * 5 + i) % len(words)] for i in range(9 + row % 3))
        p.drawText(QPointF(12, 22 + row * 22 - shift), line)
    if caret:
        p.fillRect(QRect(300, 100, 1, 18), QColor("#000000"))
    p.end()
    return img


class CaptureTests(unittest.TestCase):
    def test_lightness(self):
        self.assertTrue(C.is_light(page()))
        self.assertTrue(C.is_light(white_image(color="#808080")))
        self.assertFalse(C.is_light(white_image(color="#7A7A7A")))
        self.assertFalse(C.is_light(white_image(color="#1E1E1E")))
        self.assertTrue(C.is_light(QImage()))

    def test_check_rect_is_capped_centred_and_on_whole_device_pixels(self):
        r = C.check_rect(QRect(10, 20, 2003, 999))
        self.assertEqual((r.width(), r.height()), C.CHECK_MAX)
        self.assertEqual(((r.x() - 10) % 4, (r.y() - 20) % 4), (0, 0))
        self.assertAlmostEqual(r.center().x(), 10 + 2003 / 2, delta=4)
        small = C.check_rect(QRect(5, 5, 101, 37))
        self.assertEqual((small.x(), small.y(), small.width(), small.height()), (5, 5, 100, 36))

    def test_thumb_is_a_coarse_grid(self):
        t = C.thumb_of(page())
        cols, rows = t[0], t[1]
        self.assertEqual((cols, rows), (24, 15))
        self.assertEqual(len(t), 2 + cols * rows)
        self.assertEqual(C.grid_size(100, 400), (6, 24))
        self.assertEqual(C.thumb_of(QImage()), b"")

    def test_same_page_and_a_blinking_caret_are_not_moves(self):
        base = C.thumb_of(page())
        self.assertFalse(C.changed(base, C.thumb_of(page())))
        self.assertFalse(C.changed(base, C.thumb_of(page(caret=True))))

    def test_scrolling_one_line_or_less_is_a_move(self):
        base = C.thumb_of(page())
        for shift in (22, 11, 6):
            with self.subTest(shift=shift):
                self.assertTrue(C.changed(base, C.thumb_of(page(shift=shift))))
        self.assertTrue(C.changed(base, b""))
        self.assertTrue(C.changed(base, C.thumb_of(page(w=300))))  # a different shape

    def test_cells_under_own_windows_are_left_out(self):
        area = QRect(1000, 500, 640, 400)
        base = C.thumb_of(page())
        moved = C.thumb_of(page(shift=22))
        everything = C.cells_under(area, base, [QRect(900, 400, 1000, 700)])
        self.assertEqual(len(everything), 24 * 15)
        self.assertFalse(C.changed(base, moved, skip=everything))
        corner = C.cells_under(area, base, [QRect(1000, 500, 30, 30)])
        self.assertEqual(corner, frozenset({0, 1, 24, 25}))
        self.assertEqual(C.cells_under(area, base, [QRect(0, 0, 10, 10)]), frozenset())


# --- Marks ---------------------------------------------------------------------------------------------------------

def words_on_lines() -> list[tuple[int, int, QRectF]]:
    """Two lines of words (tight ink boxes, varied heights) and a third, as OCR reports them."""
    out, pos = [], 0
    for top, xs in ((100, (10, 70, 140)), (130, (10, 90)), (161, (10,))):
        for i, x in enumerate(xs):
            h = 12 if i % 2 else 9
            out.append((pos, pos + 4, QRectF(x, top + (12 - h), 50, h)))
            pos += 5
    return out


class MarkGeometryTests(unittest.TestCase):
    def setUp(self):
        self.t = make_theme()

    def test_words_group_into_visual_lines(self):
        rects = [r for _a, _b, r in words_on_lines()]
        self.assertEqual(SH.visual_lines(rects), [[0, 1, 2], [3, 4], [5]])
        # a word overlapping the line by less than half its height starts a new one
        self.assertEqual(SH.visual_lines([QRectF(0, 0, 10, 10), QRectF(20, 6, 10, 10)]), [[0], [1]])
        self.assertEqual(SH.visual_lines([QRectF(0, 0, 10, 10), QRectF(20, 5, 10, 10)]), [[0, 1]])

    def test_find_word(self):
        words = words_on_lines()
        self.assertEqual(SH.find_word(words, 5, 9), 1)
        self.assertEqual(SH.find_word(words, 7, 8), 1)
        self.assertIsNone(SH.find_word(words, -1, -1))
        self.assertIsNone(SH.find_word(words, 4, 4))

    def test_padding_and_line_heights(self):
        geo = SH.mark_geometry(self.t, words_on_lines(), QPoint(5, 90))
        first = geo.lines[0]
        # the line's ink union is 100..112, grown 3 above and below; its words 10..190, grown 4 across
        self.assertEqual((first.left(), first.top(), first.right(), first.bottom()), (10 - 4 - 5, 100 - 3 - 90,
                                                                                      190 + 4 - 5, 112 + 3 - 90))
        word = geo.words[1]
        self.assertEqual((word.left(), word.width(), word.top(), word.height()), (70 - 3 - 5, 56, first.top(), 18))
        self.assertEqual(geo.word_line, [0, 0, 0, 1, 1, 2])

    def test_band_paints_without_doubling_under_the_word(self):
        t = self.t
        geo = SH.mark_geometry(t, words_on_lines())
        img = QImage(300, 220, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        SH.paint_marks(p, t, "word", True, geo.band, geo.words[1])
        p.end()
        band_px = img.pixelColor(30, 106)   # band only
        word_px = img.pixelColor(95, 106)   # the word
        self.assertAlmostEqual(band_px.alphaF(), 0.16, delta=0.01)
        self.assertAlmostEqual(word_px.alphaF(), 0.40, delta=0.01)
        mark = t.screen_mark_color(True)
        for got, want in zip((word_px.red(), word_px.green(), word_px.blue()), (mark.red(), mark.green(), mark.blue())):
            self.assertAlmostEqual(got, want, delta=2)   # premultiplied rounding
        self.assertEqual(img.pixelColor(30, 124).alpha(), 0)     # between lines the page stays clear

    def test_sentence_mode_and_paused_and_off(self):
        t = self.t
        geo = SH.mark_geometry(t, words_on_lines())

        def alpha_at(mode, paused=0.0, x=95):
            img = QImage(300, 220, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            SH.paint_marks(p, t, mode, True, geo.band, geo.words[1], paused_mix=paused)
            p.end()
            return img.pixelColor(x, 106).alphaF()
        self.assertAlmostEqual(alpha_at("sentence"), 0.24, delta=0.01)
        self.assertAlmostEqual(alpha_at("word", 1.0), 0.20, delta=0.01)
        self.assertEqual(alpha_at("off"), 0.0)

    def test_dark_pages_use_soft_darks_pill(self):
        t = self.t
        geo = SH.mark_geometry(t, words_on_lines())
        img = QImage(300, 220, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        SH.paint_marks(p, t, "word", False, geo.band, geo.words[1])
        p.end()
        px = img.pixelColor(95, 106)
        for got, want in zip((px.red(), px.green(), px.blue()), (0x36, 0x52, 0x7F)):
            self.assertAlmostEqual(got, want, delta=2)

    def test_high_contrast_outlines_only(self):
        t = make_theme(hc=True)
        geo = SH.mark_geometry(t, words_on_lines())
        img = QImage(300, 220, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        SH.paint_marks(p, t, "word", True, geo.band, geo.words[1])
        p.end()
        self.assertEqual(img.pixelColor(95, 106).alpha(), 0)   # no fill inside the word
        self.assertEqual(img.pixelColor(30, 106).alpha(), 0)   # nor the band
        word = geo.words[1]
        self.assertEqual(img.pixelColor(int(word.center().x()), int(word.top()) + 1), t.color("Accent"))


class OffscreenMarks(SH.MarkWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)


class HighlightTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(SH, "MarkWindow", OffscreenMarks)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = Settings()
        self.theme = make_theme()
        self.hl = SH.ScreenHighlight(self.theme, self.settings)
        self.addCleanup(lambda: self.hl.hide(animate=False))
        self.screen = QGuiApplication.primaryScreen()
        g = self.screen.geometry()
        self.area = QRect(g.x() + 40, g.y() + 60, 260, 100)
        shifted = [(a, b, QRectF(r).translated(self.area.x() - 5, self.area.y() - 95))
                   for a, b, r in words_on_lines()]
        second = [(a - 15, b - 15, r) for a, b, r in shifted[3:]]   # offsets are per sentence
        self.source = ScreenText(self.screen.name(), self.area, {0: shifted[:3], 1: second}, b"\x01\x01\x80")
        self.moves = []
        self.hl.pageMoved.connect(self.moves.append)

    def window(self):
        return self.hl._window

    def test_frame_then_marks_on_the_first_word(self):
        shown = []
        self.hl.shown.connect(lambda: shown.append(1))
        self.hl.show_frame(self.screen, self.area)
        w = self.window()
        m = self.theme.px(SH.MARGIN)
        self.assertTrue(w.isVisible())
        self.assertEqual(w.geometry(), self.area.adjusted(-m, -m, m, m))
        self.assertTrue(w.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertEqual(shown, [1])
        self.assertEqual((self.hl._frame.value, self.hl._marks.value), (1.0, 0.0))
        self.hl.show_for(self.source)
        self.assertEqual(self.hl._frame.value, 1.0)       # still finding: the frame stays until the first word
        self.hl.set_word(0, 5, 9)
        self.assertEqual((self.hl._frame.value, self.hl._marks.value), (0.0, 1.0))
        self.assertTrue(w.isVisible(), "the frame's fade hid the window before the marks' fade began")
        self.assertEqual(self.hl._word, 1)
        self.hl.set_word(0, 10, 14)
        self.assertTrue(w.isVisible())   # and they stay for the rest of the reading

    def test_a_whole_screen_area_stays_short_of_full_screen(self):
        # Windows takes a topmost window covering its monitor for a full-screen app: the panel would hide
        g = self.screen.geometry()
        self.hl.show_frame(self.screen, g)
        self.assertFalse(self.window().geometry().contains(g))
        self.assertEqual(self.window().geometry().bottom(), g.bottom() - 1)

    def test_words_and_sentences_follow_the_engine(self):
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.hl.set_word(0, 0, 4)
        self.hl.set_word(0, 10, 14)          # the same line: the mark slides
        target = self.hl._geo.words[2]
        self.assertEqual((self.hl._word, self.hl._wx.value, self.hl._ww.value), (2, target.left(), target.width()))
        self.hl.set_word(1, 5, 9)             # next sentence
        self.assertEqual((self.hl._sentence, self.hl._word), (1, 1))
        self.hl.set_word(1, -1, -1)           # between words: the band stays
        self.assertIsNone(self.hl._word)
        self.hl.set_paused(True)
        self.assertEqual(self.hl._paused.value, 1.0)

    def test_hide_and_show_again_for_the_same_reading(self):
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.hl.set_word(0, 0, 4)
        self.hl.hide()
        self.assertFalse(self.window().isVisible())
        with mock.patch.object(SH.screen_capture, "thumb", return_value=b"\x01\x01\x80"):
            self.hl.show_for(self.source)
            self.hl.set_word(0, 0, 4)
        self.assertTrue(self.window().isVisible())
        self.assertEqual(self.hl._marks.value, 1.0)

    def test_settings_turn_the_marks_off_and_on(self):
        self.settings.set("screen.highlight", False)
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.assertFalse(self.window().isVisible())   # marks off: the frame goes when reading starts
        self.hl.set_word(0, 0, 4)
        self.assertFalse(self.window().isVisible())
        self.settings.set("screen.highlight", True)
        self.assertTrue(self.window().isVisible())
        self.settings.set("highlight", "off")
        self.assertFalse(self.window().isVisible())

    def test_page_moved_hides_and_coming_back_shows(self):
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.hl.set_word(0, 0, 4)
        self.window().excluded = True
        with mock.patch.object(SH.screen_capture, "thumb", return_value=b"\x01\x01\x10"), \
                self.assertLogs("saylight.screen_highlight", "INFO") as logs:
            self.hl._check()
            self.hl._check()   # still moved: said once
        self.assertEqual(self.moves, [True])
        self.assertTrue(self.hl.page_moved())
        self.assertFalse(self.window().isVisible())
        self.assertIn("Screen highlight: hidden (page moved)", "\n".join(logs.output))
        with mock.patch.object(SH.screen_capture, "thumb", return_value=b"\x01\x01\x80"):
            self.hl._check()
        self.assertEqual(self.moves, [True, False])
        self.assertTrue(self.window().isVisible())

    def test_own_windows_over_the_area_dont_count_as_moves(self):
        self.hl.set_own_rects(lambda: [self.area.adjusted(-50, -50, 50, 50)])
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.hl.set_word(0, 0, 4)
        with mock.patch.object(SH.screen_capture, "thumb", return_value=b"\x01\x01\x10"):
            self.hl._check()
        self.assertEqual(self.moves, [])

    def test_check_is_off_without_capture_exclusion(self):
        self.hl.show_frame(self.screen, self.area)
        self.window().excluded = False
        with self.assertLogs("saylight.screen_highlight", "INFO") as logs:
            self.hl.show_for(self.source)
        self.assertFalse(self.hl._timer.isActive())
        self.assertIn("change check off", "\n".join(logs.output))

    def test_check_runs_once_a_second_while_reading(self):
        self.hl.show_frame(self.screen, self.area)
        self.window().excluded = True
        self.hl.show_for(self.source)
        self.assertTrue(self.hl._timer.isActive())
        self.assertEqual(self.hl._timer.interval(), 1000)
        self.hl.hide()
        self.assertFalse(self.hl._timer.isActive())

    def test_forget_drops_the_source(self):
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.hl.forget()
        self.assertIsNone(self.hl._source)
        self.hl.set_word(0, 0, 4)
        self.assertFalse(self.window().isVisible())

    def test_the_page_check_survives_its_screen_going_away(self):
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.hl.set_word(0, 0, 4)
        self.window().excluded = True
        # what QScreen.geometry() raises once Qt has deleted a removed screen
        gone = RuntimeError("Internal C++ object (PySide6.QtGui.QScreen) already deleted.")
        with mock.patch.object(SH.screen_capture, "thumb", side_effect=gone), self.assertLogs(SH.log, "INFO"):
            self.hl._check()  # runs once a second while reading: it must not raise
        self.assertEqual(self.hl._marks.target, 0.0, "the marks would stay over whatever is now there")
        self.assertFalse(self.window().isVisible())
        self.assertEqual(self.moves, [True])

    def test_unplugging_the_marks_screen_hides_them(self):
        self.hl.show_frame(self.screen, self.area)
        self.hl.show_for(self.source)
        self.hl.set_word(0, 0, 4)
        self.hl._on_screen_removed(object())  # another screen: nothing changes
        self.assertTrue(self.window().isVisible())
        with self.assertLogs(SH.log, "INFO"):
            self.hl._on_screen_removed(self.screen)
        self.assertFalse(self.window().isVisible())
        self.assertEqual(self.moves, [True])
        self.hl.set_word(0, 5, 9)   # the reading goes on without them
        self.assertFalse(self.window().isVisible())

    def test_a_source_on_a_screen_that_has_gone(self):
        from dataclasses import replace
        self.hl.show_for(replace(self.source, screen="no such screen"))
        self.hl.set_word(0, 0, 4)
        self.assertFalse(self.hl.is_visible())


if __name__ == "__main__":
    unittest.main()
