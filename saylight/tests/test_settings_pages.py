"""Tests for the settings page (ui/settings_pages.py and ui/general_page.py): the tab bar, one height for every
tab, scrolling, the Reading display rows and the General tab.

The panel gets a fake controller with fake voice, preview and update services. Its window is created but never
shown, so every animation snaps; nothing touches the network, the registry or the speakers.
"""

import os
import shutil
import tempfile
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent

from saylight.settings import Settings
from saylight.tests.test_panel import FakeCtl, FakeSystem, info, setUpModule  # noqa: F401 (setUpModule runs here)
from saylight.theme import Theme
from saylight.ui import panel as P
from saylight.updates import Release
from saylight.voices.catalog import VoiceCatalog


class FakePreview(QObject):
    stateChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.state = "idle"
        self.calls: list[tuple] = []

    def play(self, voice: str, speed: float) -> None:
        self.calls.append(("play", voice, speed))
        self.set("loading")

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.set("idle")

    def set(self, state: str) -> None:
        self.state = state
        self.stateChanged.emit(state)


class FakeUpdates(QObject):
    available = Signal(object)
    downloadProgress = Signal(int, int)
    failed = Signal(str)
    checked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.latest = None
        self.calls: list[tuple] = []

    def check_now(self) -> None:
        self.calls.append(("check_now",))

    def skip(self, version: str) -> None:
        self.calls.append(("skip", version))

    def install(self, release) -> None:
        self.calls.append(("install", release.version))


class ServicesCtl(FakeCtl):
    """The fake controller with the 1.2 services the settings pages use."""

    def __init__(self, settings: Settings, theme: Theme) -> None:
        super().__init__(settings, theme)
        self.fetches = 0
        self.online: list[dict] | None = []
        self.catalog = VoiceCatalog(fetch=self._fetch)
        self.preview = FakePreview()
        self.updates = FakeUpdates()
        self.version = "1.2.0"
        self.start_on = False

    def _fetch(self) -> list[dict]:
        self.fetches += 1
        if self.online is None:
            raise OSError("offline")
        return self.online

    def set_voice(self, voice_id: str) -> None:
        self.calls.append(("set_voice", voice_id))
        self.settings.set("voice", voice_id)

    def start_with_windows(self) -> bool:
        return self.start_on

    def set_start_with_windows(self, on: bool) -> bool:
        self.calls.append(("set_start_with_windows", on))
        self.start_on = on
        return True

    def open_notices(self) -> bool:
        self.calls.append(("open_notices",))
        return True


def release(installer: bool = True) -> Release:
    return Release("1.3.0", "https://github.com/ColeCraven/saylight/releases/tag/v1.3.0",
                   "https://github.com/ColeCraven/saylight/releases/download/v1.3.0/Saylight-Setup.exe"
                   if installer else "", 23_468_330)


class ServicesCase(unittest.TestCase):
    k = 1.0

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_settings_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = Settings(os.path.join(self.dir, "s.json"))
        self.addCleanup(self.settings.flush)  # before the folder goes, or a pending save brings it back
        self.theme = Theme(self.settings, system=FakeSystem(k=self.k))
        self.ctl = ServicesCtl(self.settings, self.theme)
        self.panel = P.Panel(self.ctl)
        self.addCleanup(self.panel.deleteLater)
        self.tabs = self.panel.tabs

    def open_tab(self, i: int):
        self.panel.set_page("settings")
        self.tabs.set_index(i)
        self.tabs.page.set_shown(True)
        return self.tabs.page

    def click(self, widget, pos: QPointF) -> None:
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            buttons = Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
            QCoreApplication.sendEvent(widget, QMouseEvent(kind, pos, pos, pos + QPointF(1000, 1000),
                                                           Qt.MouseButton.LeftButton, buttons,
                                                           Qt.KeyboardModifier.NoModifier))

    def wheel(self, widget, notches: int) -> bool:
        pos = QPointF(widget.width() / 2, widget.height() / 2)
        event = QWheelEvent(pos, pos + QPointF(1000, 1000), QPoint(), QPoint(0, -120 * notches),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                            False)
        QCoreApplication.sendEvent(widget, event)
        return event.isAccepted()


