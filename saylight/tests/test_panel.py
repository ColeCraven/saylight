"""Tests for the panel (ui/panel.py): the Type and Clear buttons, the empty card that opens Type or paste,
"Text cleared. [Undo]", the covered state and the geometry helpers the Type or paste box uses.

The panel gets a fake controller and engine. Its window is created but never shown, and clicks on the card are
delivered as synthetic mouse events.
"""

import os
import shutil
import sys
import tempfile
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from saylight.settings import Settings
from saylight.text_model import Document
from saylight.theme import Theme
from saylight.ui import panel as P
from saylight.ui.motion import Clock

_app = None

TEXT = "Working memory capacity predicts reading comprehension. Comprehension improved."
LABELS = {"ready": "Ready", "ready_offline": "Offline", "shortcut_unavailable": "Shortcut unavailable",
          "copying": "Copying selection…", "preparing": "Preparing audio…", "reading": "Reading",
          "waiting": "Preparing audio…", "paused": "Paused", "finished": "Finished", "stopped": "Stopped",
          "nothing_selected": "Nothing selected", "error_service": "Voice service error"}


def setUpModule() -> None:
    global _app
    _app = QCoreApplication.instance() or QApplication([sys.argv[0], "-platform", "windows"])


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
        return False  # every tween snaps, so states can be read straight away

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
    def hover(self, owner, text_fn) -> None:
        pass

    def leave(self, owner) -> None:
        pass

    def press(self, owner) -> None:
        pass

    def refresh(self, owner) -> None:
        pass

    def show_at(self, owner, text, rect) -> None:
        pass


class FakeEngine:
    def __getattr__(self, name: str):
        return lambda *args: None


class FakeForeground:
    def last_app(self) -> int:
        return 0


class FakeCtl:
    def __init__(self, settings: Settings, theme: Theme) -> None:
        self.settings, self.theme = settings, theme
        self.tips, self.clock = FakeTips(), Clock()
        self.engine, self.foreground = FakeEngine(), FakeForeground()
        self.draft = False
        self.chord = ""
        self.calls: list[tuple] = []

    def speed(self) -> float:
        return 1.0

    def set_speed(self, v: float) -> None:
        pass

    def set_panel_visible(self, on: bool) -> None:
        self.calls.append(("set_panel_visible", on))

    def schedule_theme_refresh(self) -> None:
        pass

    def tint_items(self) -> list:
        return []

    def has_draft(self) -> bool:
        return self.draft

    def compose_shortcut(self) -> str:
        return self.chord

    def open_compose(self, source: str) -> None:
        self.calls.append(("open_compose", source))

    def clear_box(self) -> None:
        self.calls.append(("clear_box",))

    def undo_clear(self) -> None:
        self.calls.append(("undo_clear",))


def info(state: str, *, has_text: bool = False, underlying: str = "") -> dict:
    """What the engine reports for a shown state (only the keys the panel reads)."""
    base = underlying if state == "nothing_selected" else state
    resting_read = {"ready", "ready_offline", "shortcut_unavailable", "finished", "stopped"}
    return {"label": LABELS.get(state, state), "sentence": 0 if has_text else -1, "total": 2 if has_text else 0,
            "reason": "empty" if state == "nothing_selected" else "", "underlying": underlying,
            "has_text": has_text, "play_action": "read" if base in resting_read else "pause",
            "prev_action": "none", "can_next": False, "can_stop": base in P.ACTIVE, "can_seek": False,
            "can_type": state != "copying", "can_clear": has_text and state != "copying"}


