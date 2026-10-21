"""Tests for controls in ui/controls.py: the focusable Read aloud pill and Clear / Undo / Paste buttons of the Type
or paste box (keyboard activation, the keyboard-only focus ring, sizes and the room kept for the ring), and the
settings page's link line and navigation row.

Widgets are rendered into a QImage and never shown.
"""

import sys
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QFocusEvent, QFontMetricsF, QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from saylight.theme import Theme
from saylight.ui.controls import (G_CLEAR, G_OPEN, G_PLAY, G_UNDO, FOCUS_PAD, IconButton, LinkText, NavRow,
                                  PillButton, TextButton, text_button_size)

_app = None


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class FakeSettings(QObject):
    changed = Signal(str)

    def get(self, key: str) -> str:
        return {"text_size": "medium", "reading_tint": "auto", "reading_font": "verdana", "spacing": "wide"}[key]


class FakeSystem:
    def __init__(self, *, k: float = 1.0) -> None:
        self.k = k

    def color_scheme_dark(self) -> bool:
        return False

    def apps_use_light_theme(self) -> bool:
        return True

    def system_uses_light_theme(self) -> bool:
        return True

    def high_contrast(self) -> bool:
        return False

    def animations_enabled(self) -> bool:
        return False

    def text_scale_factor(self) -> float:
        return self.k

    def accent_palette(self) -> None:
        return None

    def color_prevalence(self) -> bool:
        return False

    def sys_colors(self) -> dict:
        return {}

    def font_families(self) -> frozenset[str]:
        from PySide6.QtGui import QFontDatabase
        return frozenset(QFontDatabase.families())


class FakeTips:
    def __getattr__(self, name: str):
        return lambda *args: None


def theme(k: float = 1.0) -> Theme:
    return Theme(FakeSettings(), system=FakeSystem(k=k))


def key(widget: QWidget, k: Qt.Key, mods=Qt.KeyboardModifier.NoModifier, *, repeat: bool = False) -> bool:
    event = QKeyEvent(QEvent.Type.KeyPress, k, mods, "", repeat)
    QCoreApplication.sendEvent(widget, event)
    return event.isAccepted()


def render(widget: QWidget) -> QImage:
    img = QImage(widget.size(), QImage.Format.Format_ARGB32)
    img.fill(0)
    widget.render(img)
    return img


class FocusableControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = theme()
        self.parent = QWidget()
        self.addCleanup(self.parent.deleteLater)
        self.pill = PillButton(self.t, FakeTips(), G_PLAY, "Read aloud", self.parent)
        self.clicks: list[int] = []
        self.pill.clicked.connect(lambda: self.clicks.append(1))

    def test_tab_focus_only(self) -> None:
        self.assertEqual(self.pill.focusPolicy(), Qt.FocusPolicy.TabFocus)
        panel_button = IconButton(self.t, FakeTips(), G_CLEAR, parent=self.parent)
        self.assertEqual(panel_button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertEqual((panel_button.pad(), panel_button.face()), (0, panel_button.rect().toRectF()))

    def test_space_and_enter_click(self) -> None:
        for k in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.assertTrue(key(self.pill, k))
        self.assertTrue(key(self.pill, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier))
        self.assertEqual(len(self.clicks), 4)

    def test_ctrl_enter_and_repeats_do_not_click(self) -> None:
        """Ctrl+Enter belongs to the box (read aloud), so a focused button lets it through."""
        self.assertFalse(key(self.pill, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier))
        key(self.pill, Qt.Key.Key_Space, repeat=True)
        self.pill.setEnabled(False)
        key(self.pill, Qt.Key.Key_Space)
        self.assertEqual(self.clicks, [])

    def test_place_keeps_room_for_the_ring(self) -> None:
        self.pill.place(QRect(12, 416, 122, 40))
        self.assertEqual(self.pill.geometry(), QRect(12 - FOCUS_PAD, 416 - FOCUS_PAD, 128, 46))
        face = self.pill.face()
        self.assertEqual((face.x(), face.y(), face.width(), face.height()), (FOCUS_PAD, FOCUS_PAD, 122, 40))

    def test_ring_only_after_keyboard_focus(self) -> None:
        self.pill.place(QRect(10, 10, 122, 40))
        self.pill.hasFocus = lambda: True  # a window that is never shown cannot hold real focus
        outer = QPoint(FOCUS_PAD + 61, 1)  # inside the 2 DIP ring above the face
        primary = self.t.color("TextPrimary").rgb()
        background = render(self.pill).pixel(outer)
        self.assertNotEqual(background, primary)
        QCoreApplication.sendEvent(self.pill, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason))
        self.assertFalse(self.pill.focus_ring_shown())
        QCoreApplication.sendEvent(self.pill, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
        self.assertTrue(self.pill.focus_ring_shown())
        self.assertEqual(render(self.pill).pixel(outer), primary)
        QCoreApplication.sendEvent(self.pill, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason))
        self.assertFalse(self.pill.focus_ring_shown())
        self.assertEqual(render(self.pill).pixel(outer), background)


