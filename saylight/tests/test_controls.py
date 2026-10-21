"""Tests for controls in ui/controls.py: the focusable Read aloud pill and Clear / Undo / Paste buttons of the Type
or paste box (keyboard activation, the keyboard-only focus ring, sizes and the room kept for the ring), and the
settings page's link line and navigation row, and the v1.3 additions: glyphs, action chips with label overrides,
described and focusable toggle rows, the focusable segmented control and focusable navigation rows. Then 1.4's
glyphs, the icon() stand-in for Segoe MDL2 Assets, and the chips for reading from the screen and scans.

Widgets are rendered into a QImage and never shown.
"""

import sys
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QFocusEvent, QFont, QFontInfo, QFontMetricsF, QImage, QKeyEvent, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from saylight.theme import Theme
from saylight.ui import controls as C
from saylight.ui.controls import (ACTION_PARTS, DESCRIBED_H, G_CLEAR, G_OPEN, G_PLAY, G_UNDO, FOCUS_PAD, TOGGLE_H,
                                  IconButton, LinkText, NavRow, PillButton, Segmented, TextButton, ToggleRow,
                                  action_label, measure_parts, paint_parts, text_button_size)

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


class GlyphTests(unittest.TestCase):
    def test_v13_code_points(self) -> None:
        expected = {"G_MORE": 0xE712, "G_OPEN_FILE": 0xE838, "G_DOCUMENT": 0xE8A5, "G_SAVE": 0xE74E,
                    "G_DONE": 0xE930, "G_FOLDER": 0xEC50, "G_SPEECH": 0xEFA9, "G_HISTORY": 0xE81C,
                    "G_LOCK": 0xE72E, "G_EYE": 0xE7B3, "G_EYE_OFF": 0xED1A, "G_TEST": 0xE767, "G_DELETE": 0xE74D,
                    "G_FIND": 0xE721, "G_IMPORT": 0xE8B5, "G_EXPORT": 0xEDE1, "G_CANCEL": 0xE711, "G_ARROW": 0xE72A,
                    "G_CHEVRON": 0xE76C, "G_SETTINGS": 0xE713, "G_PLUS": 0xE710, "G_CHECK": 0xE73E,
                    "G_RETRY": 0xE72C, "G_COLLAPSE": 0xE73F}
        for name, code in expected.items():
            self.assertEqual(getattr(C, name), chr(code), name)

    def test_the_links_glyph_is_unchanged(self) -> None:
        self.assertEqual(G_OPEN, chr(0xE8A7))  # OpenInNewWindow, for "Privacy and licences"

    def test_v14_code_points(self) -> None:
        expected = {"G_SCREEN": 0xF683, "G_SCREEN_MDL2": 0xE9A6, "G_LANGUAGE": 0xF2B7, "G_SCAN": 0xE8FE,
                    "G_PAGES": 0xE7C3}
        for name, code in expected.items():
            self.assertEqual(getattr(C, name), chr(code), name)

    def test_icon_swaps_only_the_screen_glyph_outside_segoe_fluent_icons(self) -> None:
        class IconTheme:
            def __init__(self, family: str) -> None:
                self.family = family

            def font(self, role: str) -> QFont:
                self.role = role
                return QFont(self.family)

        mdl2 = IconTheme("Segoe MDL2 Assets")
        self.assertEqual(C.icon(mdl2, C.G_SCREEN), C.G_SCREEN_MDL2)
        self.assertEqual(mdl2.role, "icon16")
        self.assertEqual(C.icon(IconTheme("Arial"), C.G_SCREEN), C.G_SCREEN_MDL2)  # any other resolved family
        for glyph in (C.G_LANGUAGE, C.G_SCAN, C.G_PAGES, C.G_PLAY, C.G_RETRY):  # all in both fonts
            self.assertEqual(C.icon(mdl2, glyph), glyph)
        fluent = IconTheme("Segoe Fluent Icons")
        if QFontInfo(QFont("Segoe Fluent Icons")).family() != "Segoe Fluent Icons":
            self.skipTest("Segoe Fluent Icons is not installed")
        self.assertEqual(C.icon(fluent, C.G_SCREEN), C.G_SCREEN)
        self.assertEqual(C.icon(theme(), C.G_SCREEN), C.G_SCREEN)


class ActionChipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = theme()

    def test_new_keys(self) -> None:
        self.assertEqual(ACTION_PARTS["open"], (C.G_OPEN_FILE, "Open a file"))
        self.assertEqual(ACTION_PARTS["continue"][0], C.G_HISTORY)
        self.assertEqual(ACTION_PARTS["password"], (C.G_LOCK, "Enter password"))
        self.assertEqual(ACTION_PARTS["retry"], (C.G_RETRY, "Try again"))
        self.assertEqual(ACTION_PARTS["type"], (C.G_TYPE, "Type or paste text"))  # the 1.1 keys stay

    def test_screen_and_scan_keys(self) -> None:
        expected = {"screen": (C.G_SCREEN, "Read from screen"), "screen_retry": (C.G_SCREEN, "Try again"),
                    "scan_retry": (C.G_RETRY, "Try again"), "lang_settings": (C.G_LANGUAGE, "Open language settings"),
                    "scan_first": (C.G_PLAY, "First 50 pages"), "scan_choose": (C.G_PAGES, "Choose pages…")}
        for name, part in expected.items():
            self.assertEqual(ACTION_PARTS[name], part, name)
        self.assertEqual(ACTION_PARTS["scan_all"][0], C.G_PLAY)
        self.assertEqual(action_label("scan_all", {"scan_all": "All 160 pages"}), "All 160 pages")

    def test_label_overrides(self) -> None:
        label = "Continue Smith 2024 from page 14"
        self.assertEqual(action_label("continue", {"continue": label}), label)
        self.assertEqual(action_label("open", {"continue": label}), "Open a file")
        self.assertEqual(action_label("retry", {"retry": ""}), "Try again")  # an empty override is no override
        parts = [("action", "open"), ("action", "continue")]
        plain = measure_parts(self.t, parts)
        named = measure_parts(self.t, parts, labels={"continue": label})
        fm = QFontMetricsF(self.t.font("body"))
        self.assertAlmostEqual(named - plain, fm.horizontalAdvance(label) - fm.horizontalAdvance("Continue"), 3)

    def test_painted_chip_matches_its_measured_width(self) -> None:
        label = "Continue Smith 2024 from page 14"
        img = QImage(600, 40, QImage.Format.Format_ARGB32)
        img.fill(0)
        hits: dict = {}
        p = QPainter(img)
        end = paint_parts(p, self.t, 0, 0, 40, [("action", "continue")], self.t.color("TextPrimary"), hits=hits,
                          labels={"continue": label})
        p.end()
        width = measure_parts(self.t, [("action", "continue")], labels={"continue": label})
        self.assertAlmostEqual(end, width, 3)
        self.assertAlmostEqual(hits["continue"].width(), width, 3)