class PanelCase(unittest.TestCase):
    k = 1.0
    expanded = False

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_panel_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = Settings(os.path.join(self.dir, "s.json"))
        self.settings.set("panel.expanded", self.expanded)
        self.theme = Theme(self.settings, system=FakeSystem(k=self.k))
        self.ctl = FakeCtl(self.settings, self.theme)
        self.panel = P.Panel(self.ctl)
        self.addCleanup(self.panel.deleteLater)
        self.doc = Document.from_raw(TEXT)

    def present(self, state: str, *, has_text: bool = False, underlying: str = "") -> None:
        self.panel.on_session(self.doc if has_text else None)
        self.panel.present(state, info(state, has_text=has_text, underlying=underlying))

    def paint_card(self) -> None:
        """Paint the card once, which records where its action chips are."""
        view = self.panel.view
        img = QImage(view.size(), QImage.Format.Format_ARGB32)
        view.render(img)

    def mouse(self, kind: QEvent.Type, pos: QPointF) -> None:
        button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        buttons = Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
        glob = pos + QPointF(1000, 1000)
        QCoreApplication.sendEvent(self.panel.view, QMouseEvent(kind, pos, pos, glob, button, buttons,
                                                                Qt.KeyboardModifier.NoModifier))

    def click(self, pos: QPointF) -> None:
        self.mouse(QEvent.Type.MouseButtonPress, pos)
        self.mouse(QEvent.Type.MouseButtonRelease, pos)

    def blank_spot(self) -> QPointF:
        """A point on the card away from any line of text."""
        return QPointF(self.panel.view.width() - 6, 6)


class TransportRowTests(PanelCase):
    def test_type_and_clear_sit_after_stop_on_the_centre_line(self) -> None:
        p = self.panel
        self.present("ready")
        stop, typ, clear = p.btn_stop.geometry(), p.btn_type.geometry(), p.btn_clear.geometry()
        yt = stop.y() - 2
        self.assertEqual(typ, QRect(192, yt + 4, 32, 32))
        self.assertEqual(clear, QRect(228, yt + 4, 32, 32))
        self.assertEqual({QRectF(r).center().y() for r in (stop, typ, clear)}, {yt + 20})
        self.assertEqual((p.btn_prev.x(), p.play.x(), p.btn_next.x(), stop.x()), (12, 48, 96, 140))
        self.assertGreaterEqual(p.stepper.x() - (clear.right() + 1), 16)

    def test_the_settings_page_hides_them(self) -> None:
        p = self.panel
        self.present("ready")
        p.set_page("settings")
        self.assertTrue(p.btn_type.isHidden() and p.btn_clear.isHidden())
        p.set_page("reading")
        self.assertFalse(p.btn_type.isHidden() or p.btn_clear.isHidden())

    def test_buttons_call_the_controller(self) -> None:
        p = self.panel
        self.present("reading", has_text=True)
        p.btn_type.clicked.emit()
        p.btn_clear.clicked.emit()
        self.assertEqual(self.ctl.calls, [("open_compose", "button"), ("clear_box",)])

    def test_panel_never_takes_focus(self) -> None:
        for button in (self.panel.btn_type, self.panel.btn_clear):
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)


class TransportRowLargeTextTests(PanelCase):
    k = 1.5

    def test_still_16_dip_clear_of_the_stepper(self) -> None:
        p = self.panel
        self.present("ready")
        self.assertEqual(p.btn_clear.geometry().x(), 342)
        self.assertGreaterEqual(p.stepper.x() - (p.btn_clear.geometry().right() + 1), 24)


class EnabledStateTests(PanelCase):
    def enabled(self) -> tuple[bool, bool]:
        return self.panel.btn_type.isEnabled(), self.panel.btn_clear.isEnabled()

    def test_table(self) -> None:
        cases = [  # (state, has_text, underlying, draft) -> (Type, Clear)
            (("ready", False, "", False), (True, False)),
            (("ready_offline", False, "", False), (True, False)),
            (("shortcut_unavailable", False, "", False), (True, False)),
            (("ready", False, "", True), (True, True)),
            (("ready_offline", False, "", True), (True, True)),
            (("copying", False, "", True), (False, False)),
            (("copying", True, "", True), (False, False)),
            (("preparing", True, "", False), (True, True)),
            (("reading", True, "", False), (True, True)),
            (("waiting", True, "", False), (True, True)),
            (("paused", True, "", False), (True, True)),
            (("finished", True, "", False), (True, True)),
            (("stopped", True, "", False), (True, True)),
            (("error_service", True, "", False), (True, True)),
            (("nothing_selected", False, "ready", False), (True, False)),
            (("nothing_selected", False, "ready", True), (True, True)),
            (("nothing_selected", True, "stopped", False), (True, True)),
        ]
        for (state, has_text, underlying, draft), want in cases:
            with self.subTest(state=state, has_text=has_text, draft=draft):
                self.ctl.draft = draft
                self.present(state, has_text=has_text, underlying=underlying)
                self.assertEqual(self.enabled(), want)

    def test_refresh_follows_the_typed_text(self) -> None:
        self.present("ready")
        self.assertFalse(self.panel.btn_clear.isEnabled())
        self.ctl.draft = True
        self.panel.refresh_box_actions()
        self.assertTrue(self.panel.btn_clear.isEnabled())


