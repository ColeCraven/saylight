"""Tests for the update line (ui/update_bar.py) and its tray twin: when it shows, its states, its links, and the
card moving down to make room for it.

The UpdateService is a fake; links that open the browser are caught before they reach it.
"""

import unittest
from unittest import mock

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from saylight.tests.test_settings_pages import ServicesCase, release, setUpModule  # noqa: F401
from saylight.ui import update_bar as U
from saylight.ui.tray import Tray


def info(state: str) -> dict:
    return {"label": state.capitalize(), "play_action": "pause" if state in ("reading", "paused") else "read",
            "can_type": True}


class UpdateLineTests(ServicesCase):
    def setUp(self) -> None:
        super().setUp()
        self.panel.present("ready", info("ready"))
        self.bar = self.panel.update_bar
        self.strip = self.panel.strip
        self.opened: list[str] = []
        patcher = mock.patch.object(U, "open_url", self.opened.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def line(self) -> str:
        return self.strip.text.accessibleName()

    def test_hidden_until_a_newer_version_is_found(self) -> None:
        self.assertTrue(self.strip.isHidden())
        self.assertEqual(self.panel.view.y(), 44)

    def test_ready_line_and_the_card_moving_down(self) -> None:
        h = self.panel.height()
        self.ctl.updates.available.emit(release())
        self.assertEqual(self.line(), "Saylight 1.3 is ready · What's new · Install · Not now")
        self.assertFalse(self.strip.isHidden())
        self.assertEqual(self.strip.geometry().getRect(), (12, 44, self.panel.width() - 24, 32))
        self.assertEqual(self.panel.view.y(), 84)
        self.assertEqual(self.panel.height(), h + 40)
        self.assertEqual(self.strip.text.link_keys(), ["notes", "install", "later"])

    def test_only_while_not_reading(self) -> None:
        self.ctl.updates.available.emit(release())
        for state in ("copying", "preparing", "reading", "waiting", "paused"):
            self.panel.present(state, info(state))
            self.assertTrue(self.strip.isHidden(), state)
        for state in ("finished", "stopped", "ready"):
            self.panel.present(state, info(state))
            self.assertFalse(self.strip.isHidden(), state)
        self.panel.set_page("settings")
        self.assertTrue(self.strip.isHidden())

    def test_no_install_without_an_installer(self) -> None:
        self.ctl.updates.available.emit(release(installer=False))
        self.assertEqual(self.line(), "Saylight 1.3 is ready · Get it · Not now")
        self.strip.linkClicked.emit("page")
        self.assertEqual(self.opened, [release().url])

    def test_the_python_version_and_smart_app_control_get_the_download_page(self) -> None:
        # Setup could replace a working copy with a build Smart App Control then refuses to start.
        self.ctl.updates.can_install = False
        self.ctl.updates.available.emit(release())
        self.assertEqual(self.line(), "Saylight 1.3 is ready · Get it · Not now")
        text, enabled, action = self.bar.tray_item()
        self.assertEqual((text, enabled), ("Open the Saylight 1.3 download page", True))
        action()
        self.assertEqual(self.opened, [release().url])
        self.assertNotIn(("install", "1.3.0"), self.ctl.updates.calls)

    def test_whats_new_opens_the_release_page(self) -> None:
        self.ctl.updates.available.emit(release())
        self.strip.linkClicked.emit("notes")
        self.assertEqual(self.opened, [release().url])

    def test_install_download_and_setup(self) -> None:
        self.ctl.updates.available.emit(release())
        self.strip.linkClicked.emit("install")
        self.assertEqual(self.ctl.updates.calls, [("install", "1.3.0")])
        self.assertEqual(self.line(), "Downloading Saylight 1.3…")
        self.ctl.updates.downloadProgress.emit(9_856_699, 23_468_330)
        self.assertEqual(self.line(), "Downloading Saylight 1.3… 42%")
        self.assertTrue(self.strip.show_progress)
        self.assertAlmostEqual(self.strip.progress.value, 0.42, places=2)
        self.ctl.updates.downloadProgress.emit(12_400_000, 0)
        self.assertEqual(self.line(), "Downloading Saylight 1.3… 12.4 MB")
        self.ctl.updates.downloadProgress.emit(23_468_330, 23_468_330)
        self.assertEqual(self.line(), "Starting Setup…")
        self.strip.linkClicked.emit("install")  # one install at a time
        self.assertEqual(self.ctl.updates.calls, [("install", "1.3.0")])

    def test_a_failure_offers_the_download_page(self) -> None:
        self.ctl.updates.available.emit(release())
        self.strip.linkClicked.emit("install")
        self.ctl.updates.failed.emit("checksum")
        self.assertEqual(self.line(), "Couldn't update · Open the download page · Not now")
        self.assertEqual(self.strip.glyph_token, "Critical")
        self.assertIn("damaged", self.strip.accessibleDescription())
        self.assertFalse(self.strip.show_progress)
        self.strip.linkClicked.emit("page")
        self.assertEqual(self.opened, [release().url])
        for kind, words in (("network", "didn't finish"), ("launch", "didn't start Setup")):
            self.ctl.updates.failed.emit(kind)
            self.assertIn(words, self.strip.text.text_tip)

    def test_not_now_skips_the_version_and_hides_the_line(self) -> None:
        self.ctl.updates.available.emit(release())
        self.strip.linkClicked.emit("later")
        self.assertEqual(self.ctl.updates.calls, [("skip", "1.3.0")])
        self.assertTrue(self.strip.isHidden())
        self.assertEqual(self.panel.view.y(), 44)
        self.assertEqual(self.bar.tray_item()[0], "Install Saylight 1.3…")  # the tray item stays

    def test_presses_off_the_links_drag_the_panel(self) -> None:
        self.ctl.updates.available.emit(release())
        event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(4, 10), QPointF(4, 10), QPointF(1004, 1010),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.strip.text.mousePressEvent(event)
        self.assertFalse(event.isAccepted())

    def test_short_versions(self) -> None:
        self.assertEqual([U.short_version(v) for v in ("1.3.0", "1.3.1", "2.0.0", "1.3")], ["1.3", "1.3.1", "2.0",
                                                                                            "1.3"])


class TrayUpdateItemTests(ServicesCase):
    def setUp(self) -> None:
        super().setUp()
        self.ctl.panel = self.panel
        self.ctl.composing = lambda: False
        self.ctl.panel_visible = lambda: True
        self.ctl.has_text = lambda: False
        self.ctl.can_undo_clear = lambda: False
        self.ctl.engine = mock.Mock()
        for name in ("open_settings", "reset_position", "open_log", "quit", "retry_shortcut"):
            setattr(self.ctl, name, lambda: None)
        self.tray = Tray(self.ctl, self.theme)
        self.addCleanup(self.tray.hide)

    def items(self) -> dict:
        self.tray.set_state("ready", {"label": "Ready", "can_type": True})
        self.tray._build_menu()
        return {a.text(): a for a in self.tray.menu.actions() if not a.isSeparator()}

    def test_no_item_without_a_newer_version(self) -> None:
        self.assertFalse(any("Saylight 1" in text for text in self.items()))

    def test_install_item_follows_the_line(self) -> None:
        self.ctl.updates.available.emit(release())
        item = self.items()["Install Saylight 1.3…"]
        item.trigger()
        self.assertEqual(self.ctl.updates.calls, [("install", "1.3.0")])
        self.ctl.updates.downloadProgress.emit(11_734_165, 23_468_330)
        self.assertFalse(self.items()["Downloading Saylight 1.3… 50%"].isEnabled())
        self.ctl.updates.failed.emit("network")
        with mock.patch.object(U, "open_url") as opened:
            self.items()["Open the Saylight 1.3 download page"].trigger()
        opened.assert_called_once_with(release().url)

    def test_a_skipped_version_keeps_its_tray_item(self) -> None:
        self.ctl.updates.latest = release()  # found, but skipped earlier: no panel line
        self.assertIn("Install Saylight 1.3…", self.items())
        self.assertFalse(self.panel.update_bar.visible())


if __name__ == "__main__":
    unittest.main()