class FocusableIconButtonTests(unittest.TestCase):
    def test_back_button_keeps_room_for_the_ring(self) -> None:
        for k in (1.0, 1.5):
            with self.subTest(k=k):
                t = theme(k)
                back = IconButton(t, FakeTips(), G_CLEAR, size=32, small_glyph=True, focusable=True)
                pad = t.px(FOCUS_PAD)
                self.assertEqual(back.focusPolicy(), Qt.FocusPolicy.TabFocus)
                self.assertEqual(back.width(), t.px(32) + 2 * pad)
                back.place(QRect(596, 8, t.px(32), t.px(32)))
                self.assertEqual(back.geometry(), QRect(596 - pad, 8 - pad, t.px(32) + 2 * pad, t.px(32) + 2 * pad))
                self.assertEqual(back.face().width(), t.px(32))


class PillTests(unittest.TestCase):
    def test_natural_size(self) -> None:
        for k in (1.0, 1.5):
            with self.subTest(k=k):
                t = theme(k)
                pill = PillButton(t, FakeTips(), G_PLAY, "Read aloud")
                label = QFontMetricsF(t.font("status")).horizontalAdvance("Read aloud")
                size = pill.natural_size()
                self.assertEqual(size.height(), t.px(40))
                self.assertGreaterEqual(size.width(), t.px(34) + label + t.px(16))
                self.assertLess(size.width(), t.px(34) + label + t.px(16) + 1)

    def test_disabled_fill(self) -> None:
        t = theme()
        pill = PillButton(t, FakeTips(), G_PLAY, "Read aloud")
        pill.place(QRect(0, 0, 122, 40))
        spot = QPoint(FOCUS_PAD + 100, FOCUS_PAD + 4)  # right of the label, inside the pill
        self.assertEqual(render(pill).pixel(spot), t.color("Accent").rgb())
        pill.setEnabled(False)
        self.assertEqual(render(pill).pixel(spot), t.color("AccentDisabled").rgb())


class TextButtonTests(unittest.TestCase):
    def test_size_fits_the_longest_label(self) -> None:
        t = theme()
        fm = QFontMetricsF(t.font("body"))
        size = text_button_size(t, ("Clear", "Undo"))
        widest = max(fm.horizontalAdvance("Clear"), fm.horizontalAdvance("Undo"))
        self.assertEqual(size.height(), 32)
        self.assertEqual(size.width(), int(36 + widest + 14 + 0.999))

    def test_set_content_swaps_label_and_name(self) -> None:
        t = theme()
        button = TextButton(t, FakeTips(), G_CLEAR, "Clear", name="Clear text")
        self.assertEqual(button.accessibleName(), "Clear text")
        button.set_content(G_UNDO, "Undo", "Undo clear")
        self.assertEqual((button.glyph, button.label, button.accessibleName()), (G_UNDO, "Undo", "Undo clear"))
        self.assertEqual(button.swap.value, 1.0)  # reduced motion: no crossfade

    def test_chip_uses_the_reading_keycap_colours(self) -> None:
        t = theme()
        chip = TextButton(t, FakeTips(), G_CLEAR, "Paste", style="chip")
        chip.place(QRect(0, 0, 83, 32))
        spot = QPoint(FOCUS_PAD + 80, FOCUS_PAD + 16)  # the right padding: fill only
        self.assertEqual(render(chip).pixel(spot), t.reading_colors()["keycap_fill"].rgb())



