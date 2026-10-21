"""The Save as audio sheet: what to save, with which voice, how long and how big, then the Windows Save dialog.

It covers the panel (440 x 322 at k = 1, grown from the panel's anchored edge) and never pauses reading. Enter
or "Choose where to save…" opens the native Save dialog, owned by the sheet. OK there hands the path and the
first sentence to the App through saveChosen (the App closes the sheet and starts the AudioExporter); Cancel
stays on the sheet; Esc or Back closes it with nothing written. The suggested file name comes from the file's
title, never from the text, and the last folder is remembered for this session only.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRect, QSize, QStandardPaths, Qt, Signal
from PySide6.QtGui import QFontMetricsF, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QDialog, QFileDialog, QWidget

from ..engine import speed_label
from ..export import default_name, export_format, file_label, spoken_words
from .controls import G_SAVE, FadeLabel, LinkText, Segmented, TextButton, text_button_size
from .sheet import Sheet, sheet_chrome

log = logging.getLogger("saylight.export_dialog")

NATURAL = QSize(440, 322)
LABEL_X, VALUE_X, ROW_TOP, PITCH, ROW_H = 22, 132, 46, 42, 40   # DIP
CHOOSE = "Choose where to save…"
PRIVACY = {False: "The text is sent to Microsoft to make the audio, as when reading aloud.",
           True: "Made on this PC. Nothing is sent online."}

_session_folder = ""  # the last folder chosen this session; never saved


@dataclass(frozen=True)
class SaveSetup:
    """What the sheet offers, given by the App when it opens the sheet."""
    doc: object                    # the loaded Document
    title: str = ""                # the file's title (its name without extension); "" for selected or typed text
    from_file: bool = False
    current: int = 0               # the current sentence
    page: int | None = None        # the current sentence's page, when the file has a page map
    voice: str = ""                # voice ID
    voice_name: str = ""           # "Ava"
    private: bool = False          # the voice runs on this PC
    speed: float = 1.0
    has_notes: bool = False        # the document has notes, captions or tables after the main text
    has_refs: bool = False         # ... and a reference list
    read_notes: bool = False       # the current skips, including this file's chips
    read_refs: bool = False


def includes_label(has_notes: bool, has_refs: bool, read_notes: bool, read_refs: bool) -> str:
    """The Includes row: what the save holds, following the current skips."""
    notes, refs = has_notes and read_notes, has_refs and read_refs
    if has_notes and has_refs:
        if notes and refs:
            return "Main text, notes and references"
        if notes:
            return "Main text and notes · references left out"
        if refs:
            return "Main text and references · notes left out"
        return "Main text only · notes and references left out"
    if has_notes:
        return "Main text and notes" if notes else "Main text only · notes left out"
    if has_refs:
        return "Main text and references" if refs else "Main text only · references left out"
    return "All the text"


def save_options(setup: SaveSetup) -> list[tuple[str, int]]:
    """The Save row's choices as (label, first sentence): the whole text, then from the current sentence. Only
    one choice when the current sentence is the first."""
    whole = "Whole document" if setup.from_file else "All the text"
    if setup.current <= 0:
        return [(whole, 0)]
    if setup.from_file and setup.page:
        later = f"From page {setup.page}"
    else:
        later = f"From sentence {setup.current + 1}"
    return [(whole, 0), (later, setup.current)]


def documents_folder() -> str:
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return folder or os.path.expanduser("~")


class _Para(QWidget):
    """A short paragraph in the small role, wrapped to the width (two lines here)."""

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.text = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme.changed.connect(self.update)

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self.setAccessibleName(text)
            self.update()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(t.font("small"))
        p.setPen(t.color("TextSecondary"))
        layout = QTextLayout(self.text, t.font("small"))
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        layout.setTextOption(opt)
        lh = t.line_height("small")
        layout.beginLayout()
        y = 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(self.width())
            line.setPosition(QPointF(0, y))
            y += lh
        layout.endLayout()
        layout.draw(p, QPointF(0, 0))


class _Rule(QWidget):
    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme.changed.connect(self.update)

    def paintEvent(self, _event) -> None:
        # A 1 px hairline through the middle of a 3 DIP strip, so no scale rounds it away.
        p = QPainter(self)
        p.setPen(QPen(self.theme.color("ControlStroke"), 0))
        y = self.height() / 2
        p.drawLine(QPointF(0, y), QPointF(self.width(), y))


class ExportDialog(Sheet):
    """The Save as audio sheet. Call prepare() with what to offer, then the App opens it."""

    saveChosen = Signal(str, int)   # the path the person picked, the first sentence to save

    def __init__(self, ctl):
        super().__init__(ctl, name="Save as audio", glyph=G_SAVE, title="Save as audio", natural=NATURAL)
        t, tips, host = self.theme, self.tips, self.body
        self.setup: SaveSetup | None = None
        self._options: list[tuple[str, int]] = []
        self._dialog: QFileDialog | None = None
        self.labels = [FadeLabel(t, "body", "TextPrimary", host) for _ in range(4)]
        for label, text in zip(self.labels, ("Save", "Voice", "File", "Includes")):
            label.set_text(text, animate=False)
        self.seg = Segmented(t, tips, [("", 14, "")], host, name="What to save", focusable=True)
        self.seg_value = FadeLabel(t, "body", "TextPrimary", host)
        self.voice_value = LinkText(t, tips, host, role="body", color_token="TextPrimary")
        self.voice_value.text_tip = "Uses the current voice and speed."
        self.file_value = FadeLabel(t, "body", "TextPrimary", host)
        self.includes_value = FadeLabel(t, "body", "TextPrimary", host)
        self.rule = _Rule(t, host)
        self.privacy = _Para(t, host)
        self.btn_choose = TextButton(t, tips, G_SAVE, CHOOSE, parent=host, name="Choose where to save the audio file",
                                     tooltip="Pick a folder and file name (Enter)")
        self.seg.selected.connect(lambda _i: self._sync_file())
        self.btn_choose.clicked.connect(self._choose)
        QWidget.setTabOrder(self.seg, self.btn_choose)
        QWidget.setTabOrder(self.btn_choose, self.btn_back)

    # --- Content -------------------------------------------------------------------------------

    def prepare(self, setup: SaveSetup) -> None:
        """Fill the sheet for `setup`; the Save row starts on its first choice."""
        self.setup = setup
        self._options = save_options(setup)
        labels = [label for label, _ in self._options]
        if len(labels) > 1:
            self.seg.items = [(label, 14, label) for label in labels]
            self.seg.index = 0
            self.seg.bar.snap(0.0)
            self.seg.setAccessibleName(f"What to save: {labels[0]}")
        self.seg_value.set_text(labels[0], animate=False)
        self.voice_value.set_parts([("text", f"{setup.voice_name or 'Voice'} · {speed_label(setup.speed)}")],
                                   animate=False)
        self.includes_value.set_text(includes_label(setup.has_notes, setup.has_refs, setup.read_notes,
                                                    setup.read_refs), animate=False)
        self.privacy.set_text(PRIVACY[bool(setup.private)])
        self._sync_file(animate=False)
        self._layout_at(self.width(), self.height())

    def first(self) -> int:
        """The first sentence the chosen option saves."""
        if not self._options:
            return 0
        i = self.seg.index if len(self._options) > 1 else 0
        return self._options[max(0, min(i, len(self._options) - 1))][1]

    def _sync_file(self, animate: bool = True) -> None:
        s = self.setup
        if s is None:
            return
        self.file_value.set_text(file_label(spoken_words(s.doc, self.first()), s.speed, s.voice), animate=animate)

    # --- Layout --------------------------------------------------------------------------------

    def layout_content(self, W: int, H: int) -> None:
        t = self.theme
        px = t.px
        vx, right = px(VALUE_X), W - px(LABEL_X)
        for i, label in enumerate(self.labels):
            top = px(ROW_TOP + i * PITCH)
            label.setGeometry(px(LABEL_X), top + (px(ROW_H) - px(20)) // 2, vx - px(LABEL_X) - px(8), px(20))
        rows = [px(ROW_TOP + i * PITCH) for i in range(4)]
        text_rect = lambda top: QRect(vx, top + (px(ROW_H) - px(20)) // 2, right - vx, px(20))  # noqa: E731
        two = len(self._options) > 1
        self.seg.setVisible(two)
        self.seg_value.setVisible(not two)
        self.seg_value.setGeometry(text_rect(rows[0]))
        if two:
            fm = QFontMetricsF(t.font("body"))
            seg_w = max(fm.horizontalAdvance(label) for label, _ in self._options) + 2 * px(16)
            width = min(right - vx, math.ceil(seg_w) * len(self._options))
            self.seg.place(QRect(vx, rows[0] + (px(ROW_H) - px(32)) // 2, width, px(32)))
        self.voice_value.setGeometry(text_rect(rows[1]))
        self.file_value.setGeometry(text_rect(rows[2]))
        self.includes_value.setGeometry(text_rect(rows[3]))
        self.rule.setGeometry(px(LABEL_X), px(218) - px(3) // 2, W - 2 * px(LABEL_X), px(3))
        self.privacy.setGeometry(px(LABEL_X), px(226), W - 2 * px(LABEL_X), 2 * t.line_height("small"))
        # The hint row would land on the privacy line; a note (rare: activation refused) takes its place.
        self.hint.setGeometry(px(LABEL_X), px(226), W - 2 * px(LABEL_X), px(16))
        size = text_button_size(t, (CHOOSE,))
        foot = sheet_chrome(t, W, H)["footer"]  # this size: the window may not have it yet
        self.btn_choose.place(QRect(px(12), foot.center().y() - size.height() // 2 + 1, size.width(), size.height()))

    def show_note(self, text: str) -> None:
        self.privacy.hide()
        super().show_note(text)

    def end_note(self) -> None:
        super().end_note()
        self.privacy.show()

    def focus_target(self) -> QWidget | None:
        return self.seg if len(self._options) > 1 else self.btn_choose

    def primary(self) -> None:
        self._choose()

    # --- The Save dialog -----------------------------------------------------------------------

    def _choose(self) -> None:
        s = self.setup
        if s is None:
            return
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        ext, label = export_format(s.voice)
        folder = _session_folder if _session_folder and os.path.isdir(_session_folder) else documents_folder()
        name = default_name(s.title if s.from_file else "", ext, folder, _dt.date.today())
        d = QFileDialog(self, "Save as audio", os.path.join(folder, name), f"{label} (*{ext})")
        d.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        d.setFileMode(QFileDialog.FileMode.AnyFile)
        d.setDefaultSuffix(ext[1:])
        d.setWindowModality(Qt.WindowModality.WindowModal)
        d.finished.connect(self._dialog_finished)
        self._dialog = d
        d.open()

    def _dialog_finished(self, result: int) -> None:
        global _session_folder
        d, self._dialog = self._dialog, None
        if d is None:
            return
        files = d.selectedFiles() if result == QDialog.DialogCode.Accepted.value else []
        d.deleteLater()
        if not files or not files[0]:
            self.activate()  # Cancel: back on the sheet
            return
        path = os.path.normpath(files[0])
        _session_folder = os.path.dirname(path)
        self.saveChosen.emit(path, self.first())

    def close_animated(self, reason: str) -> None:
        if self._dialog is not None:
            d, self._dialog = self._dialog, None
            d.finished.disconnect(self._dialog_finished)
            d.reject()
            d.deleteLater()
        super().close_animated(reason)
