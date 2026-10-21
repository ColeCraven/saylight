"""Tests for ui/flyout.py: where the flyout goes, its size, its rows, the window's fixed flags, and More's ring and
badge. No window is shown.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QMenu

from saylight.tests.test_controls import FakeTips, render, setUpModule, theme  # noqa: F401 (setUpModule runs here)
from saylight.ui import flyout as F

AREA = QRect(0, 0, 1920, 1032)


class FlyoutRectTests(unittest.TestCase):
    def test_from_more_it_hangs_below_the_plate_by_its_right_edge(self) -> None:
        plate = QRect(1100, 508, 32, 32)  # the panel's More at x 360, y 8, for a panel at (740, 500)
        got = F.flyout_rect(plate, QSize(220, 161), AREA, "more")
        self.assertEqual(got.right(), plate.right())
        self.assertEqual(got.top(), plate.bottom() + 1 + 4)  # panel top + 44

    def test_from_more_it_opens_upward_without_room(self) -> None:
        plate = QRect(1100, 900, 32, 32)
        got = F.flyout_rect(plate, QSize(220, 161), AREA, "more")
        self.assertEqual(got.bottom() + 1, plate.top() - 4)  # panel top + 4

    def test_at_the_pointer_it_flips_to_fit(self) -> None:
        size = QSize(252, 170)
        self.assertEqual(F.flyout_rect(QRect(500, 400, 1, 1), size, AREA, "pointer").topLeft(), QPoint(502, 402))
        got = F.flyout_rect(QRect(1900, 1000, 1, 1), size, AREA, "pointer")
        self.assertEqual((got.right() + 1, got.bottom() + 1), (1898, 998))

    def test_it_stays_inside_the_work_area(self) -> None:
        for anchor, mode in ((QRect(-50, -50, 1, 1), "pointer"), (QRect(5, 10, 32, 32), "more"),
                             (QRect(1915, 5, 1, 1), "pointer")):
            got = F.flyout_rect(anchor, QSize(300, 200), AREA, mode)
            self.assertTrue(AREA.adjusted(8, 8, -8, -8).contains(got), (anchor, mode, got))

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            F.flyout_rect(QRect(), QSize(1, 1), AREA, "menu")


class MenuItemTests(unittest.TestCase):
    def keys(self, items) -> list[str]:
        return [i.key for i in items]

    def test_the_rows_in_order(self) -> None:
        self.assertEqual(self.keys(F.menu_items()), ["open", "save", "-", "pron", "settings"])
        items = F.menu_items("fMRI", can_save=True, saved=True)
        self.assertEqual(self.keys(items), ["word", "-", "open", "save", "saved", "-", "pron", "settings"])
        self.assertEqual([i.label for i in items if i.key != "-"],
                         ["Change how “fMRI” is said…", "Open a file…", "Save as audio…",
                          "Show saved audio in folder", "Pronunciations…", "Settings"])

    def test_save_is_disabled_with_nothing_to_save(self) -> None:
        save = F.menu_items()[1]
        self.assertFalse(save.enabled)
        self.assertEqual(save.tooltip, "Open a file or read some text first.")
        self.assertFalse(F.menu_items(can_open=False)[0].enabled)

    def test_while_saving_and_after_a_failure(self) -> None:
        saving = F.menu_items(saving=0.42)[1]
        self.assertEqual((saving.key, saving.label), ("saving", "Saving audio · 42%"))
        self.assertEqual(saving.trailing[2], "Cancel saving audio")
        failed = F.menu_items(failed="No internet connection")
        self.assertEqual(self.keys(failed), ["open", "save_failed", "save", "-", "pron", "settings"])
        self.assertEqual((failed[1].detail, failed[1].glyph_token), ("No internet connection", "Critical"))
        self.assertEqual(F.item_height(failed[1]), 52)

    def test_a_long_word_is_cut_short(self) -> None:
        self.assertEqual(F.word_label("Schneps-Jonassonovich"), "Change how “Schneps-Jonasson…” is said…")
        self.assertEqual(F.word_label("RT"), "Change how “RT” is said…")


class FlyoutSizeTests(unittest.TestCase):
    def test_width_is_clamped_and_height_adds_up(self) -> None:
        t = theme()
        size = F.flyout_size(t, F.menu_items())
        self.assertEqual(size.width(), 220)
        self.assertEqual(size.height(), 4 + 36 * 4 + 9 + 4)
        wide = F.flyout_size(t, F.menu_items("Schneps-Jonassonovich"))
        self.assertGreater(wide.width(), 220)
        self.assertLessEqual(wide.width(), 320)
        self.assertLessEqual(F.flyout_size(t, [F.MenuItem("open", label="x" * 200)]).width(), 320)

    def test_it_scales_with_text_size(self) -> None:
        t = theme(k=1.5)
        self.assertEqual(F.flyout_size(t, F.menu_items()).width(), 330)


class FlyoutWindowTests(unittest.TestCase):
    def test_a_window_that_never_takes_focus(self) -> None:
        f = F.Flyout(theme(), FakeTips())
        self.addCleanup(f.deleteLater)
        self.assertNotIsInstance(f, QMenu)
        flags = f.windowFlags()
        for flag in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowDoesNotAcceptFocus):
            self.assertTrue(flags & flag, flag)
        self.assertTrue(f.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(f.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertFalse(f.is_open)

    def test_rows_and_their_signals(self) -> None:
        f = F.Flyout(theme(), FakeTips())
        self.addCleanup(f.deleteLater)
        chosen, buttons = [], []
        f.chosen.connect(chosen.append)
        f.trailingClicked.connect(buttons.append)
        f._build(F.menu_items(saving=0.5))
        self.assertEqual([r.item.key for r in f.rows], ["open", "saving", "pron", "settings"])
        self.assertEqual(f.size(), F.flyout_size(f.theme, f.items))
        f.rows[0].clicked.emit()
        f.rows[1].button.clicked.emit()
        self.assertEqual((chosen, buttons), (["open"], ["saving"]))
        self.assertEqual(f.rows[1].accessibleName(), "Saving audio, 50 percent")
        render(f)



class PointerAwayTests(unittest.TestCase):
    """The open flyout's poll, with the pointer, the mouse buttons and the clock stood in for."""

    def setUp(self) -> None:
        self.over = False
        self.f = F.Flyout(theme(), FakeTips(), inside=lambda _pos: self.over)
        self.addCleanup(self.f.deleteLater)
        self.closes = []
        self.f.close_animated = lambda: self.closes.append(self.now)
        self.now, self.down, self.tapped = 100.0, False, False
        for p in (mock.patch.object(F.Flyout, "is_open", new_callable=mock.PropertyMock, return_value=True),
                  mock.patch.object(F, "QCursor", SimpleNamespace(pos=lambda: QPoint(-5000, -5000))),
                  mock.patch.object(F.time, "monotonic", side_effect=lambda: self.now),
                  mock.patch.object(F.win32, "key_state", side_effect=self.key_state)):
            p.start()
            self.addCleanup(p.stop)

    def key_state(self, vk: int) -> tuple[bool, bool]:
        if vk != F.VK_LBUTTON:
            return False, False
        tapped, self.tapped = self.tapped, False
        return self.down, tapped

    def poll_at(self, now: float) -> None:
        self.now = now
        self.f._check_pointer()

    def test_a_tap_between_two_polls_closes_it(self) -> None:
        self.poll_at(100.0)
        self.tapped = True  # pressed and released since the last poll
        self.poll_at(100.05)
        self.assertEqual(self.closes, [100.05])

    def test_a_tap_over_the_flyout_or_the_panel_does_not(self) -> None:
        self.over, self.tapped = True, True
        self.poll_at(100.0)
        self.assertEqual(self.closes, [])

    def test_a_held_button_closes_it_once(self) -> None:
        self.down = self.tapped = True
        self.poll_at(100.0)
        self.poll_at(100.05)
        self.assertEqual(self.closes, [100.0])

    def test_four_seconds_away_by_the_clock_not_by_ticks(self) -> None:
        for now in (100.0, 101.0, 103.9):
            self.poll_at(now)
        self.assertEqual(self.closes, [])
        self.poll_at(104.0)
        self.assertEqual(self.closes, [104.0])

    def test_coming_back_starts_the_count_again(self) -> None:
        self.poll_at(100.0)
        self.over = True
        self.poll_at(103.0)
        self.over = False
        self.poll_at(103.5)
        self.poll_at(107.0)
        self.assertEqual(self.closes, [])
        self.poll_at(107.5)
        self.assertEqual(self.closes, [107.5])


class MoreButtonTests(unittest.TestCase):
    def test_ring_badge_and_tooltip(self) -> None:
        b = F.MoreButton(theme(), FakeTips())
        self.addCleanup(b.deleteLater)
        self.assertEqual(b.tooltip_text(), F.MORE_TIP)
        b.set_progress(0.42, "about 3 min left")
        self.assertEqual(b.tooltip_text(), "More · Saving audio 42%, about 3 min left")
        self.assertEqual(b.accessibleDescription(), "Saving audio, 42 percent")
        render(b)
        b.finish_progress(False)
        self.assertIsNone(b.fraction)
        self.assertEqual(b.tooltip_text(), F.MORE_TIP)
        b.set_badge(True)
        self.assertEqual(b.accessibleName(), "More, saving the audio failed")
        render(b)
        self.assertEqual(b.focusPolicy(), Qt.FocusPolicy.NoFocus)


if __name__ == "__main__":
    unittest.main()