class TooltipTests(PanelCase):
    UNDO = " You can undo this for 10 seconds."

    def tips(self) -> tuple[str, str]:
        return self.panel.btn_type.tooltip_text(), self.panel.btn_clear.tooltip_text()

    def test_clear_tooltip_names_what_it_clears(self) -> None:
        self.present("reading", has_text=True)
        self.assertEqual(self.tips()[1], "Stop and clear text." + self.UNDO)
        self.present("nothing_selected", has_text=True, underlying="paused")
        self.assertEqual(self.tips()[1], "Stop and clear text." + self.UNDO)
        self.present("stopped", has_text=True)
        self.assertEqual(self.tips()[1], "Clear text." + self.UNDO)
        self.ctl.draft = True
        self.present("ready")
        self.assertEqual(self.tips()[1], "Clear the text you typed." + self.UNDO)

    def test_type_tooltip_shortcut_and_kept_text(self) -> None:
        self.present("ready")
        self.assertEqual(self.tips()[0], "Type or paste text to read aloud")
        self.ctl.chord = "Ctrl+Alt+T"
        self.assertEqual(self.tips()[0], "Type or paste text to read aloud (Ctrl+Alt+T)")
        self.ctl.draft = True
        self.assertEqual(self.tips()[0], "Type or paste text (Ctrl+Alt+T). The text you typed is still there.")

    def test_names_and_descriptions(self) -> None:
        self.present("reading", has_text=True)
        p = self.panel
        self.assertEqual((p.btn_type.accessibleName(), p.btn_clear.accessibleName()),
                         ("Type or paste text", "Clear text"))
        self.assertEqual(p.btn_clear.accessibleDescription(), "Stop and clear text." + self.UNDO)


class CardMessageTests(PanelCase):
    def test_ready_hint_offers_type_or_paste(self) -> None:
        self.present("ready")
        v = self.panel.view
        self.assertEqual(len(v.messages), 3)
        self.assertEqual(v.messages[1], [("text", "or "), ("action", "type")])
        self.assertEqual(v._secondary_from, 2)

    def test_cleared_line_during_the_undo_window(self) -> None:
        self.present("ready")
        self.panel.set_cleared(True)
        self.assertEqual(self.panel.view.messages, [[("text", "Text cleared. "), ("action", "undo")]])
        self.panel.set_cleared(False)
        self.assertEqual(len(self.panel.view.messages), 3)

    def test_cleared_line_gives_way_to_text_and_overlays(self) -> None:
        self.panel.set_cleared(True)
        self.present("nothing_selected", underlying="ready")
        self.assertEqual(self.panel.view.messages, P.NOTHING_TEXT["empty"] + [P.TYPE_LINE])
        self.present("stopped", has_text=True)
        self.assertIsNone(self.panel.view.messages)

    def test_type_chip_disabled_while_copying(self) -> None:
        self.present("copying")
        self.assertEqual(self.panel.view.messages[1], [("text", "or "), ("action", "type")])
        self.assertFalse(self.panel.view._action_enabled["type"])
        self.present("ready")
        self.assertTrue(self.panel.view._action_enabled["type"])


class ExpandedCardTests(PanelCase):
    expanded = True

    def test_six_lines_without_the_spacer(self) -> None:
        self.present("ready")
        v = self.panel.view
        self.assertEqual(len(v.messages), 6)
        self.assertNotIn([("text", "")], v.messages)
        self.assertEqual(v._secondary_from, 2)


