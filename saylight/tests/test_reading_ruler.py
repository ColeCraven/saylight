"""Tests for ui/reading_ruler.py: the band's place and clamping, what it paints, the pointer's screen only, holding
over Saylight's windows, hiding for the lock screen and full-screen apps, suppression, and the settings.

The ruler windows are real but never reach the screen (WA_DontShowOnScreen); the pointer and the system state
are faked. Reduced motion is on, so fades finish at once.
"""

import sys
import unittest
from unittest import mock

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QApplication

from saylight import win32, win32_overlay
from saylight.tests.test_region_overlay import FakeCursor, Settings, make_theme
from saylight.theme import RULER_DIM
from saylight.ui import reading_ruler as RR

_app = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class OffscreenRuler(RR.RulerWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)


class GeometryTests(unittest.TestCase):
    def test_band_is_centred_on_the_pointer_and_clamped_to_the_work_area(self):
        work = QRect(100, 50, 1000, 600)
        above, band, below = RR.ruler_rects(work, 300, 72)
        self.assertEqual((band.top(), band.height()), (264, 72))
        self.assertEqual((above.top(), above.height(), below.top(), below.bottom()), (50, 214, 336, 649))
        self.assertEqual(above.width(), 1000)
        _a, top, _b = RR.ruler_rects(work, 0, 72)
        self.assertEqual(top.top(), 50)                     # never above the work area
        _a, bottom, below = RR.ruler_rects(work, 5000, 120)
        self.assertEqual(bottom.bottom(), 649)              # never over the taskbar
        self.assertEqual(below.height(), 0)
        _a, whole, _b = RR.ruler_rects(work, 300, 9999)
        self.assertEqual(whole, work)

    def test_the_window_stops_short_of_a_whole_screen(self):
        # Windows takes a topmost window covering its monitor for a full-screen app, and the ruler hides for those
        full = QRect(-1920, 0, 1920, 1080)
        self.assertEqual(win32_overlay.short_of_full_screen(full, full), QRect(-1920, 0, 1920, 1079))
        work = QRect(-1920, 0, 1920, 1032)   # a taskbar below: already short of it
        self.assertEqual(win32_overlay.short_of_full_screen(work, full), work)
        bigger = full.adjusted(-8, -8, 8, 8)  # the on-screen marks' window, over a whole-screen area
        self.assertEqual(win32_overlay.short_of_full_screen(bigger, full).bottom(), full.bottom() - 1)

    def test_band_heights_follow_the_setting_and_k(self):
        t = make_theme()
        self.assertEqual([RR.band_height(t, s) for s in ("small", "medium", "large", "odd")], [36, 72, 120, 72])
        self.assertEqual(RR.band_height(make_theme(1.5), "medium"), 108)

    def paint(self, t, *, lines, dpr=1.0):
        img = QImage(round(200 * dpr), round(100 * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        RR.paint_ruler(p, t, QSize(200, 100), 40, 60, t.ruler_colors("medium"), lines=lines, dpr=dpr)
        p.end()
        return img

    def test_dimming_with_solid_edge_lines(self):
        t = make_theme()
        colors = t.ruler_colors("medium")
        img = self.paint(t, lines=False)
        self.assertAlmostEqual(img.pixelColor(10, 10).alphaF(), RULER_DIM["medium"], delta=0.01)
        self.assertEqual(img.pixelColor(10, 39), colors["edge"])   # 1 px, solid, on the band side of the dim
        self.assertEqual(img.pixelColor(10, 60), colors["edge"])
        self.assertEqual(img.pixelColor(10, 50).alpha(), 0)          # the band is clear
        self.assertAlmostEqual(img.pixelColor(10, 61).alphaF(), RULER_DIM["medium"], delta=0.01)

    def test_edge_lines_are_one_device_pixel_at_125(self):
        t = make_theme()
        img = self.paint(t, lines=False, dpr=1.25)
        edge = t.ruler_colors()["edge"]
        rows = [y for y in range(img.height()) if img.pixelColor(5, y) == edge]
        self.assertEqual(rows, [49, 75])   # 40 x 1.25 - 1 and 60 x 1.25

    def test_high_contrast_draws_two_lines_and_no_dim(self):
        t = make_theme(hc=True)
        img = self.paint(t, lines=True)
        self.assertEqual(img.pixelColor(10, 10).alpha(), 0)
        self.assertEqual(img.pixelColor(10, 80).alpha(), 0)
        line = t.color("Accent")
        self.assertEqual([y for y in range(100) if img.pixelColor(10, y) == line], [38, 39, 60, 61])


class RulerCase(unittest.TestCase):
    def setUp(self):
        for target, name, value in ((RR, "RulerWindow", OffscreenRuler), (RR, "QCursor", FakeCursor),
                                    (win32, "query_user_notification_state", lambda: self.quns)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.quns = 5
        self.own: list[QRect] = []
        self.settings = Settings()
        self.theme = make_theme()
        self.screen = QGuiApplication.primaryScreen()
        self.area = self.screen.availableGeometry()
        FakeCursor.at = self.area.topLeft() + QPoint(200, 300)
        self.ruler = RR.ReadingRuler(self.theme, self.settings, lambda: list(self.own))
        self.addCleanup(self.ruler.shutdown)
        self.toggles = []
        self.ruler.toggled.connect(self.toggles.append)

    def window(self):
        return self.ruler._window

    def band_top(self) -> int:
        return self.window().top + self.window().geometry().top()

    def tick(self, at: QPoint | None = None):
        if at is not None:
            FakeCursor.at = at
        self.ruler._on_follow()


class RulerTests(RulerCase):
    def test_off_by_default(self):
        self.assertFalse(self.ruler.enabled)
        self.assertIsNone(self.window())
        self.assertFalse(self.ruler._follow.isActive())
        self.assertFalse(self.ruler._check.isActive())

    def test_turning_on_shows_on_the_pointers_screen(self):
        shown = []
        self.ruler.shown.connect(lambda: shown.append(1))
        with self.assertLogs("saylight.reading_ruler", "INFO") as logs:
            self.ruler.set_enabled(True)
        self.assertEqual(self.settings.get("ruler.enabled"), True)
        self.assertEqual(self.toggles, [True])
        w = self.window()
        self.assertTrue(w.isVisible())
        self.assertIs(w.screen_, self.screen)
        self.assertEqual(w.geometry(), RR.ruler_area(self.screen))
        self.assertTrue(w.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertEqual(self.band_top(), self.area.top() + 300 - 36)
        self.assertEqual(w.windowOpacity(), 1.0)
        self.assertEqual(shown, [1])
        self.assertTrue(self.ruler._follow.isActive())
        self.assertEqual(self.ruler._follow.interval(), 33)
        self.assertIn("Ruler: on (medium, medium)", "\n".join(logs.output))

    def test_follows_the_pointer_and_holds_over_saylight(self):
        self.ruler.set_enabled(True)
        self.tick(self.area.topLeft() + QPoint(200, 500))
        self.assertEqual(self.band_top(), self.area.top() + 500 - 36)
        self.own = [QRect(self.area.left() + 100, self.area.top() + 600, 476, 252)]
        self.tick(self.area.topLeft() + QPoint(300, 700))   # over the panel: it stays put
        self.assertEqual(self.band_top(), self.area.top() + 500 - 36)
        self.tick(self.area.topLeft() + QPoint(900, 700))
        self.assertEqual(self.band_top(), self.area.top() + 700 - 36)

    def test_a_gap_between_screens_keeps_the_last_place(self):
        self.ruler.set_enabled(True)
        before = self.band_top()
        with mock.patch.object(RR.QGuiApplication, "screenAt", return_value=None):
            self.tick(self.area.topLeft() + QPoint(200, 900))
        self.assertEqual(self.band_top(), before)

    def test_only_the_pointers_screen_is_dimmed(self):
        others = [s for s in QGuiApplication.screens() if s is not self.screen]
        if not others:
            self.skipTest("one screen")
        self.ruler.set_enabled(True)
        first = self.window()
        other = others[0]
        self.tick(other.availableGeometry().center())
        self.assertIsNot(self.window(), first)
        self.assertIs(self.window().screen_, other)
        self.assertEqual(self.window().geometry(), RR.ruler_area(other))
        self.assertFalse(first.isVisible())   # gone at once from the screen it left
        self.assertEqual(self.window().windowOpacity(), 1.0)

    def test_suppression_hides_at_once_and_returns(self):
        self.ruler.set_enabled(True)
        self.ruler.suppress("overlay", True)
        self.assertIsNone(self.window())
        self.assertFalse(self.ruler._follow.isActive())
        self.ruler.suppress("fullscreen", True)
        self.ruler.suppress("overlay", False)
        self.assertIsNone(self.window())       # still held by the other reason
        self.ruler.suppress("fullscreen", False)
        self.assertTrue(self.window().isVisible())
        self.assertTrue(self.ruler._follow.isActive())

    def test_lock_screen_and_full_screen_apps_hide_it(self):
        self.ruler.set_enabled(True)
        for state in (1, 2, 3, 4):
            with self.subTest(state=state):
                self.quns = state
                with self.assertLogs("saylight.reading_ruler", "INFO") as logs:
                    self.ruler._on_check()
                self.assertIsNone(self.window())
                self.assertIn("Ruler: hidden (full screen)", "\n".join(logs.output))
                self.quns = 5
                self.ruler._on_check()
                self.assertTrue(self.window().isVisible())
        self.assertTrue(self.ruler._check.isActive())
        self.assertEqual(self.ruler._check.interval(), 1000)

    def test_the_ruler_is_never_taken_for_a_full_screen_app(self):
        # Windows says busy while a topmost window covers a whole monitor: the ruler mustn't be one, or it would
        # hide itself (and the panel) every other second
        def busy_while_covered():
            w = self.window()
            covers = w is not None and w.isVisible() and w.geometry().contains(w.screen_.geometry())
            return 2 if covers else 5
        with mock.patch.object(win32, "query_user_notification_state", busy_while_covered):
            self.ruler.set_enabled(True)
            for _ in range(3):
                self.ruler._on_check()
                self.assertTrue(self.window().isVisible())

    def test_the_selection_overlay_is_not_taken_for_a_full_screen_app(self):
        self.ruler.set_enabled(True)
        self.ruler.suppress("overlay", True)
        self.quns = 2   # busy while the overlay covers the screen, and for a moment after it closes
        self.ruler._on_check()
        self.ruler.suppress("overlay", False)
        self.assertTrue(self.window().isVisible())   # back at once
        self.ruler._on_check()
        self.assertTrue(self.window().isVisible())
        with self.assertLogs("saylight.reading_ruler", "INFO"):
            self.ruler._on_check()   # still busy after that: a real full-screen app
        self.assertIsNone(self.window())

    def test_turning_off_fades_out_and_drops_the_window(self):
        self.ruler.set_enabled(True)
        self.ruler.set_enabled(False)
        self.assertIsNone(self.window())
        self.assertEqual(self.toggles, [True, False])
        self.assertFalse(self.ruler._check.isActive())

    def test_settings_page_and_height_and_dim(self):
        self.settings.set("ruler.enabled", True)   # the Reading display row writes the setting
        self.assertTrue(self.window().isVisible())
        self.assertEqual(self.toggles, [True])
        self.settings.set("ruler.height", "large")
        self.assertEqual(self.window().bottom - self.window().top, 120)
        self.settings.set("ruler.dim", "strong")
        self.assertAlmostEqual(self.ruler.colors()["dim"].alphaF(), RULER_DIM["strong"], delta=0.01)
        self.settings.set("ruler.enabled", False)
        self.assertIsNone(self.window())

    def test_lines_in_high_contrast_and_band_only(self):
        self.assertFalse(self.ruler.lines_only())
        self.ruler.band_only = True
        self.assertTrue(self.ruler.lines_only())
        hc = RR.ReadingRuler(make_theme(hc=True), Settings(), lambda: [])
        self.addCleanup(hc.shutdown)
        self.assertTrue(hc.lines_only())

    def test_screen_changes_rebuild_the_windows(self):
        self.ruler.set_enabled(True)
        self.ruler._screens_changed()
        self.assertIsNone(self.window())
        self.ruler._rebuild_timer.timeout.emit()
        self.assertTrue(self.window().isVisible())

    def test_moving_repaints_only_the_strips_that_change(self):
        self.ruler.set_enabled(True)
        w = self.window()
        with mock.patch.object(w, "update") as update:
            self.tick(self.area.topLeft() + QPoint(200, 310))
            self.tick(self.area.topLeft() + QPoint(200, 310))   # unchanged: no work
        rects = [c.args[0] for c in update.call_args_list]
        self.assertEqual(len(rects), 2)
        self.assertTrue(all(r.height() <= 10 + 2 * (2 + 2) and r.width() == w.width() for r in rects))


if __name__ == "__main__":
    unittest.main()
