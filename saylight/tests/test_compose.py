"""Tests for the Type or paste box (ui/compose.py): its geometry and layout, paste clean-up and the length limit,
the count, what a read gesture reads, the prefill, the resume and light-dismiss rules, and the text field and
window themselves.

No window is ever shown or activated and no sound plays: show and activation, the Win32 calls, the screens and
the clipboard are all replaced, and pastes go through QMimeData objects.
"""

import ctypes
import random
import sys
import unittest
from ctypes import wintypes
from unittest import mock

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QObject, QPoint, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import (QFont, QFontDatabase, QFontMetricsF, QImage, QKeyEvent, QResizeEvent, QTextBlockFormat,
                           QTextCursor, QWheelEvent)
from PySide6.QtWidgets import QApplication, QWidget

from saylight.engine import MAX_TEXT_CHARS
from saylight.theme import Theme
from saylight.ui import compose as C

_app = None

FIXED = QTextBlockFormat.LineHeightTypes.FixedHeight.value
AREA = QRect(0, 0, 1920, 1032)
OTHER_APP, OTHER_PID = 700, 99  # another app's window, and its process
HC_BLACK = {"window": (0, 0, 0), "windowtext": (255, 255, 255), "highlight": (26, 235, 255),
            "highlighttext": (0, 0, 0), "graytext": (63, 242, 63)}


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


def settle() -> None:
    for _ in range(3):
        QCoreApplication.processEvents()


def fire_away_check(w) -> None:
    """Run light dismiss's pending check now, rather than waiting out the settle time."""
    timer = w._away_timer
    if timer.isActive():
        assert timer.interval() == C.AWAY_SETTLE_MS
        timer.stop()
        timer.timeout.emit()


# --- Fakes -------------------------------------------------------------------------------------

class FakeSettings(QObject):
    changed = Signal(str)

    def __init__(self, **values) -> None:
        super().__init__()
        self.values = {"text_size": "medium", "reading_tint": "auto", "reading_font": "verdana", "spacing": "wide",
                       "highlight": "word", "panel.position": None, **values}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value) -> None:
        self.values[key] = value
        self.changed.emit(key)


class FakeSystem:
    def __init__(self, *, k: float = 1.0, hc: bool = False, motion: bool = False) -> None:
        self.k, self.hc, self.motion = k, hc, motion

    def color_scheme_dark(self) -> bool:
        return False

    def apps_use_light_theme(self) -> bool:
        return True

    def system_uses_light_theme(self) -> bool:
        return True

    def high_contrast(self) -> bool:
        return self.hc

    def animations_enabled(self) -> bool:
        return self.motion

    def text_scale_factor(self) -> float:
        return self.k

    def accent_palette(self) -> None:
        return None

    def color_prevalence(self) -> bool:
        return False

    def sys_colors(self) -> dict:
        return dict(HC_BLACK)

    def font_families(self) -> frozenset[str]:
        return frozenset(QFontDatabase.families())


def make_theme(settings: FakeSettings | None = None, **system) -> Theme:
    return Theme(settings or FakeSettings(), system=FakeSystem(**system))


class FakeTips:
    def hover(self, *_a) -> None:
        pass

    leave = press = refresh = show_at = hover


class FakePanel:
    def __init__(self, theme: Theme, rest: QRect, lines: int = 4) -> None:
        self.theme = theme
        self.rest = rest
        self.covered: list[bool] = []

    def width(self) -> int:
        return self.rest.width()

    def reading_width(self, expanded: bool) -> int:
        return self.theme.px(640 if expanded else 440)

    def _bottom_margin(self, _screen) -> int:
        return self.theme.px(12)

    def rest_rect(self) -> QRect:
        return QRect(self.rest)

    def set_covered(self, on: bool) -> None:
        self.covered.append(on)


class FakeCtl:
    def __init__(self, theme: Theme, settings: FakeSettings, panel: FakePanel) -> None:
        self.theme, self.settings, self.panel, self.tips = theme, settings, panel, FakeTips()


class FakeClipboard(QObject):
    dataChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.mime = QMimeData()

    def mimeData(self) -> QMimeData:
        return self.mime


class FakeScreen:
    def __init__(self, area: QRect) -> None:
        self.area = area

    def availableGeometry(self) -> QRect:
        return QRect(self.area)


class FakeGui:
    """Stands in for QGuiApplication inside compose.py: one screen, and a clipboard that is never the real one."""

    def __init__(self, area: QRect) -> None:
        self.screen = FakeScreen(area)
        self.clip = FakeClipboard()

    def screenAt(self, _point) -> FakeScreen:
        return self.screen

    def primaryScreen(self) -> FakeScreen:
        return self.screen

    def clipboard(self) -> FakeClipboard:
        return self.clip


class FakeWin32:
    """The Win32 calls compose.py makes, recorded; the foreground is whatever the test says."""

    def __init__(self) -> None:
        self.foreground = 0
        self.grant = True  # whether an activation request makes the box the foreground
        self.beeps: list[str] = []
        self.pids: dict[int, int] = {}
        self.classes: dict[int, str] = {}
        self.box = 0

    def patch(self, case: unittest.TestCase) -> None:
        def activate(hwnd: int) -> bool:
            if self.grant:
                self.foreground = hwnd
            return self.grant

        calls = dict(dwm_set_corner=lambda *_: True, dwm_set_dark=lambda *_: True,
                     dwm_disable_transitions=lambda *_: True, dwm_set_border_color=lambda *_: True,
                     set_topmost=lambda *_: None, set_foreground_window=activate,
                     get_foreground_window=lambda: self.foreground, message_beep=self.beeps.append,
                     caret_width_px=lambda: 1, get_window_pid=lambda h: self.pids.get(h, 0),
                     get_class_name=lambda h: self.classes.get(h, ""))
        patcher = mock.patch.multiple(C.win32, **calls)
        patcher.start()
        case.addCleanup(patcher.stop)


def key(widget, k, mods=Qt.KeyboardModifier.NoModifier, text: str = "") -> None:
    QCoreApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, k, mods, text))


def mime(text: str | None = None, html: str | None = None) -> QMimeData:
    md = QMimeData()
    if text is not None:
        md.setText(text)
    if html is not None:
        md.setHtml(html)
    return md


def blocks(doc) -> list:
    out, b = [], doc.firstBlock()
    while b.isValid():
        out.append(b)
        b = b.next()
    return out


# --- Pure functions ------------------------------------------------------------------------------

