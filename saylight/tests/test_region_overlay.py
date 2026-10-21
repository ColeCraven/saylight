"""Tests for ui/region_overlay.py: the selection box, the 5 px rule, the pill's place and its one-way move, the
idle countdown and timeout, the cancel reasons, and what the overlay paints.

The overlay windows are real but never reach the screen (WA_DontShowOnScreen), and the pointer is faked.
Reduced motion is on, so the pill's text swaps at once.
"""

import sys
import unittest
from unittest import mock

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from saylight.tests.test_theme import FakeSystem
from saylight.theme import Theme
from saylight.ui import region_overlay as R

_app = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class Settings(QObject):
    changed = Signal(str)

    def __init__(self, **values):
        super().__init__()
        self.values = {"text_size": "medium", "reading_tint": "auto", "reading_font": "verdana", "spacing": "wide",
                       "ruler.dim": "medium", "ruler.height": "medium", "ruler.enabled": False,
                       "highlight": "word", "screen.highlight": True, **values}

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        if self.values.get(key) != value:
            self.values[key] = value
            self.changed.emit(key)


def make_theme(k: float = 1.0, **overrides) -> Theme:
    overrides.setdefault("animations", False)
    overrides.setdefault("families", None)
    return Theme(Settings(), system=FakeSystem(scale=k, **overrides))


class OffscreenOverlay(R.RegionOverlay):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)


class FakeCursor:
    at = QPoint(0, 0)

    @classmethod
    def pos(cls):
        return QPoint(cls.at)


def mouse(kind, pos, button=Qt.MouseButton.LeftButton) -> QMouseEvent:
    p = QPointF(pos)
    buttons = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, p, p, button, buttons, Qt.KeyboardModifier.NoModifier)


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.t = make_theme()

    def test_selection_is_normalised_and_clamped_to_the_press_screen(self):
        bounds = QRect(0, 0, 800, 600)
        self.assertEqual(R.selection_rect(QPoint(100, 100), QPoint(40, 30), bounds), QRect(40, 30, 60, 70))
        self.assertEqual(R.selection_rect(QPoint(700, 500), QPoint(2000, -50), bounds), QRect(700, 0, 100, 500))
        self.assertEqual(R.selection_rect(QPoint(10, 10), QPoint(10, 10), bounds), QRect(10, 10, 0, 0))

    def test_five_physical_pixel_rule(self):
        self.assertTrue(R.too_small(QRect(0, 0, 4, 100), 1.0))
        self.assertFalse(R.too_small(QRect(0, 0, 5, 5), 1.0))
        self.assertFalse(R.too_small(QRect(0, 0, 4, 4), 1.25))   # 5 x 5 physical
        self.assertTrue(R.too_small(QRect(0, 0, 3, 40), 1.5))    # 4.5 physical
        self.assertTrue(R.too_small(QRect(0, 0, 0, 0), 2.0))

    def test_pill_sits_at_the_top_centred_on_the_pointer_and_inside_the_work_area(self):
        work = QRect(0, 0, 1600, 900)
        r = R.pill_rect(self.t, work, 800, 300, False)
        self.assertEqual((r.left(), r.top(), r.width(), r.height()), (650, 24, 300, 40))
        self.assertEqual(R.pill_rect(self.t, work, 10, 300, False).left(), 24)
        self.assertEqual(R.pill_rect(self.t, work, 1590, 300, False).right(), 1600 - 24)
        low = R.pill_rect(self.t, work, 800, 300, True)
        self.assertEqual(low.top(), 900 - 24 - 40)
        k15 = make_theme(1.5)
        self.assertEqual(R.pill_rect(k15, work, 800, 300, False).top(), 36)

    def test_pill_moves_for_the_pointer_or_the_selection(self):
        pill = QRectF(650, 24, 300, 40)
        self.assertTrue(R.pill_should_move(self.t, pill, QPointF(800, 64 + 47), None))
        self.assertFalse(R.pill_should_move(self.t, pill, QPointF(800, 64 + 60), None))
        self.assertTrue(R.pill_should_move(self.t, pill, QPointF(950 + 30, 64 + 30), None))   # 42 diagonally
        self.assertFalse(R.pill_should_move(self.t, pill, QPointF(950 + 40, 64 + 40), None))  # 57
        self.assertTrue(R.pill_should_move(self.t, pill, None, QRect(100, 70, 600, 200)))     # within 16
        self.assertFalse(R.pill_should_move(self.t, pill, None, QRect(100, 90, 600, 200)))

    def test_pill_widths(self):
        """300.6 DIP at k 1 in Segoe UI Variable (451 at k 1.5); the hint is wider; no keycap without Esc."""
        default = R.pill_content("esc")
        width = R.pill_width(self.t, default)
        self.assertTrue(290 <= width <= 312, width)
        self.assertAlmostEqual(R.pill_width(make_theme(1.5), default) / width, 1.5, delta=0.03)
        self.assertGreater(R.pill_width(self.t, R.pill_content("esc", hint=True)), width + 40)
        self.assertEqual(R.pill_content("right_click").detail, (("text", "Right-click cancels"),))
        self.assertEqual(R.pill_content("esc", seconds_left=3).detail, (("text", "Closes in 3 s"),))
        self.assertEqual(R.pill_content("esc").detail, (("key", "Esc"), ("text", "cancels")))
        self.assertEqual(R.pill_content("esc", hint=True).glyph_token, "Caution")


