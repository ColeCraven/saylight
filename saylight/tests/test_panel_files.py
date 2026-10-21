"""Tests for the panel's 1.3 parts (ui/panel.py): More in the header, the Open a file and Continue chips, the open
notices and their chips, Opening, the header info for files and its notes, closing a file, drops, the progress
bar's page marks and the top-right chips (Start from the beginning, Back to page N, the nudge to remember a place).

The panel gets the fake controller from test_panel. Its window is created but never shown, and neither is the
flyout: only the rows it would show are checked.
"""

import os
import shutil
import tempfile
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QMouseEvent

from saylight.documents.model import DocSource
from saylight.text_model import Document
from saylight.ui import panel as P
from saylight.ui.controls import G_ERROR, G_LOCK, G_NOTHING, measure_parts
from saylight.ui.flyout import MORE_TIP

from .test_panel import TEXT, PanelCase, info
from .test_panel import setUpModule  # noqa: F401  (the QApplication)

PAPER = " ".join(f"Sentence number {i} is about reading." for i in range(60))


class Recorder:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def __getattr__(self, name: str):
        return lambda *args: self.calls.append((f"engine.{name}", *args))


def file_doc(text: str = TEXT, title: str = "Smith 2024", path: str = r"C:\papers\Smith 2024.pdf") -> Document:
    doc = Document.from_raw(text)
    doc.source = DocSource("file", path, title)
    return doc


class FileCase(PanelCase):
    def show(self, state: str, doc=None, **extra) -> None:
        """Present a state with `doc` loaded (or none), with extra info keys."""
        self.panel.on_session(doc)
        base = info(state, has_text=doc is not None, underlying=extra.pop("underlying", ""))
        if doc is not None:
            base["total"] = len(doc.sentences)
        self.panel.present(state, {**base, **extra})

    def chip_center(self, key: str) -> QPointF:
        img = QImage(self.panel.view.size(), QImage.Format.Format_ARGB32)
        self.panel.view.render(img)
        return self.panel.view._hits[key].center()