class ComposeRectTests(unittest.TestCase):
    M, MB = 12, 12

    def rect(self, rest: QRect, size: QSize, snap_x=None, area: QRect = AREA, margin=M, bottom=MB) -> QRect:
        return C.compose_rect(rest, area, size, snap_x=snap_x, margin=margin, bottom_margin=bottom)

    def test_bottom_centre_panel_grows_up_and_out(self) -> None:
        rest = QRect(740, 768, 440, 252)  # bottom-centre compact panel
        r = self.rect(rest, QSize(640, 468), "center")
        self.assertEqual((r.left(), r.right()), (640, 1279))  # +100 each side
        self.assertEqual(r.bottom(), rest.bottom())            # +216 upward, bottom edge kept
        self.assertEqual(r.height(), 468)

    def test_upper_half_grows_down(self) -> None:
        rest = QRect(740, 12, 640, 360)
        r = self.rect(rest, QSize(640, 468), "center")
        self.assertEqual((r.top(), r.left(), r.width()), (rest.top(), rest.left(), 640))

    def test_snap_left_and_right_keep_their_edge(self) -> None:
        left = self.rect(QRect(12, 768, 440, 252), QSize(640, 468), "left")
        self.assertEqual(left.left(), 12)
        right = self.rect(QRect(1468, 768, 440, 252), QSize(640, 468), "right")
        self.assertEqual(right.right(), 1907)

    def test_unsnapped_is_centred(self) -> None:
        rest = QRect(500, 600, 440, 252)
        r = self.rect(rest, QSize(640, 468), None)
        self.assertEqual(r.left(), 400)
        self.assertEqual(self.rect(rest, QSize(640, 468), "center"), r)

    def test_equal_width_covers_exactly(self) -> None:
        rest = QRect(640, 600, 640, 360)
        self.assertEqual(self.rect(rest, QSize(640, 468), "center").left(), 640)

    def test_clamped_inside_each_margin(self) -> None:
        size = QSize(640, 468)
        self.assertEqual(self.rect(QRect(20, 700, 440, 252), size, "center").left(), 12)       # left edge
        self.assertEqual(self.rect(QRect(1460, 700, 440, 252), size, "center").right(), 1907)  # right edge
        up = self.rect(QRect(740, 400, 440, 252), QSize(640, 700), "center")     # grows up past the top margin
        self.assertEqual(up.top(), 12)
        down = self.rect(QRect(740, 100, 440, 252), QSize(640, 1000), "center")  # grows down past the bottom one
        self.assertEqual(down.bottom(), 1019)
        self.assertTrue(up.contains(QRect(740, 400, 440, 252)) and down.contains(QRect(740, 100, 440, 252)))

    def test_panel_inside_the_margin_stays_covered(self) -> None:
        rest = QRect(2, 776, 440, 252)  # dragged into the margin, nearly touching the screen corner
        r = self.rect(rest, QSize(640, 468), "left")
        self.assertTrue(r.contains(rest))

    def test_k_one_and_a_half(self) -> None:
        rest = QRect(630, 654, 660, 378)
        r = self.rect(rest, QSize(960, 708), "center", margin=18, bottom=18)
        self.assertTrue(r.contains(rest))
        self.assertEqual(r.bottom(), rest.bottom())

    def test_property_covers_the_panel_and_stays_on_screen(self) -> None:
        rng = random.Random(11)
        for _ in range(2000):
            area = QRect(rng.randint(-2000, 2000), rng.randint(-500, 500), rng.randint(900, 3000),
                         rng.randint(700, 2000))
            margin, bottom = rng.choice((12, 15, 18)), rng.choice((12, 18, 60))
            w, h = rng.randint(300, 900), rng.randint(200, 500)
            x = rng.randint(area.left(), area.right() + 1 - w)
            rest = QRect(x, rng.randint(area.top(), area.bottom() + 1 - h), w, h)
            W = rng.randint(w, max(w, area.width() - 2 * margin))
            H = rng.randint(h, max(h, area.height() - margin - bottom))
            r = self.rect(rest, QSize(W, H), rng.choice(("left", "right", "center", None)), area, margin, bottom)
            allowed = area.adjusted(margin, margin, -margin, -bottom).united(rest)
            self.assertTrue(r.contains(rest), (area, rest, r))
            self.assertTrue(allowed.contains(r), (area, rest, r))


class ComposeLinesTests(unittest.TestCase):
    def lines(self, rest: QRect, area: QRect = AREA, panel_lines: int = 4) -> int:
        return C.compose_lines(rest, area, lh=27, chrome=144, panel_lines=panel_lines, margin=12, bottom_margin=12)

    def test_target_when_there_is_room(self) -> None:
        self.assertEqual(self.lines(QRect(740, 768, 440, 252)), 12)
        self.assertEqual(self.lines(QRect(740, 12, 640, 360), panel_lines=8), 12)

    def test_fits_the_room_left(self) -> None:
        area = QRect(0, 0, 1280, 600)
        rest = QRect(420, 336, 440, 252)  # room upward: 588 - 12 = 576 → (576 - 144) // 27 = 16 → 12
        self.assertEqual(self.lines(rest, area), 12)
        self.assertEqual(self.lines(QRect(420, 196, 440, 252), QRect(0, 0, 1280, 460)), 10)  # (436 - 144) // 27

    def test_never_fewer_than_the_panel(self) -> None:
        self.assertEqual(self.lines(QRect(0, 0, 440, 252), QRect(0, 0, 1280, 252)), 4)
        self.assertEqual(self.lines(QRect(0, 0, 640, 360), QRect(0, 0, 1280, 360), panel_lines=8), 8)


class ComposeLayoutTests(unittest.TestCase):
    def test_default_numbers(self) -> None:
        theme = make_theme()
        self.assertEqual(theme.line_height("reading"), 27)
        lay = C.compose_layout(640, 12, theme)
        well, hint, pill, clear = lay["well"], lay["hint"], lay["pill"], lay["clear"]
        self.assertEqual((well.top(), well.bottom() + 1), (44, 388))
        self.assertEqual((hint.top(), hint.bottom() + 1), (396, 412))
        self.assertEqual((pill.top(), pill.bottom() + 1), (416, 456))
        self.assertEqual(lay["window"].height(), 468)
        self.assertEqual(lay["back"], QRect(596, 8, 32, 32))
        self.assertEqual(clear.right() + 1, 628)
        self.assertEqual(clear.center().y(), pill.center().y())

    def test_rows_columns_and_footer(self) -> None:
        for k in (1.0, 1.5):
            theme = make_theme(k=k)
            px, lh = theme.px, theme.line_height("reading")
            for L in (4, 8, 12):
                with self.subTest(k=k, L=L):
                    lay = C.compose_layout(px(640), L, theme)
                    H = lay["window"].height()
                    self.assertEqual(H, C.chrome_height(theme) + L * lh)
                    self.assertEqual(lay["text"].height(), L * lh)
                    self.assertEqual(lay["glyph"].left(), px(22))
                    self.assertEqual(lay["title"].left(), px(46))
                    self.assertEqual(lay["pill"].left() + px(10), px(22))  # the pill's glyph column
                    self.assertEqual(lay["pill"].left() + px(34), px(46))  # its label column
                    self.assertEqual(lay["text"].width() - px(6), px(640) - 2 * px(12) - 2 * px(14))
                    if k == 1.0:
                        self.assertEqual(lay["pill"].top() + 20, H - 32)
                        self.assertEqual(lay["text"].width() - 6, 640 - 52)
        self.assertEqual(C.chrome_height(make_theme()), 144)

    def test_panel_sized_layout_is_the_panel(self) -> None:
        theme = make_theme()
        lay = C.compose_layout(440, 4, theme)
        self.assertEqual(lay["well"], QRect(12, 44, 416, 4 * 27 + 20))  # the reading card
        self.assertEqual(lay["back"], QRect(440 - 12 - 32, 8, 32, 32))  # the Hide button
        self.assertEqual(lay["window"].height(), 252)                     # the compact panel
        self.assertEqual(C.play_circle(theme, lay["pill"].top()), QRect(52, lay["pill"].top(), 40, 40))

    def test_rows_line_up_with_the_card(self) -> None:
        """Every font and spacing: the editor's row baselines sit within 0.5 px of the reading card's."""
        for font in ("verdana", "segoe", "calibri", "sitka"):
            for spacing in ("standard", "wide", "extra"):
                with self.subTest(font=font, spacing=spacing):
                    theme = make_theme(FakeSettings(reading_font=font, spacing=spacing))
                    lh = theme.line_height("reading")
                    ink = QFontMetricsF(theme.font("reading")).tightBoundingRect(C.INK_SAMPLE)
                    card = 54 + (lh - ink.height()) / 2 - ink.top()  # card row 0 at 44 + 10
                    edit = C.ComposeEdit(theme)
                    doc = edit.document()
                    doc.setTextWidth(588)
                    text_y = C.compose_layout(640, 12, theme)["text"].top()
                    self.assertAlmostEqual(text_y + edit.placeholder_origin().y(), card, delta=0.5)  # empty box
                    edit.load("Hg\nHg")
                    second = blocks(doc)[1]
                    top = doc.documentLayout().blockBoundingRect(second).top()
                    line = second.layout().lineAt(0)
                    self.assertAlmostEqual(text_y + top + line.y() + line.ascent(), card + lh, delta=0.5)