class CardClickTests(PanelCase):
    def test_click_anywhere_on_the_empty_card_opens_the_box(self) -> None:
        self.present("ready")
        self.paint_card()
        self.click(self.blank_spot())
        self.click(self.panel.view._hits["type"].center())
        self.assertEqual(self.ctl.calls, [("open_compose", "card"), ("open_compose", "card")])

    def test_message_cards_that_offer_type_or_paste_are_clickable(self) -> None:
        for state, underlying in (("shortcut_unavailable", ""), ("nothing_selected", "ready")):
            with self.subTest(state=state):
                self.ctl.calls.clear()
                self.present(state, underlying=underlying)
                self.assertEqual(self.panel.view.messages[-1], P.TYPE_LINE)  # the card says what a click does
                self.click(self.blank_spot())
                self.assertEqual(self.ctl.calls, [("open_compose", "card")])
        self.panel.on_session(None)
        for reason, lines in P.NOTHING_TEXT.items():
            with self.subTest(reason=reason):
                self.panel.present("nothing_selected", {**info("nothing_selected", underlying="ready"),
                                                        "reason": reason})
                self.assertEqual(self.panel.view.messages, lines + [P.TYPE_LINE])

    def test_cards_that_say_nothing_about_typing_take_no_clicks(self) -> None:
        self.present("ready_offline")  # reading typed text would fail too
        self.assertNotIn(P.TYPE_LINE, self.panel.view.messages)
        self.mouse(QEvent.Type.MouseMove, self.blank_spot())
        self.assertFalse(self.panel.view._card_hover)
        self.click(self.blank_spot())
        self.present("nothing_selected", has_text=True, underlying="stopped")  # over a reading: no typing in
        self.assertEqual(self.panel.view.messages, P.NOTHING_TEXT["empty"])
        self.click(self.blank_spot())
        self.assertEqual(self.ctl.calls, [])

    def test_not_clickable_with_text_while_copying_or_on_settings(self) -> None:
        self.present("stopped", has_text=True)
        self.click(self.blank_spot())
        self.present("copying")
        self.click(self.blank_spot())
        self.present("ready")
        self.panel.set_page("settings")
        self.click(self.blank_spot())
        self.assertEqual(self.ctl.calls, [])

    def test_after_a_clear_only_the_undo_chip_takes_clicks(self) -> None:
        self.present("ready")
        self.panel.set_cleared(True)
        self.paint_card()
        chip = self.panel.view._hits["undo"]
        self.click(self.blank_spot())
        self.click(QPointF(chip.right() + 4, chip.center().y()))  # a near miss never opens the box
        self.assertEqual(self.ctl.calls, [])
        self.click(chip.center())
        self.assertEqual(self.ctl.calls, [("undo_clear",)])

    def test_a_drag_moves_the_panel_instead(self) -> None:
        self.present("ready")
        start = self.blank_spot()
        self.mouse(QEvent.Type.MouseButtonPress, start)
        self.mouse(QEvent.Type.MouseMove, start + QPointF(0, 12))
        self.mouse(QEvent.Type.MouseButtonRelease, start + QPointF(0, 12))
        self.assertEqual(self.ctl.calls, [])

    def test_a_small_wobble_is_still_a_click(self) -> None:
        self.present("ready")
        start = self.blank_spot()
        self.mouse(QEvent.Type.MouseButtonPress, start)
        self.mouse(QEvent.Type.MouseMove, start + QPointF(2, 1))
        self.mouse(QEvent.Type.MouseButtonRelease, start + QPointF(2, 1))
        self.assertEqual(self.ctl.calls, [("open_compose", "card")])


