"""Tests for ui/page_range.py: clamping and swapping a range, the note and its time estimate, the page
steppers (keys, typing, mouse, wheel), Enter, Esc, Cancel and Tab, where the window goes, and light dismiss.
No window is shown and nothing takes the foreground."""

import os
import unittest
from unittest import mock

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest

from saylight import win32
from saylight.tests.test_controls import render, setUpModule  # noqa: F401 (setUpModule runs here)
from saylight.tests.test_sheet import FakeCtl, FakePanel
from saylight.ui import page_range as PR
from saylight.ui.sheet import SHEET_FLAGS, Sheet


class HelperTests(unittest.TestCase):
    def test_clamp_range(self) -> None:
        self.assertEqual(PR.clamp_range(1, 50, 160), (1, 50))
        self.assertEqual(PR.clamp_range(120, 60, 160), (60, 120))   # swapped
        self.assertEqual(PR.clamp_range(0, 999, 160), (1, 160))
        self.assertEqual(PR.clamp_range(5, 5, 3), (3, 3))
        self.assertEqual(PR.clamp_range(4, 2, 0), (1, 1))

    def test_eta(self) -> None:
        self.assertEqual([PR.eta_text(s) for s in (0, 1, 40, 41, 55, 60, 128, 987, 3600, 3900)],
                         ["10 s", "10 s", "40 s", "50 s", "1 min", "1 min", "2 min", "16 min", "1 h", "1 h 5 min"])

    def test_note(self) -> None:
        self.assertEqual(PR.range_note(50, 50), "50 pages · about 40 s to find the text")
        self.assertEqual(PR.range_note(1, 1), "1 page · about 10 s to find the text")
        self.assertEqual(PR.range_note(1234, 1234), "1,234 pages · about 16 min to find the text")
        self.assertEqual(PR.range_note(3, 0), "3 pages · no scanned pages")


def mouse(kind, pos) -> QMouseEvent:
    return QMouseEvent(kind, QPointF(pos), QPointF(pos), Qt.MouseButton.LeftButton,
                       Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)


class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctl = FakeCtl()
        self.ctl.panel = FakePanel(QSize(476, 252))
        self.w = PR.PageRangeWindow(self.ctl)
        self.addCleanup(self.w.deleteLater)
        self.chosen, self.closes = [], []
        self.w.rangeChosen.connect(lambda a, b: self.chosen.append((a, b)))
        self.w.closeRequested.connect(self.closes.append)
        self.scanned = lambda a, b: sum(1 for p in range(a, b + 1) if p > 20)  # pages 21-160 are scans
        self.w.prepare(160, self.scanned)

    def test_the_window(self) -> None:
        w = self.w
        self.assertIsInstance(w, Sheet)
        self.assertEqual(w.windowFlags() & SHEET_FLAGS, SHEET_FLAGS)
        self.assertEqual(w.windowTitle(), "Choose pages – Saylight")
        self.assertEqual(w.title.parts, [("text", "Choose pages")])
        self.assertEqual(w.info.parts, [("text", "160 pages")])
        self.assertEqual(w.btn_back.tooltip_text(), "Back to the panel (Esc)")
        self.assertEqual((w.first.field.accessibleName(), w.last.field.accessibleName()), ("First page", "Last page"))
        self.assertEqual((w.pill.accessibleName(), w.btn_cancel.accessibleName()), ("Read these pages", "Cancel"))
        self.assertFalse(self.closes)

    def test_defaults_and_the_live_note(self) -> None:
        w = self.w
        self.assertEqual((w.first.value(), w.last.value()), (1, 50))
        self.assertEqual(w.note.parts, [("text", "50 pages · about 30 s to find the text")])  # 30 scans
        w.last.set_value(10)
        self.assertEqual(w.note.parts, [("text", "10 pages · no scanned pages")])
        w.prepare(7, None)
        self.assertEqual((w.first.value(), w.last.value(), w.first.n), (1, 7, 7))
        self.assertEqual(w.lbl_of.parts, [("text", "of 7")])

    def test_layout_at_476(self) -> None:
        w = self.w
        w._layout_at(476, 168)
        pad = PR.FOCUS_PAD

        def face(c):
            return c.geometry().adjusted(pad, pad, -pad, -pad)
        self.assertEqual(face(w.first), QRect(104, 52, 112, 32))
        self.assertEqual(face(w.last), QRect(252, 52, 112, 32))
        self.assertEqual(w.lbl_of.geometry().x(), 376)
        self.assertEqual(w.note.geometry(), QRect(22, 92, 432, 16))
        self.assertEqual(w.hint.geometry().y(), 92)
        self.assertEqual(face(w.pill).topLeft(), QPoint(12, 116))
        self.assertEqual((face(w.btn_cancel).right() + 1, face(w.btn_cancel).top()), (464, 120))  # on 136
        self.assertEqual(w.btn_back.geometry().adjusted(pad, pad, -pad, -pad), QRect(432, 8, 32, 32))
        render(w)

    def test_where_it_goes(self) -> None:
        area = QRect(0, 0, 1920, 1032)
        screen = mock.Mock()
        screen.availableGeometry.return_value = area
        with mock.patch.object(PR.QGuiApplication, "screenAt", return_value=screen):
            low = self.w._geometry(QRect(722, 768, 476, 252))    # bottom-anchored: the bottoms align
            high = self.w._geometry(QRect(722, 12, 476, 252))    # top-anchored: the tops align
        self.assertEqual(low, QRect(722, 852, 476, 168))
        self.assertEqual(high, QRect(722, 12, 476, 168))

    def test_read_swaps_and_closes(self) -> None:
        w = self.w
        w.first.set_value(120)
        w.last.set_value(60)
        self.assertEqual(w.note.parts, [("text", "61 pages · about 50 s to find the text")])
        QTest.keyClick(w.first.field, Qt.Key.Key_Return)
        self.assertEqual((self.chosen, self.closes), ([(60, 120)], ["read"]))
        w.pill.clicked.emit()
        self.assertEqual(len(self.chosen), 2)

    def test_esc_back_and_cancel_keep_the_card(self) -> None:
        QTest.keyClick(self.w.last.field, Qt.Key.Key_Escape)
        self.w.btn_back.clicked.emit()
        self.w.btn_cancel.clicked.emit()
        self.assertEqual(self.closes, ["esc", "back", "back"])
        self.assertEqual(self.chosen, [])

    def test_tab_order(self) -> None:
        w = self.w
        order, widget = [], w.first.field
        while len(order) < 4:
            widget = widget.nextInFocusChain()
            if widget.focusPolicy() & Qt.FocusPolicy.TabFocus:
                order.append(widget)
        self.assertEqual(order, [w.last.field, w.pill, w.btn_cancel, w.btn_back])
        self.assertIs(w.focus_target(), w.first.field)

    def test_open_selects_from(self) -> None:
        with mock.patch.object(Sheet, "open_at", return_value=True) as base:
            self.assertTrue(self.w.open_at(QRect(722, 768, 476, 252), pages=12, scanned_in=None, return_to=77))
        self.assertEqual(base.call_args.kwargs["return_to"], 77)
        self.assertEqual((self.w.n, self.w.last.value()), (12, 12))
        self.assertEqual(self.w.first.field.selectedText(), "1")

    def test_light_dismiss(self) -> None:
        w = self.w
        w.session.phase, w.session.was_active = "open", True
        with mock.patch.multiple(win32, get_window_pid=lambda h: 4242 if h == 5 else os.getpid(),
                                 get_class_name=lambda h: "Notepad"):
            self.assertTrue(w.is_dismissed_by(5))
            self.assertFalse(w.is_dismissed_by(6))  # our own window


class StepperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctl = FakeCtl()
        self.w = PR.PageRangeWindow(self.ctl)
        self.addCleanup(self.w.deleteLater)
        self.w.prepare(160, None)
        self.s = self.w.last
        self.f = self.s.field

    def test_keys(self) -> None:
        for key, value in ((Qt.Key.Key_Up, 51), (Qt.Key.Key_PageUp, 61), (Qt.Key.Key_Down, 60),
                           (Qt.Key.Key_PageDown, 50)):
            QTest.keyClick(self.f, key)
            self.assertEqual(self.s.value(), value)
        self.assertEqual(self.f.selectedText(), "50")  # typing replaces it
        self.s.set_value(155)
        QTest.keyClick(self.f, Qt.Key.Key_PageUp)
        self.assertEqual(self.s.value(), 160)

    def test_typing_is_digits_only_and_clamped(self) -> None:
        self.f.selectAll()
        QTest.keyClicks(self.f, "1a2")
        self.assertEqual((self.f.text(), self.s.value()), ("12", 12))
        self.f.selectAll()
        QTest.keyClicks(self.f, "999")
        self.assertEqual((self.f.text(), self.s.value()), ("160", 160))
        self.f.selectAll()
        QTest.keyClicks(self.f, "0")
        self.assertEqual(self.s.value(), 1)
        self.f.setText("")
        self.s.commit()
        self.assertEqual(self.f.text(), "1")  # an empty field goes back to the value
        self.assertEqual(self.f.maxLength(), 5)

    def test_mouse_steps_and_holding_speeds_up(self) -> None:
        s = self.s
        plus = s._zones()["plus"].center()
        s.mouseMoveEvent(mouse(QEvent.Type.MouseMove, plus))
        s.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, plus))
        self.assertEqual(s.value(), 51)
        self.assertEqual(s._repeat.interval(), PR.REPEAT_MS[0])
        s._on_repeat()
        self.assertEqual((s.value(), s._repeat.interval()), (52, PR.REPEAT_MS[1]))
        s._held_since -= PR.BIG_STEP_AFTER_S
        s._on_repeat()
        self.assertEqual(s.value(), 62)
        s.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, plus))
        self.assertFalse(s._repeat.isActive())
        s.set_value(1)
        minus = s._zones()["minus"].center()
        s.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, minus))  # nothing below page 1
        self.assertEqual(s.value(), 1)
        self.assertFalse(s._repeat.isActive())

    def test_wheel(self) -> None:
        def wheel(dy):
            return QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, dy),
                               Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                               Qt.ScrollPhase.NoScrollPhase, False)
        self.s.wheelEvent(wheel(240))
        self.assertEqual(self.s.value(), 52)
        self.s.wheelEvent(wheel(-120))
        self.assertEqual(self.s.value(), 51)

    def test_keyboard_focus_draws_the_ring(self) -> None:
        rings = []  # counted, not mocked: a mock would keep the painter alive past the paint
        for ring, expected in ((True, 1), (False, 0)):
            self.f.ring = ring
            with mock.patch.object(self.f, "hasFocus", return_value=True), \
                    mock.patch.object(PR, "paint_focus_ring", new=lambda *_a: rings.append(1)):
                render(self.s)
            self.assertEqual(len(rings), expected)
            rings.clear()


if __name__ == "__main__":
    unittest.main()
