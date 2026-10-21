"""Tests for ui/sheet.py: where a sheet goes (it always covers the panel), the shared chrome, the line field, and
the window's fixed flags, Esc, Enter and close handling. No window is shown and nothing takes the foreground.
"""

import random
import unittest

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QWidget

from saylight.tests.test_controls import FakeTips, render, setUpModule, theme  # noqa: F401 (setUpModule runs here)
from saylight.ui.compose import compose_rect
from saylight.ui.controls import G_SPEECH
from saylight.ui.sheet import (SHEET_FLAGS, DialogHost, LineField, Sheet, SheetSession, sheet_chrome, sheet_rect)


class FakePanel:
    def __init__(self, size: QSize) -> None:
        self._size = size
        self.covered: list[bool] = []

    def size(self) -> QSize:
        return self._size

    def set_covered(self, on: bool) -> None:
        self.covered.append(on)

    def _bottom_margin(self, _screen) -> int:
        return 12

    def rest_rect(self) -> QRect:
        return QRect(QRect(100, 100, 0, 0).topLeft(), self._size)


class FakeCtlSettings(QObject):
    changed = Signal(str)

    def get(self, key: str):
        return {"panel.position": None}[key]


class FakeCtl:
    def __init__(self) -> None:
        self.theme = theme()
        self.tips = FakeTips()
        self.settings = FakeCtlSettings()
        self.panel = FakePanel(QSize(440, 252))


class WordSheet(Sheet):
    def __init__(self, ctl) -> None:
        super().__init__(ctl, name="Pronunciations", glyph=G_SPEECH, title="Pronunciations", natural=QSize(640, 468))
        self.field = LineField(self.theme, self.body, style="well", name="Word")
        self.primaries = 0
        self.laid_out: list[tuple[int, int]] = []

    def layout_content(self, W: int, H: int) -> None:
        self.laid_out.append((W, H))
        self.field.setGeometry(self.theme.px(132), self.theme.px(48), W - self.theme.px(154), self.theme.px(34))

    def focus_target(self) -> QWidget:
        return self.field

    def primary(self) -> None:
        self.primaries += 1


class SheetRectTests(unittest.TestCase):
    def test_a_sheet_always_covers_the_panel(self) -> None:
        rng = random.Random(13)
        area = QRect(0, 0, 1920, 1040)
        for _ in range(2000):
            w, h = rng.choice((388, 440, 476, 640)), rng.choice((200, 252, 300, 360))
            rest = QRect(rng.randint(0, area.width() - w), rng.randint(0, area.height() - h), w, h)
            natural = QSize(rng.randint(200, 900), rng.randint(150, 900))
            snap_x = rng.choice((None, "left", "right"))
            got = sheet_rect(rest, area, natural, rest.size(), snap_x=snap_x, margin=12, bottom_margin=12)
            self.assertTrue(got.contains(rest), (rest, natural, snap_x, got))
            self.assertEqual(got.size(), QSize(max(w, natural.width()), max(h, natural.height())))

    def test_the_panel_rect_also_covers_a_panel_off_the_work_area(self) -> None:
        rest = QRect(1700, 900, 440, 252)  # hanging over the bottom-right corner
        got = sheet_rect(rest, QRect(0, 0, 1920, 1040), QSize(640, 468), rest.size(), margin=12, bottom_margin=12)
        self.assertTrue(got.contains(rest))

    def test_it_is_placed_like_the_type_or_paste_box(self) -> None:
        rest, area = QRect(700, 600, 440, 252), QRect(0, 0, 1920, 1040)
        got = sheet_rect(rest, area, QSize(640, 468), QSize(440, 252), snap_x="right", margin=12, bottom_margin=48)
        self.assertEqual(got, compose_rect(rest, area, QSize(640, 468), snap_x="right", margin=12, bottom_margin=48))

    def test_panel_mode_is_exactly_the_rest_rect(self) -> None:
        rest = QRect(700, 600, 440, 252)
        self.assertEqual(sheet_rect(rest, QRect(0, 0, 1920, 1040), QSize(640, 360), rest.size(), mode="panel"), rest)

    def test_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            sheet_rect(QRect(0, 0, 440, 252), QRect(0, 0, 1920, 1040), QSize(1, 1), QSize(440, 252), mode="grow")


class ChromeTests(unittest.TestCase):
    def test_positions_at_440_by_252(self) -> None:
        lay = sheet_chrome(theme(), 440, 252)
        self.assertEqual(lay["glyph"], QRect(22, 16, 16, 16))
        self.assertEqual(lay["back"], QRect(396, 8, 32, 32))
        self.assertEqual(lay["title"].left(), 46)
        self.assertEqual(lay["title"].right() + 1, 440 - 52)
        self.assertEqual(lay["hint"], QRect(22, 180, 396, 16))
        self.assertEqual(lay["footer"].center().y() + 1, 252 - 32)  # QRect's centre rounds down

    def test_info_is_right_aligned_and_the_title_stops_before_it(self) -> None:
        lay = sheet_chrome(theme(), 640, 468, info_w=80.4)
        self.assertEqual(lay["info"].right() + 1, 640 - 52)
        self.assertEqual(lay["info"].width(), 81)
        self.assertEqual(lay["title"].right() + 1, lay["info"].left() - 12)

    def test_scales_with_text_size(self) -> None:
        t = theme(1.5)
        lay = sheet_chrome(t, t.px(440), t.px(252))
        self.assertEqual(lay["back"], QRect(t.px(440) - t.px(44), t.px(8), t.px(32), t.px(32)))
        self.assertEqual(lay["hint"].top(), t.px(252) - t.px(72))


