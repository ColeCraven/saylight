"""Tests for ui/password_sheet.py: laid exactly over the panel, the field never keeps the password, Enter and the
pill submit it once, a wrong one is selected for retyping, and show/hide. No window is shown.
"""

import unittest

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit

from saylight.tests.test_controls import render, setUpModule  # noqa: F401 (setUpModule runs here)
from saylight.tests.test_sheet import FakeCtl
from saylight.ui import password_sheet as PS
from saylight.ui.sheet import sheet_rect


class PasswordSheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctl = FakeCtl()
        self.sheet = PS.PasswordSheet(self.ctl)
        self.addCleanup(self.sheet.deleteLater)
        self.sent: list[str] = []
        self.sheet.submitted.connect(self.sent.append)

    def test_it_takes_exactly_the_panel_rect(self) -> None:
        rest = QRect(740, 768, 440, 252)
        self.assertEqual(self.sheet.mode, "panel")
        self.assertEqual(sheet_rect(rest, QRect(0, 0, 1920, 1032), QSize(640, 468), rest.size(), mode="panel"), rest)
        self.assertEqual(self.sheet.windowTitle(), "PDF password – Saylight")

    def test_layout_follows_the_panel(self) -> None:
        s = self.sheet
        s._layout_at(440, 252)
        self.assertEqual(s.well.geometry(), QRect(12, 44, 416, 128))
        self.assertEqual((s.well.message_top + 44, s.field.geometry().y()), (61, 97))
        self.assertEqual(s.field.geometry(), QRect(26, 97, 388, 34))
        self.assertEqual(s.pill.geometry().adjusted(3, 3, -3, -3).topLeft().toTuple(), (12, 200))  # Play's twin
        self.assertEqual(s.hint.geometry().y(), 180)
        s._layout_at(640, 360)  # the larger view: the same block in the 44-280 well
        self.assertEqual(s.well.geometry(), QRect(12, 44, 616, 236))
        self.assertEqual(s.pill.geometry().adjusted(3, 3, -3, -3).y(), 308)
        render(s)

    def test_the_field(self) -> None:
        f = self.sheet.field
        self.assertEqual(f.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(f.maxLength(), 256)
        self.assertEqual(f.accessibleName(), "PDF password")
        self.assertEqual(f.placeholderText(), "Password")
        self.assertEqual(f.contextMenuPolicy(), Qt.ContextMenuPolicy.NoContextMenu)
        self.assertFalse(f.dragEnabled())

    def test_enter_and_the_pill_submit_once(self) -> None:
        s = self.sheet
        self.assertFalse(s.pill.isEnabled())
        s.primary()
        self.assertEqual(self.sent, [])
        s.field.setText("hunter2")
        self.assertTrue(s.pill.isEnabled())
        s.primary()
        s.pill.clicked.emit()  # still checking: no second open
        self.assertEqual(self.sent, ["hunter2"])
        self.assertFalse(s.pill.isEnabled())
        self.assertEqual(s.hint.content, ("hint", PS.CHECKING))

    def test_a_wrong_password_is_selected_for_retyping(self) -> None:
        s = self.sheet
        s.field.setText("wrong")
        s.primary()
        s.wrong_password()
        self.assertEqual(s.hint.content, ("note", PS.WRONG))
        self.assertEqual(s.field.selectedText(), "wrong")
        self.assertTrue(s.pill.isEnabled())

    def test_closing_always_clears_the_password(self) -> None:
        s = self.sheet
        s.field.setText("secret")
        s._toggle_reveal()
        self.assertEqual(s.field.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertEqual(s.btn_reveal.accessibleName(), "Hide password")
        s.close_animated("esc")
        self.assertEqual(s.field.text(), "")
        self.assertEqual(s.field.echoMode(), QLineEdit.EchoMode.Password)

    def test_closing_leaves_nothing_to_undo(self) -> None:
        # Typing builds undo history; Show password and Ctrl+Z on the next file must not bring it back.
        s = self.sheet
        QTest.keyClicks(s.field, "hunter2")
        self.assertEqual(s.field.text(), "hunter2")
        s.close_animated("esc")
        self.assertEqual(s.field.text(), "")
        s.prepare("Another file")
        s._toggle_reveal()
        s.field.undo()
        self.assertEqual(s.field.text(), "")
        self.assertFalse(s.field.isUndoAvailable())

    def test_closing_after_a_wrong_password_leaves_nothing_to_undo(self) -> None:
        s = self.sheet
        QTest.keyClicks(s.field, "wrong-one")
        s.primary()
        s.wrong_password()
        s.close_animated("esc")
        s._toggle_reveal()
        s.field.undo()
        self.assertEqual(s.field.text(), "")

    def test_the_message_names_the_file(self) -> None:
        self.assertEqual(PS.message("Smith 2024"), "“Smith 2024” needs a password.")
        self.assertEqual(PS.message(""), "This PDF needs a password.")
        self.sheet.prepare("A" * 300)
        self.sheet._layout_at(440, 252)
        render(self.sheet)  # a long title is elided in the middle, never past the well

    def test_tab_order(self) -> None:
        s = self.sheet
        self.assertIs(s.field.nextInFocusChain(), s.btn_reveal)
        self.assertIs(s.btn_reveal.nextInFocusChain(), s.pill)
        self.assertIs(s.focus_target(), s.field)


if __name__ == "__main__":
    unittest.main()