class HeaderTests(FileCase):
    def test_three_icons_with_more_in_the_middle(self) -> None:
        self.show("ready")
        p = self.panel
        self.assertEqual((p.btn_expand.x(), p.btn_more.x(), p.btn_hide.x()), (324, 360, 396))
        self.assertEqual(p.btn_more.accessibleName(), "More")
        self.assertEqual(p.btn_more.tooltip_text(), MORE_TIP)
        self.assertEqual(p.btn_more.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertFalse(hasattr(p, "btn_settings"))

    def test_more_is_hidden_on_the_settings_page(self) -> None:
        self.show("ready")
        self.panel.set_page("settings")
        self.assertTrue(self.panel.btn_more.isHidden())

    def test_the_flyout_rows_follow_the_state(self) -> None:
        self.show("ready")
        keys = [i.key for i in self.panel.flyout_items()]
        self.assertEqual(keys, ["open", "save", "-", "pron", "settings"])
        self.assertFalse(self.panel.flyout_items()[1].enabled)  # nothing to save yet
        self.show("reading", file_doc())
        self.assertTrue(self.panel.flyout_items()[1].enabled)
        self.assertEqual(self.panel.flyout_items("fMRI")[0].label, "Change how “fMRI” is said…")

    def test_flyout_choices_reach_the_controller(self) -> None:
        self.show("ready")
        self.panel._on_flyout("open")
        self.panel._on_flyout("settings")
        self.assertEqual(self.ctl.calls, [("choose_file", "flyout")])
        self.assertEqual(self.panel.page, "settings")


class ReadyCardTests(FileCase):
    def test_open_chip_opens_the_open_dialog(self) -> None:
        self.show("ready")
        self.click(self.chip_center("open"))
        self.assertEqual(self.ctl.calls, [("choose_file", "card")])

    def test_open_chip_follows_type(self) -> None:
        self.show("copying")
        self.paint_card()
        self.click(self.panel.view._hits["open"].center())
        self.assertEqual(self.ctl.calls, [])

    def test_continue_chip_names_the_file_and_fits_the_line(self) -> None:
        self.ctl.point = ("Attention Is All You Need for Readers with Dyslexia, a Longitudinal Study", 14)
        self.show("ready")
        v = self.panel.view
        label = v._labels["continue"]
        self.assertTrue(label.startswith("Continue “Attention") and label.endswith("” from page 14"), label)
        self.assertIn("…", label)  # elided in the middle
        width = measure_parts(self.theme, P.CONTINUE_LINE, role="message", labels={"continue": label})
        self.assertLessEqual(width, v.text_width())
        self.click(self.chip_center("continue"))
        self.assertEqual(self.ctl.calls, [("continue_reading", "card")])
        self.assertEqual(self.panel.continue_tip(), f"Open “{self.ctl.point[0]}” and continue from page 14")

    def test_a_short_title_is_kept_whole(self) -> None:
        self.ctl.point = ("Smith 2024", 0)
        self.show("ready")
        self.assertEqual(self.panel.view._labels["continue"], "Continue “Smith 2024” where you stopped")

    def test_a_click_elsewhere_still_opens_type_or_paste(self) -> None:
        self.show("ready")
        self.click(self.blank_spot())
        self.assertEqual(self.ctl.calls, [("open_compose", "card")])


class NoticeTests(FileCase):
    def notice(self, kind: str, title: str = "Smith 2024", **extra) -> None:
        self.panel.present("open_failed", {**info("open_failed", underlying="ready"), "underlying": "ready",
                                           "label": P_LABELS[kind], "kind": kind, "title": title, **extra})

    def test_password_needed(self) -> None:
        self.notice("password")
        v = self.panel.view
        self.assertEqual(v.messages, [[("text", "“Smith 2024” needs a password.")], [("action", "password")]])
        self.assertEqual(self.panel.status.parts, [("text", "Password needed")])
        self.assertEqual(self.panel.indicator.glyph, G_LOCK)
        self.click(self.blank_spot())  # only the chip takes clicks on a notice
        self.click(self.chip_center("password"))
        self.assertEqual(self.ctl.calls, [("open_password", "card")])

    def test_protected_is_all_text_with_the_reading_shortcut(self) -> None:
        self.settings.set("hotkeys.read", "Ctrl+Alt+Shift+`")
        self.notice("protected")
        v = self.panel.view
        self.assertEqual(v._secondary_from, 3)
        self.assertEqual(v.messages[2], [("text", "and press "), ("key", "Ctrl"), ("key", "Alt"), ("key", "Shift"),
                                         ("key", "`"), ("text", ".")])

    def test_older_word_files_get_their_own_words(self) -> None:
        self.panel.notice_ext = ".doc"
        self.notice("unsupported")
        self.assertEqual(self.panel.view.messages[0], [("text", "Saylight can't read older Word files (.doc).")])
        self.panel.notice_ext = ".xyz"
        self.notice("unsupported")
        self.assertEqual(self.panel.view.messages[0], [("text", "Saylight can't read this kind of file.")])
        self.assertEqual(self.panel.indicator.glyph, G_NOTHING)

    def test_io_offers_try_again_and_missing_offers_open(self) -> None:
        self.notice("io")
        self.assertEqual(self.panel.indicator.glyph, G_ERROR)
        self.click(self.chip_center("retry"))
        self.notice("missing")
        self.assertEqual(self.panel.view.messages[-1], [("text", "or "), ("action", "open")])
        self.click(self.chip_center("open"))
        self.assertEqual(self.ctl.calls, [("retry_open",), ("choose_file", "card")])

    def test_a_long_title_is_elided_in_the_middle(self) -> None:
        self.notice("corrupt", title="A " * 80 + "end")
        line = self.panel.view.messages[0]
        self.assertIn("…", line[0][1])
        self.assertLessEqual(measure_parts(self.theme, line, role="message"), self.panel.view.text_width())

    def test_every_kind_has_lines(self) -> None:
        for kind in P_LABELS:
            with self.subTest(kind=kind):
                lines, secondary = P.notice_lines(kind, "T")
                self.assertTrue(lines)
                self.assertIn(secondary, (1, len(lines)))


P_LABELS = {"password": "Password needed", "protected": "Can't open this PDF", "scanned": "No text to read",
            "empty": "No text to read", "unsupported": "Can't read this file", "too_large": "File too large",
            "corrupt": "Can't open this file", "io": "Can't open this file", "missing": "File not found"}


class OpeningTests(FileCase):
    def test_opening_a_file(self) -> None:
        self.show("opening", None, label="Opening…", open_kind="file", title="Smith 2024", open_done=0,
                  open_total=160, play_action="none", can_stop=True, can_type=False, can_clear=False)
        p = self.panel
        self.assertEqual(p.view.messages, [[("text", "Opening “Smith 2024”")], [("text", "To cancel, press Stop.")]])
        self.assertFalse(p.play.isEnabled())
        self.assertEqual(p.play.tooltip_text(), "Opening a file…")
        self.assertEqual(p.btn_stop.tooltip_text(), "Stop opening")
        self.assertFalse(p.btn_type.isEnabled() or p.btn_clear.isEnabled())
        self.assertEqual(p.info.parts, [])  # the page count waits 300 ms
        p._show_page_info()
        p.present("opening", {**p.info_dict, "open_done": 40})
        self.assertEqual(p.info.parts, [("text", "Page 40 of 160")])
        p.present("opening", {**p.info_dict, "open_done": 160})
        self.assertEqual(p.info.parts, [("text", "Preparing the text…")])


class InfoTests(FileCase):
    def test_a_pdf_says_its_page_and_minutes(self) -> None:
        doc = file_doc()
        self.show("reading", doc, source="file", page=14, pages=18)
        self.panel.on_time_left(52 * 60)
        self.assertEqual(self.panel.info.parts, [("text", "Page 14 of 18 · 52 min left")])

    def test_other_files_and_selections_count_sentences(self) -> None:
        self.show("reading", file_doc(), source="file", page=0, pages=0)
        self.panel.on_time_left(52 * 60)
        self.assertEqual(self.panel.info.parts, [("text", "1 / 2 · 52 min left")])
        self.show("reading", Document.from_raw(TEXT))
        self.panel.on_time_left(245)
        self.assertEqual(self.panel.info.parts, [("text", "1 / 2 · 4:05 left")])

    def test_end_of_the_main_text(self) -> None:
        self.show("finished", file_doc(), end_of_main_text=True)
        self.assertEqual(self.panel.info.parts, [("text", "End of the main text")])
        self.show("finished", file_doc())
        self.assertEqual(self.panel.info.parts[0], ("key", "Ctrl"))


class NoteTests(FileCase):
    def test_a_note_shows_then_goes(self) -> None:
        self.show("reading", file_doc(), source="file")
        self.panel.show_note("Audio saved")
        self.assertEqual(self.panel.info.parts, [("text", "Audio saved")])
        self.assertEqual(self.panel._note_timer.interval(), P.NOTE_MS)
        self.panel._end_note()
        self.assertNotEqual(self.panel.info.parts, [("text", "Audio saved")])

    def test_the_pending_speed_wins(self) -> None:
        self.show("reading", file_doc(), source="file")
        self.panel.on_speed(1.1, "1.1× from the next sentence")
        self.panel.show_note("Pronunciation added")
        self.assertEqual(self.panel.info.color_token, "Accent")

    def test_a_note_waits_for_room(self) -> None:
        self.show("reading", file_doc(), source="file")
        self.panel._info_room = lambda: 20.0
        self.panel.show_note("Continuing from page 14")
        self.assertNotEqual(self.panel.info.parts, [("text", "Continuing from page 14")])
        self.assertEqual(self.panel._note_timer.interval(), P.NOTE_WAIT_MS)
        del self.panel._info_room
        self.panel._present_info()
        self.assertEqual(self.panel.info.parts, [("text", "Continuing from page 14")])

    def test_the_dimmed_note_shows_once_ever(self) -> None:
        doc = Document.from_raw("Reading improved (Smith et al., 2019). It lasted.")
        self.assertTrue(P._has_dimmed(doc))
        self.panel.on_session(doc)
        self.assertTrue(self.settings.get("tips.dimmed_note_shown"))
        self.assertEqual(self.panel._note["text"], "Dimmed text isn't read aloud")
        self.panel._end_note()
        self.panel.on_session(Document.from_raw("Again (Jones, 2020). Here."))
        self.assertIsNone(self.panel._note)


class ClosingTests(FileCase):
    def test_clear_tooltip_names_the_file(self) -> None:
        self.show("stopped", file_doc())
        self.assertEqual(self.panel.btn_clear.tooltip_text(), "Close “Smith 2024”. You can undo this for 10 seconds.")
        self.show("reading", file_doc())
        self.assertEqual(self.panel.btn_clear.tooltip_text(),
                         "Stop and close “Smith 2024”. You can undo this for 10 seconds.")

    def test_the_cleared_card_names_the_file(self) -> None:
        self.show("ready")
        self.panel.set_cleared(True, "Smith 2024")
        self.assertEqual(self.panel.view.messages, [[("text", "Closed “Smith 2024”. "), ("action", "undo")]])
        self.panel.set_cleared(False)
        self.panel.set_cleared(True)
        self.assertEqual(self.panel.view.messages, P.CLEARED_HINT)


class DropTests(FileCase):
    def setUp(self) -> None:
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="saylight_drop_test_")
        self.addCleanup(shutil.rmtree, self.files, True)

    def make(self, name: str) -> str:
        path = os.path.join(self.files, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        return path

    def drag(self, paths: list[str]) -> QDragEnterEvent:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        event = QDragEnterEvent(QPoint(20, 80), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(self.panel, event)
        self._mime = mime
        return event

    def test_drop_kinds(self) -> None:
        pdf, doc, other = self.make("a.pdf"), self.make("b.doc"), self.make("c.xyz")
        self.assertEqual(P.drop_kind([pdf]), "ok")
        self.assertEqual(P.drop_kind([pdf, pdf]), "many")
        self.assertEqual(P.drop_kind([self.files]), "folder")
        self.assertEqual(P.drop_kind([doc]), "ext:.doc")
        self.assertEqual(P.drop_kind([other]), "ext:.xyz")

    def test_a_readable_file_says_what_a_drop_does(self) -> None:
        self.show("reading", file_doc())
        self.drag([self.make("Attention.pdf")])
        v = self.panel.view
        self.assertEqual(v.messages[0], [("glyph", P.G_DOCUMENT), ("text", " Drop to read “Attention”")])
        self.assertEqual(v.messages[1], [("text", "This replaces what you're reading now.")])
        self.assertTrue(self.panel.drop_outline.on)
        drop = QDropEvent(QPointF(20, 80), Qt.DropAction.CopyAction, self._mime, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(self.panel, drop)
        self.assertEqual(self.ctl.calls, [("open_path", os.path.join(self.files, "Attention.pdf"), "drop")])
        self.assertFalse(self.panel.drop_outline.on)
        self.assertIsNone(self.panel.view.messages)  # the reading is back

    def test_what_cannot_be_dropped(self) -> None:
        self.show("ready")
        for paths, line in (([self.make("b.doc")], "Saylight can't read older Word files (.doc)."),
                            ([self.make("c.xyz")], "Saylight can't read .xyz files."),
                            ([self.files], "Drop a file, not a folder."),
                            ([self.make("d.pdf"), self.make("e.pdf")], "Drop one file at a time.")):
            with self.subTest(line=line):
                self.drag(paths)
                self.assertEqual(self.panel.view.messages, [[("text", line)]])
                self.assertFalse(self.panel.drop_outline.on)
                QCoreApplication.sendEvent(self.panel, QEvent(QEvent.Type.DragLeave))
                self.assertEqual(len(self.panel.view.messages), 3)  # the Ready card again

    def test_from_the_settings_page_and_back(self) -> None:
        self.show("ready")
        self.panel.set_page("settings")
        self.drag([self.make("a.pdf")])
        self.assertEqual(self.panel.page, "reading")
        QCoreApplication.sendEvent(self.panel, QEvent(QEvent.Type.DragLeave))
        self.assertEqual(self.panel.page, "settings")

    def test_nothing_while_copying(self) -> None:
        self.show("copying")
        event = self.drag([self.make("a.pdf")])
        self.assertFalse(event.isAccepted())
        self.assertEqual(self.panel.view.messages[1], P.READY_ACTIONS)


class ProgressTests(FileCase):
    def test_page_marks_measure_the_spoken_span(self) -> None:
        doc = Document.from_raw(PAPER)
        n = len(doc.sentences)
        doc.page_starts = [0, doc.sentences[n // 2].start, len(doc.text) + 10]
        starts, marks = P.progress_marks(doc)
        self.assertEqual(len(starts), n)
        self.assertEqual(len(marks), 2)
        self.assertAlmostEqual(marks[1], starts[n // 2])
        self.assertEqual(P.progress_marks(Document.from_raw(TEXT))[1], None)

    def test_the_tooltip_names_the_page(self) -> None:
        doc = file_doc(PAPER)
        doc.page_of = lambda i: 3
        self.show("reading", doc)
        self.assertTrue(self.panel._sentence_label(2).startswith("Page 3 · Sentence 3 of "))


class CornerChipTests(FileCase):
    def setUp(self) -> None:
        super().setUp()
        self.ctl.engine = Recorder(self.ctl.calls)

    def test_continuing_offers_start_from_the_beginning(self) -> None:
        doc = file_doc(PAPER)
        self.show("reading", doc, sentence=30)
        self.panel.on_resumed(30, True, 14)
        self.assertEqual(self.panel._note["text"], "Continuing from page 14")
        self.assertEqual(self.panel.view._corner[:2], ("start_over", "Start from the beginning"))
        self.panel._corner_clicked("start_over")
        self.assertEqual(self.panel.view._corner[:2], ("back", "Back to page 14"))
        self.panel._corner_clicked("back")
        self.assertEqual(self.ctl.calls, [("engine.start_over",), ("engine.jump_to", 30)])
        self.assertIsNone(self.panel.view._corner)

    def test_near_and_without_pages(self) -> None:
        self.show("reading", file_doc(PAPER), sentence=30)
        self.panel.on_resumed(30, False, 14)
        self.assertEqual(self.panel._note["text"], "Continuing near page 14")
        self.panel.on_resumed(30, True, 0)
        self.assertEqual(self.panel._note["text"], "Continuing where you stopped")

    def test_prev_next_and_stop_end_it(self) -> None:
        self.show("reading", file_doc(PAPER), sentence=30)
        self.panel.on_resumed(30, True, 14)
        self.panel.btn_next.clicked.emit()
        self.assertIsNone(self.panel.view._corner)
        self.panel.on_resumed(30, True, 14)
        self.show("stopped", self.panel.doc, sentence=31)
        self.assertNotEqual((self.panel.view._corner or ("",))[0], "start_over")

    def test_the_nudge_to_remember_a_place(self) -> None:
        doc = file_doc(PAPER)
        self.show("reading", doc, sentence=2)
        self.show("stopped", doc, sentence=2)
        self.assertEqual(self.panel.view._corner[:2], ("nudge", "Remember this page next time"))
        self.assertEqual(self.settings.get("resume.offer_count"), 1)
        self.panel._corner_clicked("nudge")
        self.assertEqual(self.ctl.calls, [("remember_place",)])
        self.assertEqual(self.panel.view._corner[:3], ("remembered", "Saylight will remember your place", False))
        self.panel._corner_expired()
        self.show("reading", doc, sentence=3)
        self.show("stopped", doc, sentence=3)  # once per file a session
        self.assertIsNone(self.panel.view._corner)

    def test_no_nudge_near_the_end_with_resume_on_or_after_three(self) -> None:
        doc = file_doc(PAPER)
        n = len(doc.sentences)
        self.show("reading", doc, sentence=n - 5)
        self.show("stopped", doc, sentence=n - 5)
        self.assertIsNone(self.panel.view._corner)
        self.settings.set("resume.offer_count", 3)
        other = file_doc(PAPER, path=r"C:\papers\other.pdf")
        self.show("reading", other, sentence=1)
        self.show("stopped", other, sentence=1)
        self.assertIsNone(self.panel.view._corner)


class RightClickTests(FileCase):
    def test_a_right_click_on_text_never_seeks_or_drags(self) -> None:
        self.panel.open_flyout = lambda mode, pos=None, word=None: self.ctl.calls.append(("flyout", mode, word))
        self.show("stopped", Document.from_raw(TEXT))
        pos = QPointF(30, 20)
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            QCoreApplication.sendEvent(self.panel.view, QMouseEvent(
                kind, pos, pos, pos + QPointF(1000, 1000), Qt.MouseButton.RightButton,
                Qt.MouseButton.RightButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier))
        self.assertEqual(len(self.ctl.calls), 1)
        self.assertEqual(self.ctl.calls[0][:2], ("flyout", "pointer"))


if __name__ == "__main__":
    unittest.main()
