"""Tests for ui/pronunciation_dialog.py: the list (A-Z, Find, delete with Undo, the on/off switch, import), the edit
view (from a word or the list, Add / Save / Replace, smart case, Esc), Test through a silent fake preview, and the
"How it will be read" preview. No window is shown and nothing takes the foreground or makes a sound."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtGui import QFont

from saylight.pronunciations import Entry, PronunciationList, PronunciationStore
from saylight.tests.test_controls import FakeTips, setUpModule, theme  # noqa: F401 (setUpModule runs here)
from saylight.tests.test_sheet import FakePanel
from saylight.ui.pronunciation_dialog import (HINT_LIST, HINT_OFF, NOT_SAID, NOTES, PronunciationDialog, case_detail,
                                              count_label, plural_of, whole_detail)

CONTEXT = "so the fMRI data showed that the effect was strongest in the left hemisphere"


class FakeSettings(QObject):
    changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.values = {"pronunciations.enabled": True, "voice": "edge:en-US-AvaNeural", "panel.position": None}

    def get(self, key: str):
        return self.values[key]

    def set(self, key: str, value) -> None:
        self.values[key] = value
        self.changed.emit(key)


class FakePreview(QObject):
    stateChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.state = "idle"
        self.played: list[tuple[str, float, str]] = []

    def play(self, voice: str, speed: float, text: str = "") -> None:
        self.played.append((voice, speed, text))
        self.state = "loading"
        self.stateChanged.emit("loading")

    def stop(self) -> None:
        self.state = "idle"
        self.stateChanged.emit("idle")


class FakeCtl:
    def __init__(self) -> None:
        self.theme = theme()
        self.tips = FakeTips()
        self.settings = FakeSettings()
        self.panel = FakePanel(QSize(440, 252))
        self.preview = FakePreview()

    def speed(self) -> float:
        return 0.95


class DialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="saylight_prondlg_test_")
        self.store = PronunciationStore(os.path.join(self.dir, "pronunciations.json"))
        self.store.import_entries([Entry("fMRI", "F M R I"), Entry("et al.", "and colleagues"), Entry("ibid.", ""),
                                   Entry("GIF", "jif")])
        self.ctl = FakeCtl()
        self.d = PronunciationDialog(self.ctl, self.store)
        size = self.d.natural_size()
        self.d.resize(size)
        self.d._layout_at(size.width(), size.height())
        self.closes: list[str] = []
        self.notes: list[str] = []
        self.d.closeRequested.connect(self.closes.append)
        self.d.saved.connect(self.notes.append)

    def tearDown(self) -> None:
        self.d.deleteLater()
        shutil.rmtree(self.dir, ignore_errors=True)

    def words_shown(self) -> list[str]:
        return [(it.kind, it.entry.match) for it in self.d.rows.items]

    def test_the_list_is_sorted_and_counted(self) -> None:
        self.d.show_list()
        self.assertEqual(self.words_shown(), [("entry", "et al."), ("entry", "fMRI"), ("entry", "GIF"),
                                              ("entry", "ibid.")])
        self.assertEqual(self.d.info.parts, [("text", "4 words")])
        self.assertEqual(self.d.rows.accessibleName(), "Pronunciation list, 4 words")
        self.assertEqual(self.d.window().windowTitle(), "Pronunciations – Saylight")

    def test_find_filters_on_the_word_or_how_it_is_said(self) -> None:
        self.d.show_list()
        self.d.find.setText("COLL")
        self.assertEqual(self.words_shown(), [("entry", "et al.")])
        self.d.find.setText("zebra")
        self.assertEqual(self.words_shown(), [])
        self.assertEqual(self.d.rows._message_layout()[0][0][0], "No words match “zebra”.")
        self.d.reject()  # Esc clears the search before anything else
        self.assertEqual(self.d.find.text(), "")
        self.assertEqual(self.closes, [])
        self.d.reject()
        self.assertEqual(self.closes, ["esc"])

    def test_delete_leaves_an_undo_row_and_undo_puts_it_back(self) -> None:
        self.d.show_list()
        i = next(i for i, e in enumerate(self.store.entries()) if e.match == "GIF")
        self.d.delete_entry(i)
        self.assertEqual(len(self.store), 3)
        self.assertIn(("deleted", "GIF"), self.words_shown())
        self.assertEqual(self.words_shown().index(("deleted", "GIF")), 2)  # where it was
        self.d.undo()
        self.assertEqual(len(self.store), 4)
        self.assertNotIn(("deleted", "GIF"), self.words_shown())
        self.assertEqual(self.store.entries()[i], Entry("GIF", "jif"))

    def test_the_switch_turns_the_whole_list_off(self) -> None:
        self.d.show_list()
        self.assertEqual(self.d._hint, [("text", HINT_LIST)])
        self.d.row_on.set_checked(False, emit=True)
        self.assertFalse(self.ctl.settings.values["pronunciations.enabled"])
        self.assertEqual(self.d._hint, [("text", HINT_OFF)])

    def test_an_empty_list(self) -> None:
        for i in reversed(range(len(self.store))):
            self.store.remove(i)
        self.d.show_list()
        self.assertFalse(self.d.find.isVisibleTo(self.d))
        lines = [text for text, _origin, _role in self.d.rows._message_layout()[0]]
        self.assertEqual(lines[0], "No pronunciations yet.")
        self.assertEqual(self.d.info.parts, [("text", "No words yet")])

    def test_from_a_word_it_opens_on_the_edit_view_and_saving_closes(self) -> None:
        self.d.show_word("hemisphere", CONTEXT)
        self.assertEqual(self.d.view, "edit")
        self.assertEqual(self.d.title.parts, [("text", "Change how a word is said")])
        self.assertEqual(self.d.f_word.text(), "hemisphere")
        self.assertIs(self.d.focus_target(), self.d.f_say)
        self.assertFalse(self.d.btn_arrow.isVisibleTo(self.d))
        self.assertEqual(self.d.hint.content, ("note", NOT_SAID))  # nothing typed yet: not said
        self.d.f_say.setText("HEM is fear")
        self.assertEqual(self.d.btn_primary.label, "Add")
        self.assertIn(("say", "HEM is fear"), self.d.preview_well.parts)
        self.d.primary()
        self.assertEqual(self.store.entries()[-1], Entry("hemisphere", "HEM is fear"))
        self.assertEqual(self.notes, ["Pronunciation added"])
        self.assertEqual(self.closes, ["saved"])

    def test_a_word_already_in_the_list_opens_its_entry(self) -> None:
        self.d.show_word("fMRI", CONTEXT)
        self.assertEqual(self.d.edit.mode, "change")
        self.assertEqual(self.d.f_say.text(), "F M R I")
        self.assertEqual(self.d.f_say.selectedText(), "F M R I")
        self.d.f_say.setText("eff M R I")
        self.assertEqual(self.d.btn_primary.label, "Save")
        self.d.primary()
        self.assertEqual(self.notes, ["Pronunciation saved"])
        self.assertEqual(self.store.entries()[0].say, "eff M R I")

    def test_add_from_the_list_and_replace(self) -> None:
        self.d.show_list()
        self.d.start_add()
        self.assertEqual(self.d.title.parts, [("text", "Add a word")])
        self.assertTrue(self.d.btn_arrow.isVisibleTo(self.d))
        self.assertFalse(self.d.btn_primary.isEnabled())  # no word yet
        self.d.f_word.setText("GIF")
        self.d.f_say.setText("gif")
        self.assertEqual(self.d.btn_primary.label, "Replace")
        self.assertEqual(self.d.hint.content, ("note", "You already have “GIF”. Saving replaces it."))
        self.d.primary()
        self.assertEqual(self.d.view, "list")
        self.assertEqual(len(self.store), 4)
        self.assertEqual(self.closes, [])

    def test_the_plural_is_offered_and_added_on_request(self) -> None:
        self.d.start_add()
        self.d.f_word.setText("EEG")
        self.d.f_say.setText("E E G")
        self.assertTrue(self.d.row_slot.isVisibleTo(self.d))
        self.assertEqual(self.d.row_slot.label, "Also add “EEGs”, said “E E Gs”")
        self.d.row_slot.set_checked(True, emit=True)
        self.d.primary()
        self.assertEqual([e.match for e in self.store.entries()[-2:]], ["EEG", "EEGs"])

    def test_smart_case_until_touched(self) -> None:
        self.d.start_add()
        self.d.f_word.setText("gif")
        self.assertFalse(self.d.row_case.checked)
        self.d.f_word.setText("Gif")
        self.assertTrue(self.d.row_case.checked)  # a capital: case-sensitive, as smart case
        self.assertIsNone(self.d.current_entry().match_case)
        self.d.row_case.set_checked(False, emit=True)  # touched: now explicit
        self.d.f_word.setText("GIFF")
        self.assertFalse(self.d.row_case.checked)
        self.assertIs(self.d.current_entry().match_case, False)

    def test_esc_from_the_edit_view(self) -> None:
        self.d.show_list()
        self.d.start_change(0)
        self.d.reject()
        self.assertEqual(self.d.view, "list")
        self.assertEqual(self.closes, [])
        self.d.show_word("hemisphere", CONTEXT)
        self.d.reject()
        self.assertEqual(self.closes, ["esc"])

    def test_change_mode_can_turn_an_entry_off_and_delete_it(self) -> None:
        self.d.show_list()
        i = next(i for i, e in enumerate(self.store.entries()) if e.match == "et al.")
        self.d.start_change(i)
        self.assertEqual(self.d.title.parts, [("text", "Change “et al.”")])
        self.assertEqual(self.d.row_slot.label, "Use this pronunciation")
        self.d.row_slot.set_checked(False, emit=True)
        self.d.primary()
        self.assertFalse(self.store.entries()[i].enabled)
        self.d.start_change(i)
        self.d.btn_delete.clicked.emit()
        self.assertEqual(self.d.view, "list")
        self.assertIn(("deleted", "et al."), self.words_shown())

    def test_a_light_dismiss_keeps_the_draft_for_the_next_open(self) -> None:
        self.d.start_add()
        self.d.f_word.setText("EEG")
        with mock.patch.object(PronunciationDialog.__mro__[1], "close_animated"):
            self.d.close_animated("away")
        self.d.show_list()
        self.assertEqual((self.d.view, self.d.f_word.text()), ("edit", "EEG"))
        with mock.patch.object(PronunciationDialog.__mro__[1], "close_animated"):
            self.d.close_animated("esc")
        self.d.show_list()
        self.assertEqual(self.d.view, "list")

    def test_test_plays_the_sentence_with_this_pronunciation(self) -> None:
        self.d.show_word("hemisphere", CONTEXT)
        self.d.f_say.setText("HEM is fear")
        self.d.btn_test.clicked.emit()
        voice, speed, text = self.ctl.preview.played[-1]
        self.assertEqual((voice, speed), ("edge:en-US-AvaNeural", 0.95))
        self.assertTrue(text.endswith("in the left HEM is fear"))
        self.assertFalse(self.d.btn_test.isEnabled())  # disabled while the sample is made
        self.ctl.preview.state = "playing"
        self.ctl.preview.stateChanged.emit("playing")
        self.assertEqual(self.d.btn_test.label, "Stop")
        self.d.btn_test.clicked.emit()  # Stop
        self.assertEqual(self.ctl.preview.state, "idle")
        self.assertEqual(self.d.btn_test.label, "Test")

    def test_a_failed_test_says_why(self) -> None:
        self.d.show_word("hemisphere", CONTEXT)
        self.d.f_say.setText("HEM is fear")
        self.d.btn_test.clicked.emit()
        self.ctl.preview.stateChanged.emit("failed")
        self.assertIn(self.d.hint.content, (("note", NOTES["failed"]), ("note", NOTES["offline"])))

    def test_no_match_in_the_sentence(self) -> None:
        self.d.show_word("fMRI", CONTEXT)
        self.d.f_word.setText("fMRIs")
        self.assertEqual(self.d.preview_well.message, "“fMRIs” isn't in this sentence as a whole word.")

    def test_import_adds_new_words_only_and_can_be_undone(self) -> None:
        self.d.show_list()
        other = os.path.join(self.dir, "shared.json")
        PronunciationList.save(other, [Entry("GIF", "gif"), Entry("EEG", "E E G"), Entry("SQL", "sequel")])
        self.d.import_path(other)
        self.assertEqual(self.d.hint.content, ("note", "Added 2 words. Words already in your list were kept."))
        self.assertTrue(self.d.btn_undo.isVisibleTo(self.d))
        self.assertEqual(self.store.entries()[3], Entry("GIF", "jif"))
        self.d.undo()
        self.assertEqual(len(self.store), 4)
        self.assertFalse(self.d.btn_undo.isVisibleTo(self.d))
        with open(other, "w", encoding="utf-8") as f:
            f.write("not a list")
        self.d.import_path(other)
        self.assertEqual(self.d.hint.content, ("note", NOTES["bad_file"]))

    def test_the_preview_is_cut_around_the_word_to_two_rows(self) -> None:
        w = self.d.preview_well
        w.set_content([("text", "word " * 60), ("say", "F M R I"), ("text", " more" * 60)])
        font = QFont(self.ctl.theme.font("reading"))
        text, marks = w.fitted(400, font)
        self.assertTrue(text.startswith("…") and text.endswith("…"))
        self.assertEqual([text[a:b] for a, b, _k in marks], ["F M R I"])
        self.assertLessEqual(w._layout(text, 400, font).lineCount(), 2)


class CopyTests(unittest.TestCase):
    def test_copy(self) -> None:
        self.assertEqual([count_label(n) for n in (0, 1, 12, 2000)], ["No words yet", "1 word", "12 words",
                                                                      "2,000 words"])
        self.assertEqual(case_detail("fMRI", True), "Only “fMRI”, not “fmri” or “FMRI”.")
        self.assertEqual(case_detail("fMRI", False), "“fMRI”, “fmri” and “FMRI” all match.")
        self.assertEqual(whole_detail("RT", True), "“RT” matches “RT.” but not “ART” or “RTs”.")
        self.assertEqual(whole_detail("RT", False), "Also matches inside longer words, such as “RTs”.")
        self.assertEqual([plural_of(w) for w in ("EEG", "fMRI", "RTs", "R", "et al.", "R²")],
                         ["EEGs", "fMRIs", None, None, None, None])


if __name__ == "__main__":
    unittest.main()