class LineFieldTests(unittest.TestCase):
    def test_size_and_fonts(self) -> None:
        t = theme()
        well = LineField(t, style="well", name="Say it as")
        control = LineField(t, name="Find a word", placeholder="Find a word")
        self.assertEqual(well.height(), 34)
        self.assertLessEqual(well.font().pixelSize(), 18)
        self.assertEqual(control.font().pixelSize(), t.font("body").pixelSize())
        for field in (well, control):
            font = field.font()
            self.assertFalse(font.bold() or font.italic() or font.underline())
        self.assertEqual(control.accessibleName(), "Find a word")
        self.assertEqual(control.placeholderText(), "Find a word")
        self.assertFalse(control.hasFrame())
        well.resize(300, 34)
        render(well)

    def test_colours(self) -> None:
        t = theme()
        c = t.reading_colors()
        self.assertEqual(LineField(t, style="well").colors()[:2], (c["background"], c["edge"]))
        self.assertEqual(LineField(t).colors()[:2], (t.color("ControlFill"), t.color("ControlStroke")))
        self.assertEqual(LineField(t, in_well=True).colors()[:2], (c["keycap_fill"], c["keycap_stroke"]))

    def test_unknown_style(self) -> None:
        with self.assertRaises(ValueError):
            LineField(theme(), style="glass")


class SheetWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctl = FakeCtl()
        self.sheet = WordSheet(self.ctl)
        self.addCleanup(self.sheet.deleteLater)
        self.requests: list[str] = []
        self.sheet.closeRequested.connect(self.requests.append)

    def test_window_recipe(self) -> None:
        s = self.sheet
        self.assertEqual(s.windowFlags() & SHEET_FLAGS, SHEET_FLAGS)
        self.assertFalse(s.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertIsNone(s.parent())
        self.assertFalse(s.isModal())
        self.assertEqual(s.windowTitle(), "Pronunciations – Saylight")
        self.assertEqual(s.btn_back.accessibleName(), "Back to the panel")
        self.assertEqual(s.btn_back.focusPolicy(), Qt.FocusPolicy.TabFocus)
        self.assertEqual(s.size(), QSize(640, 468))
        self.assertEqual(s.session, SheetSession())
        self.assertFalse(s.is_open)

    def test_esc_and_reject_ask_to_close_and_enter_runs_the_primary_action(self) -> None:
        s = self.sheet
        s.reject()
        s.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        s.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.requests, ["esc", "esc"])
        self.assertEqual(s.primaries, 1)

    def test_alt_f4_is_back_while_open(self) -> None:
        s = self.sheet
        s.session.phase = "open"
        event = QCloseEvent()
        s.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(self.requests, ["back"])
        s.session.phase = "closed"
        event = QCloseEvent()
        s.closeEvent(event)
        self.assertTrue(event.isAccepted())

    def test_closing_a_closed_sheet_does_nothing(self) -> None:
        closed: list[str] = []
        self.sheet.closed.connect(closed.append)
        self.sheet.close_animated("esc")
        self.assertEqual(closed, [])

    def test_switch_hides_at_once_and_uncovers_the_panel(self) -> None:
        s = self.sheet
        closed: list[str] = []
        s.closed.connect(closed.append)
        s.session.phase = "open"
        s._covered = True
        s.close_animated("switch")
        self.assertEqual(closed, ["switch"])
        self.assertEqual(self.ctl.panel.covered, [False])
        self.assertEqual(s.session, SheetSession())

    def test_the_panel_is_covered_from_167_ms(self) -> None:
        s = self.sheet
        s.session.phase, s._motion = "opening", "open"
        s._on_motion(160 / 250)
        self.assertEqual(self.ctl.panel.covered, [])
        s._on_motion(170 / 250)
        self.assertEqual(self.ctl.panel.covered, [True])
        s._on_motion(1.0)
        self.assertEqual(s.session.phase, "open")
        self.assertFalse(s._body_fade.isEnabled())

    def test_hint_and_notes(self) -> None:
        s = self.sheet
        s.set_hint([("key", "Enter"), ("text", " saves")])
        self.assertEqual(s.hint.accessibleName(), "Enter saves")
        s.show_note("Your list is full.")
        s.set_hint([("key", "Esc"), ("text", " goes back")])  # a note stays until it ends
        self.assertTrue(s.hint.note)
        s.end_note()
        self.assertEqual(s.hint.accessibleName(), "Esc goes back")

    def test_info_narrows_the_title(self) -> None:
        s = self.sheet
        before = s.title.width()
        s.set_info("12 words")
        self.assertLess(s.title.width(), before)

    def test_content_is_laid_out_only_once_built(self) -> None:
        s = self.sheet
        self.assertEqual(s.laid_out, [])
        s._layout_at(640, 468)
        self.assertEqual(s.laid_out, [(640, 468)])
        self.assertEqual(s.field.geometry(), QRect(132, 48, 486, 34))


class DialogHostTests(unittest.TestCase):
    def test_idle_host(self) -> None:
        host = DialogHost()
        self.addCleanup(host.deleteLater)
        self.assertFalse(host.busy)
        self.assertFalse(host.isVisible())
        self.assertIsNone(host.parent())
        self.assertEqual(host.windowOpacity(), 0.0)
        self.assertEqual(host.windowFlags() & SHEET_FLAGS, SHEET_FLAGS)


if __name__ == "__main__":
    unittest.main()