class WholeRowsTests(unittest.TestCase):
    LH = 27
    TEXT = QRect(26, 54, 588, 12 * 27)  # the default box's rows (the text field less its scrollbar)

    def test_nothing_while_the_well_is_narrower_than_the_text(self) -> None:
        self.assertTrue(C.whole_rows(QRect(112, 44, 416, 128), self.TEXT, self.LH, 0).isEmpty())
        self.assertTrue(C.whole_rows(QRect(12, 44, 600, 344), self.TEXT, self.LH, 0).isEmpty())
        self.assertTrue(C.whole_rows(QRect(30, 44, 598, 344), self.TEXT, self.LH, 0).isEmpty())

    def test_the_grown_well_holds_every_row(self) -> None:
        self.assertEqual(C.whole_rows(QRect(12, 44, 616, 344), self.TEXT, self.LH, 0), self.TEXT)

    def test_a_row_the_well_cuts_stays_hidden(self) -> None:
        well = QRect(12, 68, 616, 320)  # its top edge runs through row 0
        self.assertEqual(C.whole_rows(well, self.TEXT, self.LH, 0), QRect(26, 81, 588, 11 * 27))
        self.assertEqual(C.whole_rows(QRect(12, 44, 616, 60), self.TEXT, self.LH, 0), QRect(26, 54, 588, 27))

    def test_scrolled_off_the_grid(self) -> None:
        rows = C.whole_rows(QRect(12, 44, 616, 344), self.TEXT, self.LH, 26)  # a pixel of a row shows at the top
        self.assertEqual(rows, QRect(26, 55, 588, 11 * 27))

    def test_property_whole_rows_inside_the_well(self) -> None:
        rnd = random.Random(7)
        for _ in range(500):
            well = QRect(rnd.randint(0, 120), rnd.randint(0, 200), rnd.randint(0, 700), rnd.randint(0, 400))
            scroll = rnd.randint(0, 300)
            rows = C.whole_rows(well, self.TEXT, self.LH, scroll)
            if rows.isEmpty():
                continue
            self.assertTrue(well.contains(rows), (well, scroll))
            self.assertTrue(self.TEXT.contains(rows), (well, scroll))
            self.assertEqual(rows.height() % self.LH, 0)
            self.assertEqual((rows.top() - self.TEXT.top() + scroll) % self.LH, 0)  # on the row grid


class SanitizePasteTests(unittest.TestCase):
    def test_line_breaks_become_newlines(self) -> None:
        self.assertEqual(C.sanitize_paste("a\r\nb\rc\u2028d\u2029e\nf"), "a\nb\nc\nd\ne\nf")

    def test_tabs_become_spaces(self) -> None:
        self.assertEqual(C.sanitize_paste("a\tb"), "a b")

    def test_invisible_characters_are_removed(self) -> None:
        self.assertEqual(C.sanitize_paste("re\u00adad\u200bi\u200cn\u200dg\u2060\ufeff"), "reading")

    def test_other_control_codes_are_dropped(self) -> None:
        self.assertEqual(C.sanitize_paste("a\x00b\x07c\x0bd\x0ce\x1bf\x1fg"), "abcdefg")

    def test_text_people_write_is_kept(self) -> None:
        hebrew = "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd"
        arabic = "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645"
        for s in ("café \u00a0 5\u00a0km", "Dyslexia 📚 support 👍🏽", hebrew, arabic):
            with self.subTest(s=s):
                self.assertEqual(C.sanitize_paste(s), s)


class CapInsertTests(unittest.TestCase):
    def test_under_and_exactly_at_the_limit(self) -> None:
        self.assertEqual(C.cap_insert(90, "0123456789", limit=100), ("0123456789", False))
        self.assertEqual(C.cap_insert(90, "012345678", limit=100), ("012345678", False))

    def test_over_cuts_at_the_last_whitespace_that_fits(self) -> None:
        self.assertEqual(C.cap_insert(90, "one two three", limit=100), ("one two", True))
        self.assertEqual(C.cap_insert(94, "one two three", limit=100), ("one", True))

    def test_a_space_just_past_the_room_is_a_clean_cut(self) -> None:
        self.assertEqual(C.cap_insert(96, "one two", limit=100), ("one", True))
        self.assertEqual(C.cap_insert(93, "one two three", limit=100), ("one two", True))
        self.assertEqual(C.cap_insert(92, "one two x", limit=100), ("one two", True))

    def test_no_whitespace_is_a_hard_cut(self) -> None:
        self.assertEqual(C.cap_insert(95, "abcdefghij", limit=100), ("abcde", True))

    def test_full_box_takes_nothing(self) -> None:
        self.assertEqual(C.cap_insert(100, "more", limit=100), ("", True))
        self.assertEqual(C.cap_insert(120, "", limit=100), ("", False))

    def test_default_limit_is_the_engines(self) -> None:
        self.assertEqual(C.cap_insert(MAX_TEXT_CHARS - 2, "ab cd"), ("ab", True))


class WordCountTests(unittest.TestCase):
    def test_only_runs_with_a_letter_or_digit_count(self) -> None:
        self.assertEqual(C.word_count("((( ))) …"), 0)
        self.assertEqual(C.word_count("One two"), 2)
        self.assertEqual(C.word_count("e.g. 5 km — fine, (ok)"), 5)
        self.assertEqual(C.word_count("Café 📚 naïve 漢字"), 3)
        self.assertEqual(C.word_count(""), 0)


class SpokenTests(unittest.TestCase):
    def test_keys_pressed_together_are_joined_and_sentences_end(self) -> None:
        self.assertEqual(C.spoken(C.HINT), "Ctrl+Enter reads it aloud. Esc goes back.")
        self.assertEqual(C.spoken(C.HINT_PARTIAL), "Ctrl+Enter reads the highlighted part. Esc goes back.")
        self.assertEqual(C.spoken([("text", "Press "), ("key", "Esc")]), "Press Esc.")


class CountLabelTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(C.count_label(0, 0.95), "")

    def test_words_and_minutes(self) -> None:
        self.assertEqual(C.count_label(1, 0.95), "1 word · under a minute")
        self.assertEqual(C.count_label(47, 0.95), "47 words · under a minute")
        self.assertEqual(C.count_label(312, 0.95), "312 words · about 2 min")
        self.assertEqual(C.count_label(14_779, 0.95), "14,779 words · about 1 h 50 min")

    def test_highlighted(self) -> None:
        self.assertEqual(C.count_label(12, 0.95, True), "12 words highlighted · under a minute")
        self.assertEqual(C.count_label(1, 0.95, True), "1 word highlighted · under a minute")

    def test_speed_scales_the_time(self) -> None:
        self.assertEqual(C.count_label(284, 1.0), "284 words · about 2 min")
        self.assertEqual(C.count_label(284, 2.0), "284 words · about 1 min")
        self.assertEqual(C.count_label(17_040, 1.0), "17,040 words · about 2 h")


class ReadScopeTests(unittest.TestCase):
    FULL = "First sentence. Second sentence."

    def test_no_or_blank_selection_reads_all(self) -> None:
        self.assertEqual(C.read_scope("", self.FULL), (self.FULL, "all"))
        self.assertEqual(C.read_scope("  \n ", self.FULL), (self.FULL, "all"))

    def test_partial_selection_reads_just_that(self) -> None:
        self.assertEqual(C.read_scope("Second sentence.", self.FULL), ("Second sentence.", "selection"))

    def test_whole_text_selected_reads_all(self) -> None:
        self.assertEqual(C.read_scope(self.FULL, self.FULL), (self.FULL, "all"))
        self.assertEqual(C.read_scope(self.FULL, "\n" + self.FULL + "  "), ("\n" + self.FULL + "  ", "all"))