def mouse(widget: QWidget, kind: QEvent.Type, pos: QPointF) -> bool:
    buttons = Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, pos, pos, pos, Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)
    QCoreApplication.sendEvent(widget, event)
    return event.isAccepted()


class LinkTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = theme()
        self.line = LinkText(self.t, FakeTips())
        self.line.resize(400, 20)
        self.line.set_parts([("text", "Saylight 1.3 is ready"), ("sep", " · "), ("install", "Install")])
        self.clicks: list[str] = []
        self.line.linkClicked.connect(self.clicks.append)

    def test_links_are_laid_out_after_the_text(self) -> None:
        fm = QFontMetricsF(self.t.font("body"))
        r = self.line.link_rect("install")
        self.assertAlmostEqual(r.left(), fm.horizontalAdvance("Saylight 1.3 is ready · "), places=3)
        self.assertEqual(self.line.link_keys(), ["install"])
        self.assertEqual(self.line.accessibleName(), "Saylight 1.3 is ready · Install")

    def test_a_click_on_a_link_emits_its_key(self) -> None:
        centre = self.line.link_rect("install").center()
        self.assertTrue(mouse(self.line, QEvent.Type.MouseButtonPress, centre))
        mouse(self.line, QEvent.Type.MouseButtonRelease, centre)
        self.assertEqual(self.clicks, ["install"])

    def test_presses_on_the_text_pass_through(self) -> None:
        self.assertFalse(mouse(self.line, QEvent.Type.MouseButtonPress, QPointF(5, 10)))
        self.assertEqual(self.clicks, [])

    def test_a_release_off_the_link_does_nothing(self) -> None:
        mouse(self.line, QEvent.Type.MouseButtonPress, self.line.link_rect("install").center())
        mouse(self.line, QEvent.Type.MouseButtonRelease, QPointF(5, 10))
        self.assertEqual(self.clicks, [])

    def test_disabled_links_take_no_clicks(self) -> None:
        self.line.setEnabled(False)
        centre = self.line.link_rect("install").center()
        mouse(self.line, QEvent.Type.MouseButtonPress, centre)
        mouse(self.line, QEvent.Type.MouseButtonRelease, centre)
        self.assertEqual(self.clicks, [])
        self.assertIsNone(self.line._key_at(centre))

    def test_the_tooltip_follows_the_pointer(self) -> None:
        self.line.link_tips = {"install": "Install it"}
        self.line.text_tip = "About the update"
        self.assertEqual(self.line.tooltip_text(), "About the update")
        self.line._set_hover_key("install")
        self.assertEqual(self.line.tooltip_text(), "Install it")


class NavRowTests(unittest.TestCase):
    def test_click_name_and_detail(self) -> None:
        row = NavRow(theme(), FakeTips(), "Privacy and licences", glyph=G_OPEN)
        row.resize(416, 40)
        clicks: list[int] = []
        row.clicked.connect(lambda: clicks.append(1))
        mouse(row, QEvent.Type.MouseButtonPress, QPointF(200, 20))
        mouse(row, QEvent.Type.MouseButtonRelease, QPointF(200, 20))
        self.assertEqual(clicks, [1])
        self.assertEqual(row.accessibleName(), "Privacy and licences")
        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.NoFocus)
        row.set_detail("12 words")
        self.assertEqual(row.detail, "12 words")
        render(row)  # paints with a detail and a glyph


if __name__ == "__main__":
    unittest.main()