class ToggleRowTests(unittest.TestCase):
    def test_row_heights(self) -> None:
        for k in (1.0, 1.5):
            t = theme(k)
            self.assertEqual(ToggleRow(t, FakeTips(), "Skip citations").row_height(), t.px(TOGGLE_H))
            row = ToggleRow(t, FakeTips(), "Match capitals", detail="Only “fMRI”, not “fmri” or “FMRI”.")
            self.assertEqual(row.row_height(), t.px(DESCRIBED_H))
        self.assertEqual((TOGGLE_H, DESCRIBED_H), (40, 52))

    def test_a_described_row_keeps_its_height_and_names_its_detail(self) -> None:
        t = theme()
        row = ToggleRow(t, FakeTips(), "Pick up where you left off", focusable=True,
                        detail="Remembers your place in up to 20 files, never their text.")
        row.place(QRect(12, 0, 416, row.row_height()))
        self.assertEqual(row.height(), t.px(DESCRIBED_H) + 2 * t.px(FOCUS_PAD))
        self.assertEqual(row.accessibleDescription(), "Remembers your place in up to 20 files, never their text.")
        row.set_detail("Saylight forgot your place in 3 files.")
        self.assertEqual(row.accessibleDescription(), "Saylight forgot your place in 3 files.")
        row.set_detail("")
        self.assertEqual(row.row_height(), t.px(DESCRIBED_H))
        render(row)

    def test_focusable_row_toggles_from_the_keyboard(self) -> None:
        row = ToggleRow(theme(), FakeTips(), "Whole word only", focusable=True)
        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.TabFocus)
        seen: list[bool] = []
        row.toggled.connect(seen.append)
        self.assertTrue(key(row, Qt.Key.Key_Space))
        self.assertEqual((row.checked, seen, row.accessibleName()), (True, [True], "Whole word only: on"))

    def test_settings_rows_stay_unfocusable(self) -> None:
        row = ToggleRow(theme(), FakeTips(), "Skip citations")
        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertFalse(row.described)
        row.resize(416, 40)
        render(row)


class SegmentedTests(unittest.TestCase):
    ITEMS = [("Whole document", 14, "Whole document"), ("From page 14", 14, "From page 14")]

    def test_arrow_keys_move_a_focusable_control(self) -> None:
        seg = Segmented(theme(), FakeTips(), self.ITEMS, name="Save", focusable=True)
        seg.place(QRect(0, 0, 300, 34))
        self.assertEqual(seg.focusPolicy(), Qt.FocusPolicy.TabFocus)
        seen: list[int] = []
        seg.selected.connect(seen.append)
        self.assertTrue(key(seg, Qt.Key.Key_Right))
        self.assertTrue(key(seg, Qt.Key.Key_Right))  # already last: nothing more
        self.assertTrue(key(seg, Qt.Key.Key_Home))
        self.assertTrue(key(seg, Qt.Key.Key_End))
        self.assertEqual(seen, [1, 0, 1])
        self.assertEqual(seg.accessibleName(), "Save: From page 14")
        render(seg)

    def test_clicks_land_on_the_face(self) -> None:
        t = theme()
        seg = Segmented(t, FakeTips(), self.ITEMS, name="Save", focusable=True)
        seg.place(QRect(0, 0, 300, 34))
        pad = t.px(FOCUS_PAD)
        self.assertEqual(seg._index_at(pad + 149), 0)
        self.assertEqual(seg._index_at(pad + 151), 1)

    def test_a_settings_control_ignores_keys(self) -> None:
        seg = Segmented(theme(), FakeTips(), self.ITEMS, name="Save")
        seg.resize(300, 34)
        self.assertEqual(seg.focusPolicy(), Qt.FocusPolicy.NoFocus)
        key(seg, Qt.Key.Key_Right)
        self.assertEqual(seg.index, 0)
        self.assertEqual(seg._index_at(151), 1)


class FocusableNavRowTests(unittest.TestCase):
    def test_focusable_row_and_detail_description(self) -> None:
        t = theme()
        row = NavRow(t, FakeTips(), "Pronunciations", focusable=True, detail="12 words")
        row.place(QRect(12, 0, 416, 40))
        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.TabFocus)
        self.assertEqual(row.height(), t.px(40) + 2 * t.px(FOCUS_PAD))
        self.assertEqual(row.accessibleDescription(), "12 words")
        clicks: list[int] = []
        row.clicked.connect(lambda: clicks.append(1))
        key(row, Qt.Key.Key_Return)
        self.assertEqual(clicks, [1])
        row.set_detail("12 words · off")
        self.assertEqual(row.accessibleDescription(), "12 words · off")
        render(row)

if __name__ == "__main__":
    unittest.main()