class TabTests(ServicesCase):
    def test_three_tabs_in_the_header(self) -> None:
        bar = self.tabs.bar
        self.assertEqual([item[0] for item in bar.items], ["Reading display", "Voice", "General"])
        self.panel.set_page("settings")
        self.assertTrue(bar.isVisibleTo(self.panel))
        self.assertTrue(self.panel.status.isHidden())
        self.assertEqual((bar.x(), bar.y(), bar.height()), (50, 8, 32))
        self.assertEqual(bar.geometry().right() + 1, self.panel.btn_hide.x() - 8)
        self.assertEqual(bar.accessibleName(), "Settings, Reading display tab")
        self.tabs.set_index(1)
        self.assertEqual(bar.accessibleName(), "Settings, Voice tab")
        self.assertEqual(self.panel.btn_more.accessibleName(), "More")

    def test_every_tab_has_the_same_height_and_the_bar_stays_put(self) -> None:
        self.panel.set_page("settings")
        sizes = set()
        for i in range(3):
            self.tabs.set_index(i)
            self.panel.relayout()
            sizes.add((self.panel.height(), self.tabs.bar.geometry().getRect()))
        self.assertEqual(len(sizes), 1)

    def test_reading_display_keeps_the_1_1_rows(self) -> None:
        self.panel.set_page("settings")
        page = self.tabs.pages[0]
        tabs_top = self.tabs.y()
        ys = [tabs_top + seg.y() - 3 for _key, _lbl, seg, _opts in page.option_rows]
        self.assertEqual(ys, [46, 88, 130, 172])
        self.assertEqual(tabs_top + page.swatches.geometry().bottom() + 1, 254)
        self.assertFalse(hasattr(page, "row_links"))  # what's read aloud moved to Voice

    def test_only_the_current_tab_shows(self) -> None:
        self.panel.set_page("settings")
        self.tabs.set_index(2)
        shown = [any(w.isVisibleTo(self.panel) for w in page.widgets()) for page in self.tabs.pages]
        self.assertEqual(shown, [False, False, True])
        self.panel.set_page("reading")
        self.assertTrue(self.tabs.isHidden() and self.tabs.bar.isHidden())

    def test_display_rows_change_settings(self) -> None:
        page = self.open_tab(0)
        page.option_rows[0][2].selected.emit(2)
        self.assertEqual(self.settings.get("text_size"), "large")
        self.settings.set("spacing", "extra")
        self.assertEqual(page.option_rows[2][2].index, 2)

    def test_a_tab_taller_than_the_room_scrolls(self) -> None:
        self.panel._max_height = lambda: 300
        page = self.open_tab(1)
        self.panel.relayout()
        self.assertEqual(self.panel.height(), 300)
        self.assertGreater(self.tabs.max_scroll(), 0)
        top = page.caption.y()
        self.assertTrue(self.wheel(self.tabs, 1))
        self.assertEqual(self.tabs.scroll.value, 42)
        self.assertEqual(page.caption.y(), top - 42)
        self.wheel(self.tabs, 10)
        self.assertEqual(self.tabs.scroll.value, self.tabs.max_scroll())
        self.assertFalse(self.wheel(self.tabs, 1))  # at the end: nothing left to scroll
        self.tabs.set_index(2)
        self.assertEqual(self.tabs.scroll.value, 0)

    def test_a_short_page_does_not_scroll(self) -> None:
        self.open_tab(2)
        self.assertEqual(self.tabs.max_scroll(), 0)
        self.assertFalse(self.wheel(self.tabs, 1))

    def test_nothing_on_the_page_takes_focus(self) -> None:
        self.panel.set_page("settings")
        widgets = [self.tabs.bar] + [w for page in self.tabs.pages for w in page.widgets()]
        self.assertTrue(all(w.focusPolicy() == Qt.FocusPolicy.NoFocus for w in widgets))