class PrefillPlanTests(unittest.TestCase):
    def test_typed_text_is_kept(self) -> None:
        self.assertEqual(C.prefill_plan(True, "my notes", ("Reading.", 0)), ("keep", "", 0))

    def test_blank_typed_text_gives_way_to_the_reading(self) -> None:
        self.assertEqual(C.prefill_plan(True, "  \n", ("Reading.", 0)), ("load", "Reading.", 0))

    def test_reading_loads_at_its_current_sentence_in_utf16(self) -> None:
        text = "Good 📚 work. Next one."
        start = text.index("Next")
        self.assertEqual(C.prefill_plan(False, "old prefill", (text, start)), ("load", text, start + 1))

    def test_empty(self) -> None:
        self.assertEqual(C.prefill_plan(False, "", None), ("empty", "", 0))
        self.assertEqual(C.prefill_plan(False, "untouched prefill", None), ("empty", "", 0))
        self.assertEqual(C.prefill_plan(False, "", ("", 0)), ("empty", "", 0))


class ShouldResumeTests(unittest.TestCase):
    def test_each_reason(self) -> None:
        doc = object()
        for reason in ("esc", "back", "away", "tray", "hotkey", "screen"):
            self.assertTrue(C.should_resume(doc, doc, "paused", reason), reason)
        for reason in ("read", "quit"):
            self.assertFalse(C.should_resume(doc, doc, "paused", reason), reason)

    def test_only_the_same_reading_still_paused(self) -> None:
        doc = object()
        self.assertFalse(C.should_resume(doc, object(), "paused", "esc"))  # a new reading started
        self.assertFalse(C.should_resume(doc, doc, "reading", "esc"))      # already resumed
        self.assertFalse(C.should_resume(None, None, "ready", "esc"))      # cleared meanwhile
        self.assertFalse(C.should_resume(None, doc, "paused", "esc"))      # the box didn't pause it


class LightDismissTests(unittest.TestCase):
    OWN = 4242

    def dismiss(self, *, phase="open", was_active=True, hwnd=100, pid=77, cls="Notepad") -> bool:
        return C.is_light_dismiss(phase, was_active, hwnd, pid, cls, own_pid=self.OWN)

    def test_another_app(self) -> None:
        self.assertTrue(self.dismiss())

    def test_shell_own_and_nothing(self) -> None:
        self.assertFalse(self.dismiss(cls="Shell_TrayWnd"))
        self.assertFalse(self.dismiss(cls="Windows.UI.Core.CoreWindow"))
        self.assertFalse(self.dismiss(pid=self.OWN))
        self.assertFalse(self.dismiss(hwnd=0, pid=0, cls=""))

    def test_not_before_the_first_activation_or_while_moving(self) -> None:
        self.assertFalse(self.dismiss(was_active=False))
        for phase in ("opening", "closing", "closed"):
            self.assertFalse(self.dismiss(phase=phase), phase)

    def test_transient_input_classes_are_ignored(self) -> None:
        with mock.patch.object(C, "TRANSIENT_INPUT_CLASSES", frozenset({"ClipboardHistory"})):
            self.assertFalse(self.dismiss(cls="ClipboardHistory"))


# --- The text field ------------------------------------------------------------------------------

class ComposeEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.win32 = FakeWin32()
        self.win32.patch(self)
        self.settings = FakeSettings()
        self.theme = make_theme(self.settings)
        self.edit = C.ComposeEdit(self.theme)

    def assert_pitch(self) -> None:
        settle()
        lh = self.theme.line_height("reading")
        for b in blocks(self.edit.document()):
            fmt = b.blockFormat()
            self.assertEqual((fmt.lineHeightType(), fmt.lineHeight()), (FIXED, lh), b.text())

    def test_setup_before_the_first_show(self) -> None:
        e = self.edit
        self.assertFalse(e.acceptRichText())
        self.assertTrue(e.viewport().autoFillBackground())
        self.assertEqual(e.document().documentMargin(), 0)
        self.assertTrue(e.tabChangesFocus())
        self.assertIsInstance(e.verticalScrollBar(), C.ThinScrollBar)
        self.assertEqual(e.verticalScrollBar().sizeHint().width(), 6)
        self.assertEqual(e.cursorWidth(), 2)
        self.assertEqual(e.placeholderText(), "")  # painted on the first row instead (placeholder_origin)
        self.assertEqual(e.placeholder, "Type or paste text here.")
        self.assertEqual(e.font().family(), self.theme.font("reading").family())
        self.assert_pitch()

    def test_placeholder_sits_where_typing_appears(self) -> None:
        e = self.edit
        e.document().setTextWidth(588)
        origin = e.placeholder_origin()
        e.load("D")
        line = e.document().firstBlock().layout().lineAt(0)
        self.assertAlmostEqual(origin.y(), line.y() + line.ascent(), delta=0.01)
        self.assertEqual(origin.x(), line.x())
        self.assertIsNone(e.placeholder_origin())  # gone once there is text
        e.load("")
        e.document().firstBlock().layout().setPreeditArea(0, "か")  # an IME composition under way
        self.assertIsNone(e.placeholder_origin())

    def test_scrolling_to_the_caret_ends_on_whole_rows(self) -> None:
        e, lh = self.edit, self.theme.line_height("reading")
        e.resize(594, 4 * lh)
        QCoreApplication.sendEvent(e, QResizeEvent(e.size(), QSize()))  # lay the hidden field out
        QCoreApplication.sendEvent(e.viewport(), QResizeEvent(e.viewport().size(), QSize()))
        e.load("\n".join(f"Row {i}" for i in range(20)))
        bar = e.verticalScrollBar()
        self.assertEqual(bar.maximum(), 16 * lh)
        e.moveCursor(QTextCursor.MoveOperation.End)  # Qt scrolls to the caret's ink, a pixel short of the row
        settle()
        self.assertEqual(bar.value(), 16 * lh)
        e.moveCursor(QTextCursor.MoveOperation.Start)
        settle()
        self.assertEqual(bar.value(), 0)
        c = e.textCursor()
        c.setPosition(e.document().findBlockByNumber(10).position())
        e.setTextCursor(c)
        settle()
        self.assertEqual(bar.value(), 7 * lh)  # row 10 is the bottom row, whole
        bar.setValue(lh + 5)
        with mock.patch.object(C.QApplication, "mouseButtons", return_value=Qt.MouseButton.LeftButton):
            e._snap_rows()  # a selection drag scrolls where the pointer says
        self.assertEqual(bar.value(), lh + 5)

    def test_caret_follows_the_windows_setting(self) -> None:
        with mock.patch.object(C.win32, "caret_width_px", return_value=5):
            self.assertEqual(C.ComposeEdit(self.theme).cursorWidth(), 5)

    def test_pitch_holds_through_pastes_deletes_undo_and_loads(self) -> None:
        e = self.edit
        e.insertFromMimeData(mime("First paragraph.\n\nSecond one.\nThird line.\n\nFourth."))
        self.assert_pitch()
        self.assertEqual(len(blocks(e.document())), 6)
        e.selectAll()
        key(e, Qt.Key.Key_Delete)
        self.assert_pitch()
        e.undo()
        self.assert_pitch()
        self.assertIn("Fourth.", e.toPlainText())
        e.redo()  # the enforcer's pass after the undo left the redo history alone
        self.assert_pitch()
        self.assertEqual(e.toPlainText(), "")
        e.setPlainText("one\ntwo\nthree")
        self.assert_pitch()
        e.load("a\nb\nc")
        self.assert_pitch()
        self.assertFalse(e.document().isUndoAvailable())  # a load is not an undo step

    def test_one_undo_removes_a_paste_with_its_formatting(self) -> None:
        e = self.edit
        e.insertFromMimeData(mime("kept"))
        settle()
        e.insertFromMimeData(mime("\nnew one\nnew two"))
        settle()
        e.undo()
        settle()
        self.assertEqual(e.toPlainText(), "kept")
        self.assert_pitch()

    def test_pitch_follows_a_theme_change(self) -> None:
        self.edit.insertFromMimeData(mime("one\ntwo"))
        settle()
        self.settings.set("spacing", "extra")
        self.edit.apply_theme()
        self.assertEqual(self.theme.line_height("reading"), 32)
        self.assert_pitch()

    def test_html_paste_becomes_plain_text(self) -> None:
        e = self.edit
        e.insertFromMimeData(mime(html="<b style='color:red;font-family:Comic Sans MS'>Bold</b> <i>red</i>"))
        self.assertEqual(e.toPlainText(), "Bold red")
        c = QTextCursor(e.document())
        c.setPosition(2)
        fmt = c.charFormat()
        self.assertNotEqual(fmt.fontWeight(), QFont.Weight.Bold)
        self.assertFalse(fmt.fontItalic())
        self.assertNotIn("Comic", fmt.fontFamilies() or [])
        self.assertFalse(fmt.foreground().color().name() == "#ff0000")

    def test_paste_is_cleaned_and_images_are_ignored(self) -> None:
        e = self.edit
        e.insertFromMimeData(mime("a\tb\u00adc\r\nd"))
        self.assertEqual(e.toPlainText(), "a bc\nd")
        image = QMimeData()
        image.setData("image/png", b"\x89PNG")
        self.assertFalse(e.canInsertFromMimeData(image))
        e.insertFromMimeData(image)
        self.assertEqual(e.toPlainText(), "a bc\nd")

    def test_paste_over_the_limit_is_cut_and_says_so(self) -> None:
        e = self.edit
        hits = []
        e.limitHit.connect(lambda: hits.append(1))
        e.load("x" * (MAX_TEXT_CHARS - 10))
        e.moveCursor(QTextCursor.MoveOperation.End)
        e.insertFromMimeData(mime(" twelve characters and more"))
        self.assertEqual(e.toPlainText()[MAX_TEXT_CHARS - 10:], " twelve")
        self.assertEqual(hits, [1])

    def test_typing_past_the_limit_is_trimmed(self) -> None:
        e = self.edit
        hits = []
        e.limitHit.connect(lambda: hits.append(1))
        e.load("x" * MAX_TEXT_CHARS)
        e.moveCursor(QTextCursor.MoveOperation.End)
        e.textCursor().insertText("yz")  # as typing or an IME commit arrives
        settle()
        self.assertEqual(len(e.toPlainText()), MAX_TEXT_CHARS)
        self.assertEqual(hits, [1])

    def test_keys(self) -> None:
        e = self.edit
        got = []
        e.readKey.connect(lambda: got.append("read"))
        e.escapeKey.connect(lambda: got.append("esc"))
        e.sizeStep.connect(lambda n: got.append(n))
        e.undoKey.connect(lambda: got.append("undo"))
        ctrl = Qt.KeyboardModifier.ControlModifier
        key(e, Qt.Key.Key_Return, ctrl)
        key(e, Qt.Key.Key_Enter, ctrl | Qt.KeyboardModifier.KeypadModifier)
        key(e, Qt.Key.Key_QuoteLeft, ctrl, "`")
        key(e, Qt.Key.Key_Escape)
        key(e, Qt.Key.Key_Equal, ctrl, "=")
        key(e, Qt.Key.Key_Minus, ctrl, "-")
        self.assertEqual(got, ["read", "read", "read", "esc", 1, -1])
        self.assertEqual(e.toPlainText(), "")

    def test_shift_enter_is_a_normal_paragraph(self) -> None:
        e = self.edit
        key(e, Qt.Key.Key_A, text="a")
        key(e, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier, "\r")
        key(e, Qt.Key.Key_B, text="b")
        self.assertEqual(e.document().blockCount(), 2)
        self.assertNotIn("\u2028", e.document().toRawText())
        self.assert_pitch()

    def test_ctrl_z_undoes_the_clear_only_in_an_empty_box(self) -> None:
        e = self.edit
        got = []
        e.undoKey.connect(lambda: got.append("undo"))
        ctrl = Qt.KeyboardModifier.ControlModifier
        e.undo_mode = True
        key(e, Qt.Key.Key_Z, ctrl, "z")
        self.assertEqual(got, ["undo"])
        key(e, Qt.Key.Key_A, text="a")
        key(e, Qt.Key.Key_Z, ctrl, "z")  # text in the box: an ordinary undo
        self.assertEqual(got, ["undo"])
        self.assertEqual(e.toPlainText(), "")

    def test_ctrl_wheel_steps_the_text_size(self) -> None:
        e = self.edit
        steps = []
        e.sizeStep.connect(steps.append)
        for dy in (120, -120, 0):
            c = QPointF(10, 10)
            e.wheelEvent(QWheelEvent(c, c, QPoint(0, 0), QPoint(0, dy), Qt.MouseButton.NoButton,
                                     Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False))
        self.assertEqual(steps, [1, -1])
        self.assertEqual(e.font().pixelSize(), self.theme.font("reading").pixelSize())  # no QTextEdit zoom

    def test_edited_only_for_the_users_changes(self) -> None:
        e = self.edit
        edits = []
        e.edited.connect(lambda: edits.append(1))
        e.load("loaded")
        e.apply_theme()
        settle()
        self.assertEqual(edits, [])
        key(e, Qt.Key.Key_A, text="a")
        self.assertEqual(len(edits), 1)

    def test_blank(self) -> None:
        e = self.edit
        self.assertTrue(e.is_blank())
        e.load(" \n\u00a0\t ")
        self.assertTrue(e.is_blank())
        e.load("  x ")
        self.assertFalse(e.is_blank())


# --- The window --------------------------------------------------------------------------------