class PaintTests(unittest.TestCase):
    def paint(self, t, selection):
        img = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        R.paint_overlay(p, t, QSize(400, 300), selection)
        p.end()
        return img

    def test_scrim_cut_out_and_ring_outside_the_box(self):
        t = make_theme()
        img = self.paint(t, QRect(100, 100, 120, 60))
        self.assertEqual(img.pixelColor(10, 10).alpha(), 0x66)            # the 40% scrim
        self.assertEqual(img.pixelColor(160, 130).alpha(), 1)             # inside: clear, but it keeps the pointer
        self.assertEqual(img.pixelColor(98, 130), QColor("#FFFFFF"))      # the white edge, 1-3 px outside
        halo = img.pixelColor(100 - 4, 130)
        self.assertEqual((halo.red(), halo.green(), halo.blue()), (0, 0, 0))
        self.assertGreater(halo.alpha(), 0x66)                            # the halo on top of the scrim
        self.assertEqual(img.pixelColor(100 - 6, 130).alpha(), 0x66)

    def test_high_contrast_frame_and_no_scrim(self):
        t = make_theme(hc=True)
        img = self.paint(t, QRect(100, 100, 120, 60))
        highlight = t.color("Accent")
        self.assertEqual(img.pixelColor(1, 150), highlight)                # the 3 DIP frame along the edge
        self.assertEqual(img.pixelColor(399, 150), highlight)
        self.assertEqual(img.pixelColor(10, 10).alpha(), 1)               # no scrim, but the pointer is kept
        self.assertEqual(img.pixelColor(98, 130), highlight)              # the ring's middle is Highlight
        self.assertEqual(img.pixelColor(96, 130), t.color("Surface"))     # with Window either side


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.t = make_theme()
        self.now = [100.0]
        self.mgr = R.OverlayManager(self.t, clock=lambda: self.now[0])
        self.events = []
        self.mgr.selected.connect(lambda screen, rect: self.events.append(("selected", screen, QRect(rect))))
        self.mgr.cancelled.connect(lambda reason: self.events.append(("cancelled", reason)))
        self.screen = QGuiApplication.primaryScreen()
        g = self.screen.geometry()
        FakeCursor.at = g.topLeft() + QPoint(g.width() // 2, g.height() // 2)
        for target, value in ((R, "RegionOverlay"), (R, "QCursor")):
            patcher = mock.patch.object(target, value, OffscreenOverlay if value == "RegionOverlay" else FakeCursor)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.mgr.close)
        self.mgr.open()

    def window(self) -> R.RegionOverlay:
        return next(w for w in self.mgr.windows() if w.screen_ is self.screen)

    def drag(self, w, a: QPoint, b: QPoint) -> None:
        w.mousePressEvent(mouse(QMouseEvent.Type.MouseButtonPress, a))
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, (a + b) / 2))
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, b))
        w.mouseReleaseEvent(mouse(QMouseEvent.Type.MouseButtonRelease, b))

    def test_one_window_per_screen_and_the_pill_on_the_pointers_screen(self):
        windows = self.mgr.windows()
        self.assertEqual(len(windows), len(QGuiApplication.screens()))
        for w in windows:
            self.assertEqual(w.geometry(), w.screen_.geometry())
            self.assertTrue(w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
            self.assertTrue(w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
            self.assertEqual(w.accessibleName(), R.ACCESSIBLE_NAME)
        self.assertEqual([w for w in windows if w.pill_x is not None], [self.window()])
        self.assertEqual(self.window().pill_x, self.screen.geometry().width() // 2)
        self.mgr.open()  # already open: nothing new
        self.assertEqual(self.mgr.windows(), windows)

    def test_drag_selects_in_global_coordinates(self):
        w = self.window()
        self.drag(w, QPoint(300, 400), QPoint(120, 250))
        g = self.screen.geometry()
        self.assertEqual(self.events, [("selected", self.screen, QRect(g.x() + 120, g.y() + 250, 180, 150))])
        self.assertTrue(self.mgr.is_open())  # the caller grabs, then closes

    def test_drag_past_the_screen_edge_stays_on_the_press_screen(self):
        w = self.window()
        self.drag(w, QPoint(50, 50), QPoint(-400, 30000))
        g = self.screen.geometry()
        self.assertEqual(self.events[0][2], QRect(g.x(), g.y() + 50, 50, g.height() - 50))

    def test_a_click_shows_the_hint_and_keeps_the_overlay_open(self):
        w = self.window()
        self.drag(w, QPoint(300, 400), QPoint(302, 401))
        self.assertEqual(self.events, [])
        self.assertTrue(self.mgr.is_open())
        self.assertEqual(w.content.label, R.HINT_TEXT)
        self.now[0] += R.HINT_S + 0.1
        self.mgr._on_tick()
        self.assertEqual(w.content.label, R.PILL_TEXT)

    def test_right_click_cancels_even_mid_drag(self):
        w = self.window()
        w.mousePressEvent(mouse(QMouseEvent.Type.MouseButtonPress, QPoint(300, 400)))
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, QPoint(500, 500)))
        w.mousePressEvent(mouse(QMouseEvent.Type.MouseButtonPress, QPoint(500, 500), Qt.MouseButton.RightButton))
        self.assertEqual(self.events, [("cancelled", "right_click")])
        self.assertFalse(self.mgr.is_open())

    def test_escape_key_and_other_reasons(self):
        from PySide6.QtGui import QKeyEvent
        esc = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        self.window().keyPressEvent(esc)
        self.assertEqual(self.events, [("cancelled", "esc")])
        self.mgr.open()
        self.mgr.cancel("hotkey")
        self.mgr.cancel("hotkey")  # closed already: nothing
        self.mgr.open()
        self.mgr._screens_changed()
        self.assertEqual(self.events[1:], [("cancelled", "hotkey"), ("cancelled", "screens_changed")])
        self.mgr.open()
        self.mgr.close()  # the caller's own close says nothing
        self.assertEqual(len(self.events), 3)

    def test_idle_countdown_and_timeout(self):
        w = self.window()
        self.now[0] += 24.9
        self.mgr._on_tick()
        self.assertEqual(w.content.detail, (("key", "Esc"), ("text", "cancels")))
        self.now[0] += 0.2
        self.mgr._on_tick()
        self.assertEqual(w.content.detail, (("text", "Closes in 5 s"),))
        self.now[0] += 2.0
        self.mgr._on_tick()
        self.assertEqual(w.content.detail, (("text", "Closes in 3 s"),))
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, QPoint(700, 700)))  # any movement resets it
        self.assertEqual(w.content.detail, (("key", "Esc"), ("text", "cancels")))
        self.now[0] += 29.9
        self.mgr._on_tick()
        self.assertEqual(self.events, [])
        self.now[0] += 0.2
        self.mgr._on_tick()
        self.assertEqual(self.events, [("cancelled", "timeout")])

    def test_pill_moves_once_and_never_back(self):
        w = self.window()
        top = w.pill_geometry()
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, top.center()))
        low = w.pill_geometry()
        self.assertGreater(low.top(), top.top())
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, QPointF(top.center().x(), top.bottom() + 300)))
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, low.center()))
        self.assertEqual(w.pill_geometry().top(), low.top())

    def test_selection_meeting_the_pill_moves_it(self):
        w = self.window()
        pill = w.pill_geometry()
        w.mousePressEvent(mouse(QMouseEvent.Type.MouseButtonPress, QPoint(int(pill.left()) - 200, 300)))
        self.assertFalse(w.pill_low)
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, QPoint(int(pill.right()) + 200, int(pill.bottom()) + 120)))
        self.assertFalse(w.pill_low)  # the pointer is 120 below; the box doesn't reach it yet
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, QPoint(int(pill.left()) - 200, 300)))
        w.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, QPoint(int(pill.right()) + 300, int(pill.bottom()) + 10)))
        self.assertTrue(w.pill_low)

    def test_the_pill_follows_the_pointer_to_another_screen(self):
        others = [w for w in self.mgr.windows() if w is not self.window()]
        if not others:
            self.skipTest("one screen")
        other = others[0]
        other.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, QPoint(100, 200)))
        self.assertIsNone(self.window().pill_x)
        self.assertEqual(other.pill_x, 100)

    def test_pill_detail_switches_to_right_click(self):
        self.mgr.set_pill_detail("right_click")
        self.assertEqual(self.window().content.detail, (("text", "Right-click cancels"),))

    def test_activation_fallback(self):
        with mock.patch.object(R.win32_overlay, "activate", return_value=False):
            self.assertFalse(self.mgr.activate())
        self.assertIsNone(self.mgr.activated)
        with mock.patch.object(R.win32_overlay, "activate", return_value=True), \
                mock.patch.object(OffscreenOverlay, "grabKeyboard") as grab:
            self.assertTrue(self.mgr.activate())
        grab.assert_called_once()
        self.assertIs(self.mgr.activated, self.window())
        hwnd = self.mgr.activated_hwnd
        self.assertEqual(hwnd, int(self.window().winId()))
        self.mgr.close()
        self.assertEqual(self.mgr.activated_hwnd, hwnd)  # kept for handing the foreground back

    def test_wheel_is_swallowed(self):
        event = mock.Mock()
        self.window().wheelEvent(event)
        event.accept.assert_called_once()


if __name__ == "__main__":
    unittest.main()
