"""Tests for ui/export_dialog.py: the Save row's choices, the Includes and privacy lines, the File estimate following
the choice, and the Save dialog's suggested name, filter and result. The native dialog is never shown."""

import os
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from unittest import mock

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QDialog, QFileDialog

from saylight.tests.test_controls import FakeTips, setUpModule, theme  # noqa: F401 (setUpModule runs here)
from saylight.tests.test_sheet import FakeCtlSettings, FakePanel
from saylight.ui import export_dialog as E
from saylight.ui.export_dialog import ExportDialog, SaveSetup, includes_label, save_options


@dataclass
class Sentence:
    index: int
    text: str

    @property
    def say(self) -> str:
        return self.text


@dataclass
class Doc:
    sentences: list


def doc(n: int = 400) -> Doc:
    return Doc([Sentence(i, "one two three four five six seven eight nine ten") for i in range(n)])


class FakeCtl:
    def __init__(self) -> None:
        self.theme = theme()
        self.tips = FakeTips()
        self.settings = FakeCtlSettings()
        self.panel = FakePanel(QSize(440, 252))


PAPER = dict(title="Smith 2024", from_file=True, current=180, page=14, voice="edge:en-US-AvaNeural",
             voice_name="Ava", speed=0.95, has_notes=True, has_refs=True)


class ChoiceTests(unittest.TestCase):
    def test_the_save_row(self) -> None:
        d = doc()
        self.assertEqual(save_options(SaveSetup(d, **PAPER)), [("Whole document", 0), ("From page 14", 180)])
        self.assertEqual(save_options(SaveSetup(d, **dict(PAPER, page=None))),
                         [("Whole document", 0), ("From sentence 181", 180)])
        self.assertEqual(save_options(SaveSetup(d, current=4)), [("All the text", 0), ("From sentence 5", 4)])
        self.assertEqual(save_options(SaveSetup(d, **dict(PAPER, current=0))), [("Whole document", 0)])

    def test_the_includes_row_follows_the_skips(self) -> None:
        self.assertEqual(includes_label(True, True, False, False), "Main text only · notes and references left out")
        self.assertEqual(includes_label(True, True, True, False), "Main text and notes · references left out")
        self.assertEqual(includes_label(True, True, False, True), "Main text and references · notes left out")
        self.assertEqual(includes_label(True, True, True, True), "Main text, notes and references")
        self.assertEqual(includes_label(False, True, False, False), "Main text only · references left out")
        self.assertEqual(includes_label(False, False, True, True), "All the text")


class SheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_exportdlg_test_")
        self.d = ExportDialog(FakeCtl())
        size = self.d.natural_size()
        self.d.resize(size)
        self.chosen: list[tuple[str, int]] = []
        self.d.saveChosen.connect(lambda path, first: self.chosen.append((path, first)))

    def tearDown(self) -> None:
        self.d.deleteLater()
        shutil.rmtree(self.dir, ignore_errors=True)
        E._session_folder = ""

    def test_rows_and_the_file_estimate(self) -> None:
        self.d.prepare(SaveSetup(doc(), **PAPER))
        self.assertEqual(self.d.window().windowTitle(), "Save as audio – Saylight")
        self.assertEqual(self.d.natural_size(), QSize(440, 322))
        self.assertTrue(self.d.seg.isVisibleTo(self.d))
        self.assertEqual(self.d.seg.accessibleName(), "What to save: Whole document")
        self.assertEqual(self.d.voice_value.parts, [("text", "Ava · 0.95×")])
        self.assertEqual(self.d.file_value.parts, [("text", "MP3 · about 30 min · about 11 MB")])
        self.assertEqual(self.d.privacy.text, E.PRIVACY[False])
        self.d.seg.set_index(1)
        self.d.seg.selected.emit(1)
        self.assertEqual(self.d.first(), 180)
        self.assertEqual(self.d.file_value.parts, [("text", "MP3 · about 16 min · about 6 MB")])
        self.assertIs(self.d.focus_target(), self.d.seg)

    def test_one_choice_is_plain_text(self) -> None:
        self.d.prepare(SaveSetup(doc(), **dict(PAPER, current=0, voice="windows:Zira", private=True)))
        self.assertFalse(self.d.seg.isVisibleTo(self.d))
        self.assertEqual(self.d.seg_value.parts, [("text", "Whole document")])
        self.assertEqual(self.d.privacy.text, E.PRIVACY[True])
        self.assertTrue(self.d.file_value.parts[0][1].startswith("WAV · "))
        self.assertIs(self.d.focus_target(), self.d.btn_choose)

    def _choose(self, setup: SaveSetup, picked: str | None):
        self.d.prepare(setup)
        with mock.patch.object(QFileDialog, "open") as opened, \
                mock.patch.object(E, "documents_folder", return_value=self.dir):
            self.d.primary()  # Enter
            opened.assert_called_once()
            dialog = self.d._dialog
            seen = (dialog.windowTitle(), dialog.nameFilters(), dialog.defaultSuffix(), dialog.directory().path(),
                    dialog.acceptMode())
            with mock.patch.object(QFileDialog, "selectedFiles", return_value=[picked] if picked else []):
                result = QDialog.DialogCode.Accepted if picked else QDialog.DialogCode.Rejected
                with mock.patch.object(self.d, "activate"):
                    self.d._dialog_finished(result.value)
        return seen

    def test_the_save_dialog_suggests_the_title_and_hands_back_the_path(self) -> None:
        target = os.path.join(self.dir, "Smith 2024.mp3")
        title, filters, suffix, folder, mode = self._choose(SaveSetup(doc(), **PAPER), target)
        self.assertEqual((title, filters, suffix), ("Save as audio", ["MP3 audio (*.mp3)"], "mp3"))
        self.assertEqual(os.path.normcase(os.path.normpath(folder)), os.path.normcase(os.path.normpath(self.dir)))
        self.assertEqual(mode, QFileDialog.AcceptMode.AcceptSave)
        self.assertEqual(self.chosen, [(target, 0)])
        self.assertEqual(E._session_folder, self.dir)  # remembered for this session only

    def test_cancel_stays_on_the_sheet(self) -> None:
        self._choose(SaveSetup(doc(), **PAPER), None)
        self.assertEqual(self.chosen, [])
        self.assertIsNone(self.d._dialog)

    def test_selected_text_never_names_the_file(self) -> None:
        self.d.prepare(SaveSetup(doc(), current=4, voice="windows:Zira", voice_name="Zira", private=True))
        made = []

        class Spy(QFileDialog):
            def __init__(self, *args):
                super().__init__(*args)
                made.append(args)

        with mock.patch.object(E, "QFileDialog", Spy), mock.patch.object(QFileDialog, "open"), \
                mock.patch.object(E, "documents_folder", return_value=self.dir):
            self.d.primary()
            name = os.path.basename(made[0][2])  # (owner, caption, suggested path, filter)
            self.assertRegex(name, r"^Saylight audio \d{4}-\d{2}-\d{2}\.wav$")
            self.assertEqual(self.d._dialog.nameFilters(), ["WAV audio (*.wav)"])
            self.d.close_animated("esc")
            self.assertIsNone(self.d._dialog)


if __name__ == "__main__":
    unittest.main()