class WindowCase(unittest.TestCase):
    REST = QRect(740, 768, 440, 252)

    def setUp(self) -> None:
        self.win32 = FakeWin32()
        self.win32.patch(self)
        self.gui = FakeGui(AREA)
        patcher = mock.patch.object(C, "QGuiApplication", self.gui)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("show", "activateWindow"):
            patcher = mock.patch.object(C.ComposeWindow, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = FakeSettings(**{"panel.position": {"snap_x": "center", "snap_y": "bottom"}})
        self.theme = make_theme(self.settings)
        self.panel = FakePanel(self.theme, QRect(self.REST))
        self.win = C.ComposeWindow(FakeCtl(self.theme, self.settings, self.panel))
        self.win32.box = self.win.hwnd
        self.events: list[tuple] = []
        w = self.win
        w.readRequested.connect(lambda text, scope: self.events.append(("read", text, scope)))
        w.clearRequested.connect(lambda: self.events.append(("clear",)))
        w.undoRequested.connect(lambda: self.events.append(("undo",)))
        w.closeRequested.connect(lambda reason: self.events.append(("close", reason)))
        w.sizeStepRequested.connect(lambda n: self.events.append(("size", n)))
        w.closed.connect(lambda reason: self.events.append(("closed", reason)))
        w.textEdited.connect(lambda: self.events.append(("edited",)))

    def open(self, prefill=None, *, panel_visible: bool = True, return_to: int = 555) -> None:
        self.win.open_at(self.panel.rest_rect(), panel_visible=panel_visible, panel_lines=4, prefill=prefill,
                         return_to=return_to)

    def type(self, text: str) -> None:
        for ch in text:
            key(self.win.edit, Qt.Key.Key_A, text=ch)

    def native(self, message: int, wparam: int = 0):
        """Hand the window a Windows message the way Qt does, and return its answer."""
        msg = wintypes.MSG()
        msg.message, msg.wParam = message, wparam
        return self.win.nativeEvent(b"windows_generic_MSG", shiboken6.VoidPtr(ctypes.addressof(msg)))


class WindowSetupTests(WindowCase):
    def test_unowned_tool_window_that_can_take_focus(self) -> None:
        w = self.win
        self.assertIsNone(w.parent())
        flags = w.windowFlags()
        for flag in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint):
            self.assertTrue(flags & flag, flag)
        self.assertFalse(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertFalse(w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(w.windowTitle(), "Type or paste – Saylight")
        self.open(("Text.", 0))
        w.close_animated("esc")
        self.assertEqual(w.windowFlags(), flags)  # never changed at runtime

    def test_tab_order(self) -> None:
        w = self.win
        self.gui.clip.mime.setText("copied")
        w._sync_paste()
        order, widget = [], w.edit
        for _ in range(200):
            if widget in (w.edit, w.btn_paste, w.pill, w.btn_clear, w.btn_back) and widget not in order:
                order.append(widget)
            widget = widget.nextInFocusChain()
            if widget is w.edit:
                break
        self.assertEqual(order, [w.edit, w.btn_paste, w.pill, w.btn_clear, w.btn_back])
        for control in (w.btn_paste, w.pill, w.btn_clear, w.btn_back):
            self.assertEqual(control.focusPolicy(), Qt.FocusPolicy.TabFocus)

    def test_alt_f4_is_back(self) -> None:
        self.open(("Text.", 0))
        self.assertFalse(self.win.close())
        self.assertEqual(self.events[-1], ("close", "back"))

    def test_a_box_on_its_way_out_accepts_the_close_qt_sends_on_quit(self) -> None:
        # While it fades out it is still a visible window, and an ignored close would cancel QApplication.quit().
        w = self.win
        self.open(("Text.", 0))
        w._t.to = lambda *_a: None  # hold the close fade
        w.close_animated("esc")
        self.assertEqual(w.session.phase, "closing")
        self.assertTrue(w.close())
        self.assertNotIn(("close", "back"), self.events)
        w._on_motion(1.0)
        self.assertTrue(w.close())  # closed and hidden: nothing to hold up either
        self.assertNotIn(("close", "back"), self.events)


class WindowOpenTests(WindowCase):
    def test_geometry_covers_the_panel(self) -> None:
        self.open()
        w = self.win
        self.assertEqual(w.geometry(), QRect(640, 552, 640, 468))
        self.assertTrue(w.geometry().contains(self.REST))
        self.assertEqual(w.edit.geometry(), w._final["text"])

    def test_activation_granted(self) -> None:
        self.open(return_to=555)
        s = self.win.session
        self.assertEqual((s.phase, s.return_to, s.was_active), ("open", 555, True))
        self.assertFalse(self.win.hint.note)
        self.assertFalse(self.win.edit.isReadOnly())

    def test_activation_refused_asks_for_a_click(self) -> None:
        self.win32.grant = False
        self.open()
        self.assertFalse(self.win.session.was_active)
        self.assertEqual(self.win.hint.content, ("note", "Click in the box to start typing."))
        self.assertEqual(self.win32.beeps, [])

    def test_the_click_that_brings_a_refused_box_to_the_front_arms_light_dismiss(self) -> None:
        # After a refused activation Qt already counts the box as active, so that click changes nothing in Qt:
        # Windows only sends WM_NCACTIVATE (or WM_ACTIVATEAPP).
        w = self.win
        self.win32.grant = False
        self.win32.foreground = 555
        self.open()
        for message in (C.WM_NCACTIVATE, C.WM_ACTIVATEAPP):
            self.native(message, 1)  # while your app stays in front, nothing changes
            settle()
            self.assertFalse(w.session.was_active)
        self.win32.foreground = w.hwnd  # your click in the box brings it to the front
        self.native(C.WM_NCACTIVATE, 1)
        settle()
        self.assertTrue(w.session.was_active)
        self.assertFalse(w.hint.note)
        self.win32.pids[OTHER_APP], self.win32.classes[OTHER_APP] = OTHER_PID, "Chrome_WidgetWin_1"
        self.win32.foreground = OTHER_APP
        w._check_away()
        self.assertEqual(self.events[-1], ("close", "away"))

    def test_a_click_on_the_fading_box_never_brings_it_back(self) -> None:
        w = self.win
        self.open(("Text.", 0))
        self.assertEqual(self.native(C.WM_MOUSEACTIVATE), (False, 0))  # open: a click activates it as usual
        w._t.to = lambda *_a: None  # hold the close fade
        w.close_animated("esc")
        self.assertEqual(self.native(C.WM_MOUSEACTIVATE), (True, C.MA_NOACTIVATE))

    def test_prefill_selects_everything_with_the_caret_at_the_start(self) -> None:
        text = "One. Two 📚. Three."
        self.open((text, text.index("Three")))
        e = self.win.edit
        self.assertEqual(e.toPlainText(), text)
        c = e.textCursor()
        self.assertEqual((c.position(), c.anchor()), (0, len(text) + 1))  # the emoji is two UTF-16 units
        self.assertFalse(self.win.user_text)
        self.assertFalse(self.win.has_draft())
        self.assertFalse(e.document().isUndoAvailable())

    def test_typed_text_is_kept_and_untouched_prefill_is_not(self) -> None:
        self.open(("Reading text.", 0))
        self.type("Mine")
        self.assertTrue(self.win.user_text)
        self.assertTrue(self.win.has_draft())
        self.assertIn(("edited",), self.events)
        self.win.close_animated("away")
        self.open(("Another reading.", 0))
        self.assertEqual(self.win.edit.toPlainText(), "Mine")
        self.win.clear_text()
        self.win.close_animated("esc")
        self.open(("Another reading.", 0))
        self.assertEqual(self.win.edit.toPlainText(), "Another reading.")
        self.win.close_animated("esc")
        self.open(None)
        self.assertEqual(self.win.edit.toPlainText(), "")

    def test_opening_an_open_box_activates_it_again(self) -> None:
        self.open(("Text.", 0))
        self.win32.foreground = 999
        self.open(("Other.", 0))
        self.assertEqual(self.win32.foreground, self.win.hwnd)
        self.assertEqual(self.win.edit.toPlainText(), "Text.")

    def test_covers_the_panel_only_when_it_is_shown(self) -> None:
        self.open()
        self.assertEqual(self.panel.covered, [True])
        self.win.close_animated("esc")
        self.assertEqual(self.panel.covered, [True, False])
        self.open(panel_visible=False)
        self.win.close_animated("esc")
        self.assertEqual(self.panel.covered, [True, False])

    def test_close_hides_keeps_the_text_and_reports_why(self) -> None:
        self.open(("Kept.", 0))
        self.win.close_animated("read")
        self.assertEqual(self.events[-1], ("closed", "read"))
        self.assertEqual(self.win.session.phase, "closed")
        self.assertTrue(self.win.edit.isReadOnly())
        self.assertEqual(self.win.edit.toPlainText(), "Kept.")
        self.win.close_animated("read")  # closing a closed box does nothing
        self.assertEqual([e for e in self.events if e[0] == "closed"], [("closed", "read")])


class WindowMotionTests(WindowCase):
    def test_grow_starts_on_the_panel_and_ends_on_the_box(self) -> None:
        w = self.win
        self.open()
        offset = self.REST.topLeft() - w.geometry().topLeft()
        start = C.compose_layout(440, 4, self.theme)
        w._motion = "open"
        w._on_motion(0.0)
        self.assertEqual(w.pill.geometry(), C.play_circle(self.theme, start["pill"].top()).translated(offset)
                         .adjusted(-3, -3, 3, 3))  # the face plus room for the focus ring
        self.assertEqual(w.overlay.well, start["well"].translated(offset - w._final["well"].topLeft()))
        self.assertFalse(w.overlay.isHidden())
        self.assertEqual(w.pill.label_opacity, 0.0)
        self.assertEqual(w.windowOpacity(), 0.0)
        self.assertEqual(w.edit.geometry(), w._final["text"])  # the text never moves
        w._on_motion(0.5)  # 125 ms: half the content fade-in is still to come
        self.assertGreater(w.pill.label_opacity, 0.0)
        self.assertLess(w.pill.label_opacity, 1.0)
        w._on_motion(1.0)
        self.assertTrue(w.overlay.isHidden())
        self.assertEqual(w.windowOpacity(), 1.0)
        self.assertEqual(w.pill.label_opacity, 1.0)
        self.assertEqual(w.btn_clear.geometry(), w._final["clear"].adjusted(-3, -3, 3, 3))

    def test_close_shrinks_back_into_the_panel(self) -> None:
        w = self.win
        self.open()
        w._t.to = lambda *_a: None  # hold the clock so each frame can be checked
        w.close_animated("esc")
        self.assertEqual(self.panel.covered, [True, False])  # uncovered at once
        w._on_motion(0.0)
        self.assertEqual(w.windowOpacity(), 1.0)
        w._on_motion(0.5)  # 60 ms: the content is mostly gone, the box part way back
        self.assertLess(w.pill.label_opacity, 0.5)
        self.assertFalse(w.overlay.isHidden())
        self.assertLess(w.windowOpacity(), 1.0)
        w._on_motion(1.0)
        self.assertEqual(self.events[-1], ("closed", "esc"))
        self.assertTrue(w.overlay.isHidden())

    def test_reduced_motion_opens_and_closes_in_one_frame(self) -> None:
        self.assertTrue(self.theme.reduced_motion)
        self.open()
        self.assertEqual(self.win.session.phase, "open")
        self.assertEqual(self.win.windowOpacity(), 1.0)
        self.win.close_animated("esc")
        self.assertEqual(self.win.session.phase, "closed")

    def test_panel_hidden_slides_instead_of_growing(self) -> None:
        w = self.win
        self.open(panel_visible=False)
        rect = QRect(w._rect)
        w._motion = "open"
        w._on_motion(0.0)
        self.assertEqual(w.pos(), rect.topLeft() + QPoint(0, 8))  # from below: the panel rests in the lower half
        self.assertTrue(w.overlay.isHidden())
        w._on_motion(1.0)
        self.assertEqual(w.pos(), rect.topLeft())

    def test_text_shows_only_in_rows_the_growing_box_holds_whole(self) -> None:
        w = self.win
        self.open(("\n".join(f"Line {i}." for i in range(20)), 0))
        lh = self.theme.line_height("reading")
        w._motion = "open"
        w._on_motion(0.0)
        self.assertTrue(w.overlay.rows.isEmpty())  # the card's well is narrower than the box's text
        shown = 0
        for i in range(1, 100):
            w._on_motion(i / 100)
            rows, well = w.overlay.rows, w.overlay.well
            if not rows.isEmpty():
                shown += 1
                self.assertTrue(well.contains(rows), i)
                self.assertEqual(rows.height() % lh, 0, i)
        self.assertGreater(shown, 0)
        w._on_motion(1.0)
        self.assertTrue(w.overlay.isHidden())

    def test_hidden_text_never_shows_through_the_growing_edge(self) -> None:
        # Surface and the well's background must not meet at one antialiased edge: at 125% the text showed
        # through that seam as faint ticks.
        w = self.win
        self.open(("Text.", 0))
        w._motion = "open"
        w._on_motion(0.25)  # 62 ms: the box part way grown, its content still hidden
        overlay = w.overlay
        self.assertEqual(overlay.q, 0.0)
        img = QImage(overlay.size() * 1.25, QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(1.25)
        img.fill(Qt.GlobalColor.transparent)
        overlay.render(img, renderFlags=QWidget.RenderFlag.DrawChildren)  # no window background under it
        alpha = img.convertToFormat(QImage.Format.Format_Alpha8)
        data, bpl = bytes(alpha.constBits()), alpha.bytesPerLine()
        self.assertEqual({min(data[y * bpl:y * bpl + alpha.width()]) for y in range(alpha.height())}, {255})


class WindowBehaviourTests(WindowCase):
    def test_read_gestures(self) -> None:
        self.open(None)
        self.win.read_from_shortcut()
        self.assertEqual(self.win.hint.content, ("note", "Type or paste some text first."))
        self.assertEqual(self.win32.beeps, ["ok"])
        self.type("One two")
        self.assertFalse(self.win.hint.note)  # the next edit ends a note
        key(self.win.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        c = self.win.edit.textCursor()
        c.setPosition(4)
        c.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
        self.win.edit.setTextCursor(c)
        self.win.pill.clicked.emit()
        self.assertEqual([e for e in self.events if e[0] == "read"], [("read", "One two", "all"),
                                                                       ("read", "two", "selection")])

    def test_header_hint_and_pill_follow_the_text(self) -> None:
        w = self.win
        self.open(None)
        w.on_speed(0.95, "")
        self.assertEqual(w.info.parts, [])
        self.assertFalse(w.pill.isEnabled())
        self.type("Three short words")
        w._refresh()
        self.assertEqual(w.info.parts, [("text", "3 words · under a minute")])
        self.assertTrue(w.pill.isEnabled())
        w.edit.selectAll()
        c = w.edit.textCursor()
        c.setPosition(0)
        c.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
        w.edit.setTextCursor(c)
        w._refresh()
        self.assertEqual(w.info.parts, [("text", "1 word highlighted · under a minute")])
        self.assertEqual(w.hint.content, ("hint", C.HINT_PARTIAL))
        self.assertEqual(w.pill.tooltip_text(), "Read the highlighted part aloud (Ctrl+Enter)")

    def test_keys_and_buttons_ask_to_close_clear_or_resize(self) -> None:
        w = self.win
        self.open(("Text.", 0))
        key(w.edit, Qt.Key.Key_Escape)
        w.btn_back.clicked.emit()
        w.btn_clear.clicked.emit()
        c = QPointF(10, 10)
        QCoreApplication.sendEvent(w.edit.viewport(), QWheelEvent(
            c, c, QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase, False))
        self.assertEqual(self.events, [("close", "esc"), ("close", "back"), ("clear",), ("size", 1)])

    def test_undo_window(self) -> None:
        w = self.win
        self.open(None)
        self.type("Draft")
        snap = w.snapshot()
        w.clear_text()
        self.assertEqual(w.edit.toPlainText(), "")
        self.assertFalse(w.edit.document().isUndoAvailable())  # the undo history goes with the text
        self.assertFalse(w.has_draft())
        w.set_undo_mode(True)
        self.assertEqual((w.btn_clear.label, w.btn_clear.accessibleName()), ("Undo", "Undo clear"))
        self.assertEqual(w.info.parts, [("text", "Text cleared")])
        self.assertTrue(w.btn_clear.isEnabled())
        w.btn_clear.clicked.emit()
        key(w.edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier, "z")
        self.assertEqual(self.events[-2:], [("undo",), ("undo",)])
        w.restore_snapshot(snap)
        w.set_undo_mode(False)
        self.assertEqual(w.edit.toPlainText(), "Draft")
        self.assertTrue(w.has_draft())
        self.assertEqual(w.edit.textCursor().position(), 5)
        self.assertEqual(w.btn_clear.label, "Clear")

    def test_snapshot_round_trip(self) -> None:
        w = self.win
        self.open(None)
        self.type("Hello there")
        c = w.edit.textCursor()
        c.setPosition(6)
        c.setPosition(2, QTextCursor.MoveMode.KeepAnchor)
        w.edit.setTextCursor(c)
        snap = w.snapshot()
        w.clear_text()
        w.restore_snapshot(snap)
        self.assertEqual(w.snapshot(), snap)
        self.assertEqual((snap.text, snap.position, snap.anchor, snap.user_text), ("Hello there", 2, 6, True))

    def test_limit_note_beeps_at_most_every_two_seconds(self) -> None:
        w = self.win
        self.open(None)
        w.edit.load("x" * (MAX_TEXT_CHARS - 3))
        w.edit.moveCursor(QTextCursor.MoveOperation.End)
        w.edit.insertFromMimeData(mime(" ab cd"))
        self.assertEqual(w.hint.content[1], "The box holds up to 100,000 characters, so the rest wasn't added.")
        w.edit.insertFromMimeData(mime(" ef"))
        self.assertEqual(self.win32.beeps, ["ok"])

    def test_paste_chip_only_for_an_empty_box_and_text_on_the_clipboard(self) -> None:
        w = self.win
        self.open(None)
        self.assertTrue(w.btn_paste.isHidden())
        self.gui.clip.mime.setText("copied")
        self.gui.clip.dataChanged.emit()
        self.assertFalse(w.btn_paste.isHidden())
        self.type("x")
        self.assertTrue(w.btn_paste.isHidden())
        with mock.patch.object(w.edit, "paste") as paste:
            w.btn_paste.clicked.emit()
        paste.assert_called_once()

    def test_light_dismiss(self) -> None:
        w = self.win
        self.win32.pids.update({w.hwnd: 4242, 700: 99, 701: 98})
        self.win32.classes.update({700: "Chrome_WidgetWin_1", 701: "Shell_TrayWnd"})
        self.open(None)
        self.assertTrue(w.is_dismissed_by(700))
        self.assertFalse(w.is_dismissed_by(701))
        self.assertFalse(w.is_dismissed_by(0))
        self.win32.foreground = 700
        w._check_away()
        self.assertEqual(self.events[-1], ("close", "away"))

    def test_clicking_away_while_it_grows_closes_it_once_open(self) -> None:
        w = self.win
        self.win32.pids[OTHER_APP], self.win32.classes[OTHER_APP] = OTHER_PID, "Chrome_WidgetWin_1"
        w._t.to = lambda *_a: None  # hold the opening motion at its first frame
        self.open()
        self.assertEqual((w.session.phase, w.session.was_active), ("opening", True))
        self.win32.foreground = OTHER_APP  # you click back into your document while the box is still growing
        w._check_away()  # the deactivation backstop; the App's light dismiss is ignored the same way while opening
        self.assertNotIn(("close", "away"), self.events)
        w._on_motion(1.0)  # the motion ends: the box would be open, inactive, and on top of your document
        settle()
        self.assertNotIn(("close", "away"), self.events)  # judged once the foreground settles, not at once
        fire_away_check(w)
        self.assertIn(("close", "away"), self.events)

    def test_still_in_front_once_open_nothing_is_checked(self) -> None:
        w = self.win
        w._t.to = lambda *_a: None
        self.open()
        w._on_motion(1.0)
        settle()
        self.assertFalse(w._away_timer.isActive())

    def test_light_dismiss_lets_a_passing_shell_window_go_by(self) -> None:
        # Win+. can put one of explorer's ApplicationFrameWindows in front for a few milliseconds, and the box
        # hears it lost activation; it is back in front before the check runs.
        w = self.win
        frame = 0x72D1A60
        self.win32.pids[frame], self.win32.classes[frame] = 14732, "ApplicationFrameWindow"
        self.open(("Text.", 0))
        self.assertEqual((w.session.phase, w.session.was_active), ("open", True))
        self.win32.foreground = frame
        QCoreApplication.sendEvent(w, QEvent(QEvent.Type.ActivationChange))
        settle()
        self.win32.foreground = w.hwnd
        fire_away_check(w)
        self.assertNotIn(("close", "away"), self.events)
        # The same passing window as the grow ends.
        w.session.phase = "opening"
        self.win32.foreground = frame
        w._on_motion(1.0)
        settle()
        self.win32.foreground = w.hwnd
        fire_away_check(w)
        self.assertNotIn(("close", "away"), self.events)

    def test_a_note_crossfades_into_the_hint_the_selection_calls_for(self) -> None:
        w = self.win
        self.open(None)
        self.type("One two")
        c = w.edit.textCursor()
        c.setPosition(4)
        c.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
        w.edit.setTextCursor(c)
        w._refresh()
        w.show_note("empty")
        w._end_note()  # the note's 6 s are up
        self.assertEqual(w.hint.content, ("hint", C.HINT_PARTIAL))
        self.assertEqual(w.hint._prev[0], "note")  # the note fades out; it doesn't vanish in one frame

    def test_the_hint_row_names_its_keys_apart(self) -> None:
        w = self.win
        self.open(None)
        self.assertEqual(w.hint.accessibleName(), "Ctrl+Enter reads it aloud. Esc goes back.")
        self.type("One two")
        c = w.edit.textCursor()
        c.setPosition(4)
        c.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
        w.edit.setTextCursor(c)
        w._refresh()
        self.assertEqual(w.hint.accessibleName(), "Ctrl+Enter reads the highlighted part. Esc goes back.")

    def test_the_count_leaves_out_what_cannot_be_read(self) -> None:
        w = self.win
        self.open(None)
        self.type("((( ))) …")
        w._refresh()
        self.assertEqual(w.info.parts, [])  # no "3 words" beside "nothing to read"
        self.type(" ok")
        w._refresh()
        self.assertEqual(w.info.parts, [("text", "1 word · under a minute")])

    def test_no_count_beside_the_nothing_to_read_note(self) -> None:
        # A page number over a line of punctuation: the count sees a word, the engine finds nothing to read.
        w = self.win
        self.open(None)
        self.type("Page 4\n-")
        w._refresh()
        self.assertEqual(w.info.parts, [("text", "2 words · under a minute")])
        w.show_note("nothing")
        self.assertEqual(w.info.parts, [])
        w._refresh()
        self.assertEqual(w.info.parts, [])
        w._end_note()
        self.assertEqual(w.info.parts, [("text", "2 words · under a minute")])

    def test_the_log_gets_counts_never_text(self) -> None:
        w = self.win
        self.open(None)
        with self.assertLogs("saylight.compose", "INFO") as logs:
            w.edit.insertFromMimeData(mime("private words"))
            w.close_animated("away")
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["Compose: pasted 13 characters (cut: no)", "Compose closed (away)"])

    def test_back_says_close_when_the_panel_is_hidden(self) -> None:
        self.open(None)
        self.assertEqual(self.win.btn_back.tooltip_text(), "Back to the panel (Esc). Your text stays here.")
        self.win.close_animated("esc")
        self.open(None, panel_visible=False)
        self.assertEqual(self.win.btn_back.tooltip_text(), "Close (Esc). Your text stays here.")

    def test_no_room_closes_for_the_screen(self) -> None:
        self.open(("Text.", 0))
        self.gui.screen.area = QRect(0, 0, 1280, 200)
        self.win.reanchor(QRect(0, 0, 440, 252))
        self.assertEqual(self.events[-1], ("close", "screen"))

    def test_reanchor_follows_a_text_size_change(self) -> None:
        w = self.win
        self.open(("Text.", 0))
        before = w.geometry()
        self.settings.set("text_size", "large")  # theme.changed: fonts, pitch and geometry follow at once
        lh = self.theme.line_height("reading")
        self.assertEqual(w.geometry().height(), C.chrome_height(self.theme) + w._lines * lh)
        self.assertEqual(w.geometry().bottom(), before.bottom())
        self.assertEqual(w.edit.document().firstBlock().blockFormat().lineHeight(), lh)


class HighContrastTests(unittest.TestCase):
    def test_system_colours(self) -> None:
        fake = FakeWin32()
        fake.patch(self)
        theme = make_theme(hc=True)
        edit = C.ComposeEdit(theme)
        pal = edit.palette()
        self.assertEqual(pal.color(pal.ColorGroup.Inactive, pal.ColorRole.Highlight).getRgb()[:3], (26, 235, 255))
        self.assertEqual(pal.color(pal.ColorRole.PlaceholderText).getRgb()[:3], (63, 242, 63))
        self.assertEqual(pal.color(pal.ColorRole.Base).getRgb()[:3], (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