class CardHoverTests(PanelCase):
    def test_outline_ibeam_and_type_chip_hover(self) -> None:
        self.present("ready")
        self.paint_card()
        v = self.panel.view
        self.mouse(QEvent.Type.MouseMove, self.blank_spot())
        self.assertTrue(v._card_hover)
        self.assertEqual(v.cursor().shape(), Qt.CursorShape.IBeamCursor)
        self.assertEqual(v._action_hover["type"].value, 1.0)
        self.mouse(QEvent.Type.MouseMove, v._hits["type"].center())
        self.assertEqual(v.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        QCoreApplication.sendEvent(v, QEvent(QEvent.Type.Leave))
        self.assertFalse(v._card_hover)
        self.assertEqual(v._action_hover["type"].value, 0.0)

    def test_press_shows_on_the_type_chip(self) -> None:
        self.present("ready")
        self.mouse(QEvent.Type.MouseButtonPress, self.blank_spot())
        self.assertEqual(self.panel.view._action_press["type"].value, 1.0)
        self.mouse(QEvent.Type.MouseButtonRelease, self.blank_spot())
        self.assertEqual(self.panel.view._action_press["type"].value, 0.0)

    def test_hover_ends_when_text_arrives(self) -> None:
        self.present("ready")
        self.mouse(QEvent.Type.MouseMove, self.blank_spot())
        self.present("preparing", has_text=True)
        self.assertFalse(self.panel.view._card_hover)

    def test_no_card_hover_with_text(self) -> None:
        self.present("stopped", has_text=True)
        self.mouse(QEvent.Type.MouseMove, self.blank_spot())
        self.assertFalse(self.panel.view._card_hover)

    def test_after_a_clear_only_the_undo_chip_reacts(self) -> None:
        self.present("ready")
        self.panel.set_cleared(True)
        self.paint_card()
        v = self.panel.view
        self.mouse(QEvent.Type.MouseMove, v._hits["undo"].center())
        self.assertEqual(v.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.assertEqual(v._action_hover["undo"].value, 1.0)
        self.assertFalse(v._card_hover)  # no outline: the card itself does nothing
        self.mouse(QEvent.Type.MouseMove, self.blank_spot())
        self.assertEqual(v.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertFalse(v._card_hover)
        self.assertEqual(v._action_hover["undo"].value, 0.0)

    def test_other_cards_work_as_usual_during_the_undo_window(self) -> None:
        # Clear, then Ctrl+` with nothing highlighted: the Nothing-selected card (no Undo chip) takes over for a
        # few seconds while the undo window runs on underneath.
        self.panel.set_cleared(True)
        self.present("nothing_selected", underlying="ready")
        v = self.panel.view
        self.assertEqual(v.messages, P.NOTHING_TEXT["empty"] + [P.TYPE_LINE])
        self.mouse(QEvent.Type.MouseMove, self.blank_spot())
        self.assertTrue(v._card_hover)
        self.click(self.blank_spot())
        self.assertEqual(self.ctl.calls, [("open_compose", "card")])


class CoveredTests(PanelCase):
    def test_covered_panel_is_shown_at_opacity_zero(self) -> None:
        p = self.panel
        p.fade.snap(0.4)
        p.set_covered(True)
        self.assertEqual((p.fade.value, p.slide.value, p.windowOpacity()), (1.0, 0.0, 0.0))
        p._apply_window_motion()
        self.assertEqual(p.windowOpacity(), 0.0)
        p.set_covered(False)
        self.assertEqual(p.windowOpacity(), 1.0)

    def test_covering_never_touches_the_visibility_setting(self) -> None:
        self.settings.set("panel.visible", True)
        changed: list[str] = []
        self.settings.changed.connect(changed.append)
        self.panel.set_covered(True)
        self.panel.set_covered(False)
        self.assertEqual(changed, [])
        self.assertNotIn(("set_panel_visible", False), self.ctl.calls)


class GeometryHelperTests(PanelCase):
    def test_reading_width_drives_the_panel_width(self) -> None:
        p = self.panel
        self.present("ready")
        self.assertEqual(p.panel_width(), p.reading_width(False))
        self.assertGreaterEqual(p.reading_width(True), self.theme.px(P.EXPANDED_W))
        self.assertGreater(p.reading_width(True), p.reading_width(False))

    def test_rest_rect_restores_a_position_first(self) -> None:
        p = self.panel
        self.present("ready")
        p._rest = QPoint()
        rect = p.rest_rect()
        self.assertFalse(rect.topLeft().isNull())
        self.assertEqual(rect.size(), p.size())
        self.assertEqual(rect.topLeft(), p._rest)

    def test_step_text_size(self) -> None:
        self.panel.step_text_size(1)
        self.assertEqual(self.settings.get("text_size"), "large")
        self.panel.step_text_size(-1)
        self.panel.step_text_size(-1)
        self.panel.step_text_size(-1)
        self.assertEqual(self.settings.get("text_size"), "small")


if __name__ == "__main__":
    unittest.main()