class LargeTextTabTests(ServicesCase):
    k = 1.5

    def test_rows_scale_with_k(self) -> None:
        self.panel.set_page("settings")
        page = self.tabs.pages[0]
        ys = [self.tabs.y() + seg.y() - self.theme.px(3) for _key, _lbl, seg, _opts in page.option_rows]
        self.assertEqual(ys, [self.theme.px(46 + 42 * i) for i in range(4)])
        self.assertEqual(self.tabs.bar.height(), self.theme.px(32))


class GeneralPageTests(ServicesCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = self.open_tab(2)

    def test_start_with_windows(self) -> None:
        self.assertFalse(self.page.row_start.checked)
        self.page.row_start.clicked.emit()
        self.assertIn(("set_start_with_windows", True), self.ctl.calls)
        self.assertTrue(self.page.row_start.checked)

    def test_shortcut_row(self) -> None:
        parts = self.page.val_shortcut.parts
        self.assertEqual(parts[:2], [("key", "Ctrl"), ("key", "`")])
        self.assertEqual(parts[-1], ("text", " reads the highlighted text"))
        self.assertEqual(self.page.val_shortcut.accessibleName(), "Ctrl + ` reads the highlighted text")
        self.assertEqual(self.page.row_start.accessibleName(), "Start with Windows: off")  # before any change
        self.panel.present("shortcut_unavailable", {**info("ready"), "label": "Shortcut unavailable"})
        self.assertEqual(self.page.val_shortcut.parts[-1], ("text", " · another app is using it"))
        self.assertEqual(self.page.val_shortcut.color_token, "Critical")

    def test_a_long_shortcut_never_runs_off_the_row(self) -> None:
        from saylight.ui.controls import measure_parts
        v = self.page.val_shortcut
        for chord in ("Ctrl+Alt+Shift+F12", "Ctrl+Alt+Shift+Win+PageDown"):
            with self.subTest(chord=chord):
                self.settings.set("hotkeys.read", chord)
                self.assertGreater(v.width(), 0)
                self.assertLessEqual(measure_parts(self.theme, v.parts), v.width())
                self.assertEqual([k for k, _ in v.parts[:4]], ["key"] * 4)  # the keycaps always stay
                self.assertTrue(v.accessibleName().endswith("reads the highlighted text"))
        self.settings.set("hotkeys.read", "Ctrl+`")
        self.assertEqual(v.parts[-1], ("text", " reads the highlighted text"))

    def test_update_check_can_be_turned_off_and_on(self) -> None:
        line = self.page.updates_line
        self.assertEqual(line.accessibleName(), "Saylight checks GitHub for updates once a day · Turn off")
        line.linkClicked.emit("toggle")
        self.assertFalse(self.settings.get("updates.check"))
        self.assertEqual(line.accessibleName(), "Update checks are off · Turn on")
        line.linkClicked.emit("toggle")
        self.assertTrue(self.settings.get("updates.check"))

    def test_the_turn_off_link_takes_a_click(self) -> None:
        line = self.page.updates_line
        self.click(line, line.link_rect("toggle").center())
        self.assertFalse(self.settings.get("updates.check"))
        self.click(line, QPointF(4, line.height() / 2))  # the text itself is not a link
        self.assertFalse(self.settings.get("updates.check"))

    def test_check_for_updates(self) -> None:
        line = self.page.version_line
        self.assertEqual(line.accessibleName(), "Saylight 1.2.0 · Check for updates")
        line.linkClicked.emit("check")
        self.assertEqual(self.ctl.updates.calls, [("check_now",)])
        self.assertEqual(line.accessibleName(), "Saylight 1.2.0 · Checking…")
        self.ctl.updates.checked.emit("current")
        self.assertEqual(line.accessibleName(), "Saylight 1.2.0 · You have the latest version")
        self.page.set_shown(False)  # leaving the tab clears the result
        self.assertEqual(line.accessibleName(), "Saylight 1.2.0 · Check for updates")
        line.linkClicked.emit("check")
        self.ctl.updates.latest = release()
        self.ctl.updates.checked.emit("newer")
        self.assertEqual(line.accessibleName(), "Saylight 1.2.0 · Saylight 1.3 is ready")
        self.page.set_shown(False)
        line.linkClicked.emit("check")
        self.ctl.updates.checked.emit("error")
        self.assertEqual(line.accessibleName(), "Saylight 1.2.0 · Couldn't check. Try again later.")

    def test_a_check_nobody_asked_for_is_not_reported(self) -> None:
        self.ctl.updates.checked.emit("current")
        self.assertEqual(self.page.version_line.accessibleName(), "Saylight 1.2.0 · Check for updates")

    def test_privacy_and_licences_opens_the_notices(self) -> None:
        self.page.row_notices.clicked.emit()
        self.assertEqual(self.ctl.calls[-1], ("open_notices",))

    def test_pick_up_where_you_left_off(self) -> None:
        from saylight.ui import general_page as G
        p = self.page
        self.assertEqual(p.lbl_files.parts, [("text", G.FILES_CAPTION)])
        self.assertFalse(p.row_resume.checked)  # off by default
        self.assertEqual(p.row_resume.detail, G.RESUME_DETAIL)
        self.assertEqual(p.row_resume.row_height(), 52)
        forgot = []
        self.ctl.set_resume = lambda on: (self.settings.set("resume.enabled", on), forgot.append(on), 0 if on else 7)[2]
        p.row_resume.clicked.emit()
        self.assertTrue(self.settings.get("resume.enabled"))
        p.row_resume.clicked.emit()
        self.assertEqual(forgot, [True, False])
        self.assertEqual(p.row_resume.detail, "Saylight forgot your place in 7 files.")
        p._forgot_timer.timeout.emit()
        self.assertEqual(p.row_resume.detail, G.RESUME_DETAIL)
        self.assertEqual(G.forgot_detail(1), "Saylight forgot your place in 1 file.")
        self.assertEqual(G.forgot_detail(0), "Saylight forgot every place it remembered.")
        self.settings.set("resume.enabled", True)  # turned on elsewhere (the nudge)
        self.assertTrue(p.row_resume.checked)

    def test_the_files_group_sits_between_two_rules(self) -> None:
        p = self.page
        y = p.lbl_files.geometry().y()
        self.assertEqual(p.rules[0] + 5, y)
        self.assertEqual(p.row_resume.geometry().y(), y + 22)
        self.assertEqual(p.rules[1], p.row_resume.geometry().bottom() + 1 + 2 + 4)


class MissingServicesTests(unittest.TestCase):
    """The panel still builds without the 1.2 services (as in 1.1 tests); their rows are disabled."""

    def test_rows_are_disabled(self) -> None:
        folder = tempfile.mkdtemp(prefix="saylight_settings_test_")
        self.addCleanup(shutil.rmtree, folder, True)
        settings = Settings(os.path.join(folder, "s.json"))
        self.addCleanup(settings.flush)
        panel = P.Panel(FakeCtl(settings, Theme(settings, system=FakeSystem())))
        self.addCleanup(panel.deleteLater)
        voice, general = panel.tabs.pages[1], panel.tabs.pages[2]
        self.assertFalse(voice.row_all.isEnabled())
        self.assertFalse(general.row_start.isEnabled() or general.row_notices.isEnabled())
        self.assertEqual(general.version_line.parts, [])
        self.assertFalse(panel.update_bar.visible())


if __name__ == "__main__":
    unittest.main()
