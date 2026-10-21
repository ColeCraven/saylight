"""Tests for the tray menu (ui/tray.py): Open a file, Continue reading, Enter PDF password, the Type or paste, Clear
text and Undo clear items, Save as audio and its progress, Pronunciations, Settings, and the Type or paste box
and the sheets closing when the menu opens or the icon is clicked.

The tray icon is created but never shown; the menu is built directly.
"""

import sys
import unittest

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from saylight.theme import Theme
from saylight.ui.tray import Tray

_app = None


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


class FakeSettings(QObject):
    changed = Signal(str)

    def get(self, key: str):
        return {"text_size": "medium", "reading_tint": "auto", "reading_font": "verdana", "spacing": "wide",
                "hud_when_hidden": True}[key]

    def set(self, key: str, value) -> None:
        pass


class FakeSystem:
    def color_scheme_dark(self) -> bool:
        return True

    def apps_use_light_theme(self) -> bool:
        return False

    def system_uses_light_theme(self) -> bool:
        return False

    def high_contrast(self) -> bool:
        return False

    def animations_enabled(self) -> bool:
        return False

    def text_scale_factor(self) -> float:
        return 1.0

    def accent_palette(self) -> None:
        return None

    def color_prevalence(self) -> bool:
        return False

    def sys_colors(self) -> dict:
        return {}

    def font_families(self) -> frozenset[str]:
        return frozenset()


class Recorder:
    """Any method call is recorded as (name, *args)."""

    def __init__(self, calls: list, prefix: str = "") -> None:
        self._calls, self._prefix = calls, prefix

    def __getattr__(self, name: str):
        return lambda *args, **kw: self._calls.append((self._prefix + name, *args, *kw.values()))


class FakeCtl:
    def __init__(self) -> None:
        self.settings = FakeSettings()
        self.calls: list[tuple] = []
        self.engine = Recorder(self.calls, "engine.")
        self.engine.document = None
        self.text = self.draft = self.undo = self.compose_open = False
        self.point: tuple[str, int] | None = None
        self.saving: float | None = None
        self.saved = ""

    def __getattr__(self, name: str):  # toggle_panel, open_compose, clear_box, quit and the rest
        return lambda *args, **kw: self.calls.append((name, *args, *kw.values()))

    def close_sheets(self, reason: str) -> None:
        pass

    def continue_point(self):
        return self.point

    def save_fraction(self):
        return self.saving

    def saved_audio(self) -> str:
        return self.saved

    def composing(self) -> bool:
        return self.compose_open

    def close_compose(self, reason: str) -> None:
        self.calls.append(("close_compose", reason))
        self.compose_open = False

    def panel_visible(self) -> bool:
        return True

    def speed(self) -> float:
        return 1.0

    def has_text(self) -> bool:
        return self.text

    def has_draft(self) -> bool:
        return self.draft

    def can_undo_clear(self) -> bool:
        return self.undo


class TrayMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctl = FakeCtl()
        self.tray = Tray(self.ctl, Theme(self.ctl.settings, system=FakeSystem()))
        self.addCleanup(self.tray.hide)

    def build(self, state: str, *, has_text: bool = False) -> dict[str, bool]:
        """Build the menu for a shown state; returns {item text: enabled}."""
        self.ctl.text = has_text
        self.tray.set_state(state, {"label": state.capitalize(), "has_text": has_text,
                                    "can_type": state not in ("copying", "opening"),
                                    "can_clear": has_text and state not in ("copying", "opening")})
        self.tray._build_menu()
        return {a.text(): a.isEnabled() for a in self.tray.menu.actions() if not a.isSeparator()}

    def texts(self) -> list[str]:
        return [a.text() for a in self.tray.menu.actions() if not a.isSeparator()]

    def box_group(self) -> list[str]:
        """The items between Open a file and the Save as audio group."""
        texts = self.texts()
        end = next(i for i, t in enumerate(texts) if t.startswith(("Save as audio", "Saving audio", "Show saved")))
        return texts[texts.index("Open a file…") + 1:end]

    def test_open_a_file(self) -> None:
        items = self.build("ready")
        texts = self.texts()
        self.assertTrue(items["Open a file…"])
        self.assertEqual(texts.index("Open a file…") + 1, texts.index("Type or paste text…"))
        {a.text(): a for a in self.tray.menu.actions()}["Open a file…"].trigger()
        self.assertEqual(self.ctl.calls, [("choose_file", "tray")])
        for state in ("copying", "opening"):  # not while copying or opening
            with self.subTest(state=state):
                self.assertFalse(self.build(state)["Open a file…"])

    def test_continue_reading_a_file(self) -> None:
        self.ctl.point = ("A very long title of a paper about reading with dyslexia", 14)
        self.build("ready")
        texts = self.texts()
        item = texts[texts.index("Open a file…") + 1]
        self.assertTrue(item.startswith("Continue reading “") and "…" in item, item)
        self.assertLessEqual(len(item), len("Continue reading “”") + 32)
        {a.text(): a for a in self.tray.menu.actions()}[item].trigger()
        self.assertEqual(self.ctl.calls, [("continue_reading", "tray")])

    def test_enter_pdf_password_only_with_the_notice(self) -> None:
        self.build("ready")
        self.assertNotIn("Enter PDF password…", self.texts())
        self.tray.set_state("open_failed", {"label": "Password needed", "kind": "password", "can_type": True,
                                            "underlying": "ready"})
        self.tray._build_menu()
        {a.text(): a for a in self.tray.menu.actions()}["Enter PDF password…"].trigger()
        self.assertEqual(self.ctl.calls, [("open_password", "tray")])

    def test_save_as_audio_and_its_progress(self) -> None:
        items = self.build("ready")
        self.assertFalse(items["Save as audio…"])  # nothing to save
        self.ctl.engine.document = object()
        items = self.build("stopped", has_text=True)
        self.assertTrue(items["Save as audio…"])
        self.assertIn("Pronunciations…", items)
        self.ctl.saving = 0.42
        items = self.build("stopped", has_text=True)
        self.assertFalse(items["Saving audio… 42%"])
        self.assertIn("Cancel saving audio", items)
        self.assertIn("Quit Saylight (stops saving audio)", items)
        self.assertIn("Saving audio 42%", self.tray.icon.toolTip())
        self.ctl.saving, self.ctl.saved = None, "saved.mp3"
        items = self.build("stopped", has_text=True)
        self.assertIn("Show saved audio in folder", items)
        self.assertIn("Quit Saylight", items)

    def test_read_the_notes_and_references_at_the_end_of_the_main_text(self) -> None:
        self.ctl.text = True
        self.tray.set_state("finished", {"label": "Finished", "has_text": True, "can_type": True,
                                         "end_of_main_text": True})
        self.tray._build_menu()
        self.assertIn("Read the notes and references too", self.texts())
        self.build("finished", has_text=True)
        self.assertNotIn("Read the notes and references too", self.texts())

    def test_opening_tooltip(self) -> None:
        self.tray.set_state("opening", {"label": "Opening…", "open_kind": "file", "title": "x" * 60,
                                        "open_done": 40, "open_total": 160})
        tip = self.tray.icon.toolTip()
        self.assertTrue(tip.startswith("Saylight · Opening “"), tip)
        self.assertTrue(tip.endswith("… page 40 of 160"), tip)
        self.assertLessEqual(len(tip), 127)

    def test_settings_opens_the_settings_page(self) -> None:
        self.build("ready")
        actions = {a.text(): a for a in self.tray.menu.actions()}
        self.assertNotIn("Reading display…", actions)
        actions["Settings…"].trigger()
        self.assertEqual(self.ctl.calls, [("open_settings",)])

    def test_ready_offers_type_or_paste_only(self) -> None:
        items = self.build("ready")
        self.assertTrue(items["Type or paste text…"])
        self.assertEqual(self.box_group(), ["Type or paste text…"])

    def test_copying_disables_type_and_hides_clear(self) -> None:
        self.ctl.draft = True
        items = self.build("copying", has_text=True)
        self.assertFalse(items["Type or paste text…"])
        self.assertEqual(self.box_group(), ["Type or paste text…", "Pause", "Stop"])

    def test_reading_and_resting_with_text(self) -> None:
        self.build("reading", has_text=True)
        self.assertEqual(self.box_group(), ["Type or paste text…", "Pause", "Stop", "Clear text"])
        self.build("stopped", has_text=True)
        self.assertEqual(self.box_group(), ["Type or paste text…", "Read again from start", "Clear text"])

    def test_typed_text_alone_can_be_cleared(self) -> None:
        self.ctl.draft = True
        self.build("ready")
        self.assertEqual(self.box_group(), ["Type or paste text…", "Clear text"])

    def test_undo_during_the_undo_window(self) -> None:
        self.ctl.undo = True
        self.build("ready")
        self.assertEqual(self.box_group(), ["Type or paste text…", "Undo clear"])

    def test_items_call_the_controller(self) -> None:
        self.ctl.undo = True
        self.build("stopped", has_text=True)
        actions = {a.text(): a for a in self.tray.menu.actions()}
        for text in ("Type or paste text…", "Clear text", "Undo clear"):
            actions[text].trigger()
        self.assertEqual(self.ctl.calls, [("open_compose", "tray"), ("clear_box",), ("undo_clear",)])

    def test_opening_the_menu_closes_the_box(self) -> None:
        self.build("ready")
        self.assertEqual(self.ctl.calls, [])
        self.ctl.compose_open = True
        self.build("ready")
        self.assertEqual(self.ctl.calls, [("close_compose", "tray")])


class TrayClickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctl = FakeCtl()
        self.tray = Tray(self.ctl, Theme(self.ctl.settings, system=FakeSystem()))
        self.addCleanup(self.tray.hide)

    def test_click_closes_the_box_without_toggling_the_panel(self) -> None:
        self.ctl.compose_open = True
        self.tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertEqual(self.ctl.calls, [("close_compose", "tray")])
        QCoreApplication.processEvents()
        self.assertEqual(self.ctl.calls, [("close_compose", "tray"), ("restore_foreground",)])

    def test_click_toggles_the_panel_otherwise(self) -> None:
        self.tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertEqual(self.ctl.calls, [("toggle_panel", True)])


if __name__ == "__main__":
    unittest.main()
