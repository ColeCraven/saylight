"""The Pronunciations sheet: the list of words Saylight says another way, and the view that adds or changes one.

One focusable sheet over the panel (640 x 468 at k = 1) with two views that crossfade: the list (a switch for the
whole list, Find, Add a word, the words A-Z, Import and Export) and the edit view (Word, Say it as, "How it will be
read", Test, and the matching switches). From a word in the reading box it opens straight in the edit view, and
saving closes the sheet so reading carries on with the new pronunciation.

The App wires it: it pauses reading as the sheet opens, closes it on closeRequested, rebuilds the reading when the
store changes, and shows `saved`'s note in the panel header. Test plays through the shared VoicePreview, whose
files stay in the run's temporary folder. Nothing here logs a word.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QKeySequence, QPainter, QPainterPath, QPen, QShortcut,
                           QTextCharFormat, QTextLayout, QTextOption)
from PySide6.QtNetwork import QNetworkInformation
from PySide6.QtWidgets import QDialog, QFileDialog, QGraphicsOpacityEffect, QWidget

from ..pronunciations import (EXPORT_NAME, FILE_FILTER, MAX_ENTRIES, MAX_MATCH, MAX_SAY, Entry, PronunciationStore,
                              preview_parts, preview_spoken, read_list)
from ..voices.base import parse_voice_id
from .compose import ThinScrollBar
from .controls import (G_ARROW, G_BACK, G_BUSY, G_CHECK, G_DELETE, G_EXPORT, G_FIND, G_IMPORT, G_PLUS, G_SPEECH,
                       G_TEST, G_UNDO, ActionState, FadeLabel, IconButton, Part, TextButton, ToggleRow, draw_glyph,
                       paint_focus_ring, paint_parts, paint_stop_square, text_button_size)
from .motion import MEDIUM, Tween, decelerate, with_alpha
from .sheet import LineField, Sheet, sheet_chrome

log = logging.getLogger("saylight.pronunciation_dialog")

NATURAL = QSize(640, 468)
ROW_H = 40             # DIP: a word in the list
UNDO_MS = 10_000       # a deleted row offers Undo this long (held while the pointer is over it)
NOTE_MS = 10_000       # list notes replace the hint this long
FLASH_HOLD_MS, FLASH_FADE_MS = 1000, 500
KEEP_DRAFT = frozenset({"away", "tray", "switch", "screen"})  # closes that keep an unsaved edit for next time

TITLE = "Pronunciations"
HINT_LIST = "Right-click a word in the reading box to add it here."
HINT_OFF = "Pronunciations are off, so words are read as written."
NOT_SAID = "Saylight won't say this word. It stays on screen, dimmed."
FULL_TIP = f"Your list is full ({MAX_ENTRIES:,} words). Delete some to add more."
TEST_TIP = "Play the sentence with this pronunciation. Nothing is saved."
NOTES = {"offline": "Testing needs the internet with this voice.",
         "failed": "The voice didn't answer. Try again.",
         "bad_file": "That file isn't a Saylight pronunciation list.",
         "write": "Couldn't save the file. Try another folder."}


# --- Copy ----------------------------------------------------------------------------------------

def count_label(n: int) -> str:
    return "No words yet" if n == 0 else "1 word" if n == 1 else f"{n:,} words"


def words(n: int) -> str:
    return "1 word" if n == 1 else f"{n:,} words"


def cut(word: str, n: int = 16) -> str:
    """`word` cut at `n` characters with "…", for copy that quotes it."""
    return word if len(word) <= n else word[:n - 1].rstrip() + "…"


def case_detail(word: str, on: bool) -> str:
    w = cut(word or "fMRI")
    others = [v for v in dict.fromkeys((w.lower(), w.upper(), w.capitalize())) if v != w][:2]
    if not others:  # nothing to tell apart ("42")
        return "Letters are matched as typed." if on else "Capitals don't matter."
    listed = " or ".join(f"“{v}”" for v in others)
    if on:
        return f"Only “{w}”, not {listed}."
    return f"“{w}”, {' and '.join(f'“{v}”' for v in others)} all match." if len(others) > 1 else \
        f"“{w}” and “{others[0]}” both match."


def whole_detail(word: str, on: bool) -> str:
    w = cut(word or "RT")
    a = "A" if w[:1].isupper() else "a"
    if on:
        return f"“{w}” matches “{w}.” but not “{a}{w}” or “{w}s”."
    return f"Also matches inside longer words, such as “{w}s”."


def plural_of(word: str) -> str | None:
    """The plural the Add view offers: for one word of 2 or more letters that doesn't end in "s"."""
    if len(word) < 2 or " " in word or not word[-1].isalpha() or word[-1] in "sS":
        return None
    return word + "s"


def row_name(e: Entry) -> str:
    said = f"said as {e.say}" if e.say else "not said"
    return f"{e.match}, {said}{', off' if not e.enabled else ''}"


def _offline() -> bool:
    info = QNetworkInformation.instance()
    return info is not None and info.reachability() == QNetworkInformation.Reachability.Disconnected


def reading_font(theme) -> QFont:
    """The reader's own font, at most 18 DIP, for words and previews."""
    font = QFont(theme.font("reading"))
    size, cap = font.pixelSize(), theme.px(18)
    if size > cap:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, font.letterSpacing() * cap / size)
        font.setPixelSize(cap)
    font.setBold(False)
    font.setItalic(False)
    font.setUnderline(False)
    return font


def _documents() -> str:
    return (QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            or os.path.expanduser("~"))


# --- Parts ---------------------------------------------------------------------------------------

class _FindField(LineField):
    """The Find field: a magnifier at +10 and the text at +34. Down moves into the list."""

    toList = Signal()

    def apply_theme(self) -> None:
        super().apply_theme()
        t = self.theme
        self.setTextMargins(t.px(34), 0, t.px(10), 0)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()  # Esc clears the search before anything else
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self.toList.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = t.px(16)
        _fill, _border, _focused, _text, placeholder = self.colors()
        draw_glyph(p, QRectF(t.px(10), (self.height() - g) / 2, g, g), G_FIND, t.font("icon16"), placeholder)


class _TestButton(TextButton):
    """Test ⇄ Stop, with a turning ring while the sample is made (the busy glyph under reduced motion)."""

    RING, STOP = "\x00ring", "\x00stop"

    def __init__(self, theme, tips, parent=None):
        super().__init__(theme, tips, G_TEST, "Test", parent=parent, tooltip=TEST_TIP,
                         name="Test this pronunciation")
        self._spin = QTimer(self)
        self._spin.setInterval(16)
        self._spin.timeout.connect(self.update)
        self._t0 = 0.0

    def set_state(self, state: str) -> None:
        """"idle", "loading" or "playing"."""
        if state == "loading":
            glyph = G_BUSY if self.theme.reduced_motion else self.RING
            self.set_content(glyph, "Test", "Test this pronunciation")
            if glyph == self.RING and not self._spin.isActive():
                self._t0 = _now()
                self._spin.start()
        elif state == "playing":
            self.set_content(self.STOP, "Stop", "Stop the test")
        else:
            self.set_content(G_TEST, "Test", "Test this pronunciation")
        if state != "loading":
            self._spin.stop()

    def hideEvent(self, event) -> None:
        self._spin.stop()
        super().hideEvent(event)

    def _paint_content(self, p: QPainter, r: QRectF, glyph: str, label: str, color: QColor) -> None:
        t = self.theme
        if glyph not in (self.RING, self.STOP):
            super()._paint_content(p, r, glyph, label, color)
            return
        box = QRectF(r.left() + t.px(12), r.center().y() - t.px(8), t.px(16), t.px(16))
        if glyph == self.STOP:
            paint_stop_square(t)(p, box, color)
        else:
            s = _now() - self._t0
            pen = QPen(color, t.k * 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            rot = (s / 1.2) * 360.0
            sweep = 30 + 240 * (0.5 - 0.5 * math.cos(2 * math.pi * s / 1.6))
            p.drawArc(box.adjusted(t.k * 2, t.k * 2, -t.k * 2, -t.k * 2), int(-rot * 16), int(-sweep * 16))
        super()._paint_content(p, r, "", label, color)


def _now() -> float:
    return time.monotonic()


@dataclass
class _Item:
    kind: str           # "entry" | "deleted"
    entry: Entry
    index: int          # the entry's index in the store ("deleted": where it was)
    height: float = 1.0  # 0-1 of a row: a deleted row closes up


class _Rows(QWidget):
    """The words, A-Z, in the reading well. Hover or keyboard selection draws a band plate with Test and Delete;
    a click opens the word; a deleted word leaves "“word” deleted. [Undo]" in its place for 10 s."""

    editRequested = Signal(int)     # store index
    testRequested = Signal(int)
    deleteRequested = Signal(int)
    undoRequested = Signal()
    addRequested = Signal()
    undoClosed = Signal()           # the deleted row has closed up: its Undo is over

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.tips = tips
        self.items: list[_Item] = []
        self.query = ""
        self.empty = True            # the store has no words at all
        self.sheet_w = 0             # the sheet's width: the columns are set in window coordinates
        self.hover_i: int | None = None
        self.sel_i: int | None = None
        self._ring = False
        self._undo_hit = QRectF()
        self._flash: dict[str, Tween] = {}
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMouseTracking(True)
        self.bar = ThinScrollBar(theme, self)
        self.bar.valueChanged.connect(lambda _v: (self._place_actions(), self.update()))
        self.btn_test = IconButton(theme, tips, G_TEST, size=32, parent=self, name="Test",
                                   tooltip="Play this pronunciation. Nothing is saved.")
        self.btn_delete = IconButton(theme, tips, G_DELETE, size=32, parent=self, name="Delete",
                                     tooltip="Delete this pronunciation. You can undo this.")
        self.btn_add = TextButton(theme, tips, G_PLUS, "Add a word", style="chip", parent=self,
                                  tooltip="Add a word (Ctrl+N)")
        self.btn_test.clicked.connect(lambda: self._act(self.testRequested))
        self.btn_delete.clicked.connect(lambda: self._act(self.deleteRequested))
        self.btn_add.clicked.connect(self.addRequested.emit)
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.timeout.connect(self._close_deleted)
        self._close_up = Tween(self, theme, 1.0, MEDIUM, decelerate())
        self._close_up.on_change = self._on_close_up
        theme.changed.connect(self.relayout)
        self.relayout()

    # --- Content -------------------------------------------------------------------------------

    def set_items(self, items: list[_Item], *, query: str, empty: bool) -> None:
        key = self._key_at(self.sel_i)
        self.items, self.query, self.empty = items, query, empty
        self.sel_i = self._index_of_key(key) if key is not None else None
        self.hover_i = None
        self.relayout()

    def _key_at(self, i: int | None) -> str | None:
        return self.items[i].entry.key if i is not None and 0 <= i < len(self.items) else None

    def _index_of_key(self, key: str) -> int | None:
        return next((i for i, it in enumerate(self.items) if it.kind == "entry" and it.entry.key == key), None)

    def flash(self, key: str) -> None:
        """A new or changed word: a band plate that holds 1 s, then fades over 500 ms."""
        tw = self._flash.get(key) or Tween(self, self.theme, 0.0, FLASH_FADE_MS)
        self._flash[key] = tw
        tw.snap(1.0)
        i = self._index_of_key(key)
        if i is not None:
            self.ensure_visible(i)
        hold = FLASH_HOLD_MS if not self.theme.reduced_motion else FLASH_HOLD_MS + FLASH_FADE_MS
        QTimer.singleShot(hold, self, lambda: tw.to(0.0) if tw.value > 0 else None)

    def selected_entry(self) -> _Item | None:
        if self.sel_i is not None and 0 <= self.sel_i < len(self.items):
            return self.items[self.sel_i]
        return None

    # --- Geometry ------------------------------------------------------------------------------

    def row_h(self) -> int:
        return self.theme.px(ROW_H)

    def _tops(self) -> list[float]:
        tops, y = [], float(self.theme.px(4))
        for it in self.items:
            tops.append(y)
            y += self.row_h() * it.height
        tops.append(y)
        return tops

    def content_h(self) -> float:
        return self._tops()[-1] + self.theme.px(4)

    def row_rect(self, i: int) -> QRectF:
        tops = self._tops()
        t = self.theme
        return QRectF(t.px(4), tops[i] - self.bar.value(), self.width() - t.px(4) - t.px(10),
                      self.row_h() * self.items[i].height)

    def _x(self, x: int) -> int:
        """A window x in this widget's coordinates (it sits 1 px inside the well at 12)."""
        return x - self.theme.px(12) - 1

    def columns(self) -> tuple[int, int, int]:
        """(word x, arrow x, said-as x): 26, col - 22 and col in the window, col = 26 + 0.42 (W - 52)."""
        t = self.theme
        W = self.sheet_w or self.width() + 2 * t.px(12)
        col = t.px(26) + round(0.42 * (W - t.px(52)))
        return self._x(t.px(26)), self._x(col - t.px(22)), self._x(col)

    def relayout(self) -> None:
        t = self.theme
        self.bar.setGeometry(self.width() - t.px(6) - t.px(2), t.px(2), t.px(6), max(0, self.height() - t.px(4)))
        self.bar.setRange(0, max(0, math.ceil(self.content_h() - self.height())))
        self.bar.setPageStep(self.height())
        self.bar.setSingleStep(self.row_h())
        message = self._message_layout()
        chip = message[2] if message is not None else None
        self.btn_add.setVisible(chip is not None)
        if chip is not None:
            self.btn_add.place(chip)
        self._place_actions()
        self._sync_name()
        self.update()

    def resizeEvent(self, event) -> None:
        self.relayout()
        super().resizeEvent(event)

    def _active(self) -> int | None:
        """The row whose Test and Delete show: the hovered one, else the selected one."""
        i = self.hover_i if self.hover_i is not None else self.sel_i
        if i is None or not (0 <= i < len(self.items)) or self.items[i].kind != "entry":
            return None
        return i

    def _place_actions(self) -> None:
        i = self._active()
        if i is None:
            self.btn_test.hide()
            self.btn_delete.hide()
            return
        t = self.theme
        r = self.row_rect(i)
        W = self.sheet_w or self.width() + 2 * t.px(12)
        y = round(r.center().y()) - t.px(16)
        self.btn_test.move(self._x(W - t.px(88)), y)
        self.btn_delete.move(self._x(W - t.px(52)), y)
        self.btn_test.setVisible(bool(self.items[i].entry.say) and 0 <= r.center().y() <= self.height())
        self.btn_delete.setVisible(0 <= r.center().y() <= self.height())

    def ensure_visible(self, i: int) -> None:
        tops = self._tops()
        top, bottom = tops[i], tops[i + 1]
        v = self.bar.value()
        if top - self.theme.px(4) < v:
            self.bar.setValue(int(top - self.theme.px(4)))
        elif bottom + self.theme.px(4) > v + self.height():
            self.bar.setValue(math.ceil(bottom + self.theme.px(4) - self.height()))

    def _sync_name(self) -> None:
        n = sum(1 for it in self.items if it.kind == "entry")
        self.setAccessibleName(f"Pronunciation list, {words(n)}")
        it = self.selected_entry()
        self.setAccessibleDescription(row_name(it.entry) if it and it.kind == "entry" else "")

    # --- Deleted rows --------------------------------------------------------------------------

    def start_undo_window(self) -> None:
        self._close_up.snap(1.0)
        self._undo_timer.start(UNDO_MS)

    def _close_deleted(self) -> None:
        i = next((i for i, it in enumerate(self.items) if it.kind == "deleted"), None)
        if i is None:
            return
        if self.hover_i == i:  # the pointer holds it
            self._undo_timer.start(1000)
            return
        self._close_up.snap(1.0)
        self._close_up.to(0.0)

    def _on_close_up(self, v: float) -> None:
        for it in self.items:
            if it.kind == "deleted":
                it.height = v
        if v <= 0.0:
            self.items = [it for it in self.items if it.kind != "deleted"]
            self.undoClosed.emit()
        self.relayout()

    def has_deleted(self) -> bool:
        return any(it.kind == "deleted" for it in self.items)

    # --- Input ---------------------------------------------------------------------------------

    def _row_at(self, pos: QPointF) -> int | None:
        for i in range(len(self.items)):
            if self.row_rect(i).contains(pos):
                return i
        return None

    def _act(self, signal) -> None:
        i = self._active()
        if i is not None:
            signal.emit(self.items[i].index)

    def mouseMoveEvent(self, event) -> None:
        i = self._row_at(event.position())
        if i != self.hover_i:
            self.hover_i = i
            self._place_actions()
            self.update()
        over_undo = self._undo_hit.contains(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if over_undo else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if not (self.btn_test.underMouse() or self.btn_delete.underMouse()):
            self.hover_i = None
            self._place_actions()
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._undo_hit.contains(event.position()):
            self.undoRequested.emit()
            return
        i = self._row_at(event.position())
        if i is not None and self.items[i].kind == "entry":
            self.sel_i = i
            self.editRequested.emit(self.items[i].index)

    def wheelEvent(self, event) -> None:
        self.bar.setValue(self.bar.value() - event.angleDelta().y() // 120 * self.row_h())
        event.accept()

    def focusInEvent(self, event) -> None:
        self._ring = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        if self.sel_i is None:
            self._select(self._step(-1, 1))
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._ring = False
        self.update()
        super().focusOutEvent(event)

    def _step(self, start: int, d: int) -> int | None:
        i = start + d
        while 0 <= i < len(self.items):
            if self.items[i].kind == "entry":
                return i
            i += d
        return None

    def _select(self, i: int | None) -> None:
        if i is None:
            return
        self.sel_i = i
        self.ensure_visible(i)
        self._place_actions()
        self._sync_name()
        self.update()

    def keyPressEvent(self, event) -> None:
        k = event.key()
        cur = self.sel_i if self.sel_i is not None else -1
        if k == Qt.Key.Key_Down:
            self._select(self._step(cur, 1))
        elif k == Qt.Key.Key_Up:
            self._select(self._step(cur if cur >= 0 else len(self.items), -1))
        elif k == Qt.Key.Key_Home:
            self._select(self._step(-1, 1))
        elif k == Qt.Key.Key_End:
            self._select(self._step(len(self.items), -1))
        elif k in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
            n = max(1, self.height() // self.row_h())
            target = max(0, min(len(self.items) - 1, cur + (n if k == Qt.Key.Key_PageDown else -n)))
            self._select(self._step(target - 1, 1) if self.items else None)
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.selected_entry():
            self.editRequested.emit(self.selected_entry().index)
        elif k == Qt.Key.Key_Delete and self.selected_entry():
            self.deleteRequested.emit(self.selected_entry().index)
        elif k == Qt.Key.Key_Space and self.selected_entry() and self.selected_entry().entry.say:
            self.testRequested.emit(self.selected_entry().index)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # --- Painting ------------------------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        t = self.theme
        c = t.reading_colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._undo_hit = QRectF()
        message = self._message_layout()
        if message is not None:
            self._paint_message(p, c, message)
            return
        font = reading_font(t)
        fm = QFontMetricsF(font)
        x_word, x_arrow, x_say = self.columns()
        active = self._active()
        p.setClipRect(QRectF(0, 0, self.width(), self.height()))
        for i, it in enumerate(self.items):
            r = self.row_rect(i)
            if r.bottom() < 0 or r.top() > self.height():
                continue
            if it.kind == "deleted":
                self._paint_deleted(p, r, it, c)
                continue
            band = 1.0 if i in (self.hover_i, self.sel_i) else 0.0
            tw = self._flash.get(it.entry.key)
            band = max(band, tw.value if tw else 0.0)
            if band > 0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(c["band"], band) if not t.high_contrast else QColor(0, 0, 0, 0))
                p.drawRoundedRect(r, t.px(5), t.px(5))
                if t.high_contrast and band > 0.5:
                    p.setPen(QPen(t.color("HoverOutline"), 0))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), t.px(5), t.px(5))
            if self._ring and self.hasFocus() and i == self.sel_i:
                paint_focus_ring(p, t, r, t.px(5))
            e = it.entry
            on = e.enabled
            ink = c["text"] if on else c["secondary"]
            baseline = r.top() + (r.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
            right = (self.btn_test.x() - t.px(8)) if i == active else self.width() - t.px(16)
            p.setFont(font)
            p.setPen(ink)
            p.drawText(QPointF(x_word, baseline),
                       fm.elidedText(e.match, Qt.TextElideMode.ElideRight, x_arrow - x_word - t.px(8)))
            g = t.px(12)
            draw_glyph(p, QRectF(x_arrow, r.center().y() - g / 2, g, g), G_ARROW, t.font("icon12"), c["secondary"])
            if e.say:
                said, said_ink = e.say, ink
            else:
                said, said_ink = "Not said", c["skipped"] if on else c["secondary"]
            if not on:
                said += " (off)"
            p.setFont(font)
            p.setPen(said_ink)
            p.drawText(QPointF(x_say, baseline),
                       fm.elidedText(said, Qt.TextElideMode.ElideRight, max(0.0, right - x_say)))

    def _paint_deleted(self, p: QPainter, r: QRectF, it: _Item, c: dict) -> None:
        t = self.theme
        if r.height() < 2:
            return
        p.save()
        p.setClipRect(r)
        p.setOpacity(min(1.0, it.height))
        parts: list[Part] = [("text", f"“{cut(it.entry.match, 24)}” deleted. "), ("action", "undo")]
        hits: dict[str, QRectF] = {}
        x_word = self.columns()[0]
        paint_parts(p, t, x_word, r.top(), self.row_h(), parts, c["text"], role="body",
                    caps=(c["keycap_fill"], c["keycap_stroke"], c["keycap_text"]),
                    actions={"undo": ActionState(hover=1.0 if self.hover_i is not None and
                                                 self.items[self.hover_i] is it else 0.0)}, hits=hits)
        self._undo_hit = hits.get("undo", QRectF())
        p.restore()

    def _message_layout(self):
        """The empty or no-match message: (lines as (text, baseline origin, colour role), font, the Add chip's
        rect or None), or None while there are rows."""
        if any(it.kind == "entry" for it in self.items) or self.has_deleted():
            return None
        t = self.theme
        font = t.font("message")
        fm = QFontMetricsF(font)
        lh = t.line_height("message")

        def base(top: float) -> float:
            return top + (lh - (fm.ascent() + fm.descent())) / 2 + fm.ascent()

        if not self.empty:
            line = f"No words match “{cut(self.query, 32)}”."
            top = (self.height() - lh) / 2
            origin = QPointF((self.width() - fm.horizontalAdvance(line)) / 2, base(top))
            return [(line, origin, "secondary")], font, None
        first, second, tail = ("No pronunciations yet.", "If Saylight says a word wrong, right-click it",
                               "in the reading box, or ")
        add = text_button_size(t, ("Add a word",))
        top = (self.height() - 3 * lh) / 2
        lines = [(first, QPointF((self.width() - fm.horizontalAdvance(first)) / 2, base(top)), "text"),
                 (second, QPointF((self.width() - fm.horizontalAdvance(second)) / 2, base(top + lh)), "secondary")]
        w = fm.horizontalAdvance(tail) + t.px(4) + add.width()
        x = (self.width() - w) / 2
        lines.append((tail, QPointF(x, base(top + 2 * lh)), "secondary"))
        chip = QRect(round(x + fm.horizontalAdvance(tail) + t.px(4)), round(top + 2 * lh + (lh - add.height()) / 2),
                     add.width(), add.height())
        return lines, font, chip

    def _paint_message(self, p: QPainter, c: dict, message) -> None:
        lines, font, _chip = message
        p.setFont(font)
        for text, origin, role in lines:
            p.setPen(c[role])
            p.drawText(origin, text)


class _Well(QWidget):
    """The reading-tinted well: background, the 1 px edge, radius 6."""

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        theme.changed.connect(self.update)

    def paintEvent(self, _event) -> None:
        t = self.theme
        c = t.reading_colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(c["background"])
        p.setPen(QPen(t.color("TextPrimary") if t.high_contrast else c["edge"], 0))
        p.drawRoundedRect(r, t.px(6), t.px(6))


class _Preview(_Well):
    """ "How it will be read": the sentence as the voice will get it, the replacement on the word pill's colours, a
    word that isn't said dimmed. At most two rows, cut with "…" around the word."""

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(theme, parent)
        self.parts: list[tuple[str, str]] = []
        self.message = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_content(self, parts: list[tuple[str, str]], message: str = "") -> None:
        if (parts, message) != (self.parts, self.message):
            self.parts, self.message = parts, message
            self.setAccessibleName("How it will be read: " + (message or "".join(s for _, s in parts)))
            self.update()

    def _pitch(self, font: QFont) -> int:
        """The reading line pitch, scaled to the capped preview size."""
        full = self.theme.font("reading").pixelSize()
        return round(self.theme.line_height("reading") * font.pixelSize() / max(1, full))

    def _layout(self, text: str, width: float, font: QFont) -> QTextLayout:
        layout = QTextLayout(text, font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        layout.setTextOption(opt)
        lh = self._pitch(font)
        layout.beginLayout()
        y = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(0, y))
            y += lh
        layout.endLayout()
        return layout

    def fitted(self, width: float, font: QFont, rows: int = 2) -> tuple[str, list[tuple[int, int, str]]]:
        """The text to show and its marked ranges (start, end, kind), cut with "…" around the first mark, a word
        at a time, until it fits in `rows`."""
        text, marks = "", []
        for kind, s in self.parts:
            if kind != "text":
                marks.append((len(text), len(text) + len(s), kind))
            text += s
        if not marks or self._layout(text, width, font).lineCount() <= rows:
            return text, marks
        a, b, kind = marks[0]
        head, tail = text[:a].split(" "), text[b:].split(" ")
        for k in range(max(len(head), len(tail)), 0, -1):
            h, tl = " ".join(head[-k:]), " ".join(tail[:k])
            pre = "…" if len(h) < a else ""
            post = "…" if len(tl) < len(text) - b else ""
            piece = pre + h + text[a:b] + tl + post
            lo, hi = a - len(h), b + len(tl)
            shift = len(pre) - lo
            if self._layout(piece, width, font).lineCount() <= rows:
                return piece, [(s + shift, e + shift, kd) for s, e, kd in marks if s >= lo and e <= hi]
        return text[a:b], [(0, b - a, kind)]

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        t = self.theme
        c = t.reading_colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        pad_x, width = t.px(14), self.width() - 2 * t.px(14)
        if self.message:
            font = t.font("body")
            fm = QFontMetricsF(font)
            p.setFont(font)
            p.setPen(c["secondary"])
            p.drawText(QPointF(pad_x, (self.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()),
                       fm.elidedText(self.message, Qt.TextElideMode.ElideRight, width))
            return
        font = reading_font(t)
        text, marks = self.fitted(width, font)
        layout = self._layout(text, width, font)
        formats = []
        for a, b, kind in marks:
            f = QTextCharFormat()
            f.setForeground(c["highlight_text"] if kind == "say" else c["skipped"])
            r = QTextLayout.FormatRange()
            r.start, r.length, r.format = a, b - a, f
            formats.append(r)
        n = layout.lineCount()
        lh = self._pitch(font)
        top = (self.height() - lh * n) / 2
        fm = QFontMetricsF(font)
        for a, b, kind in marks:  # pills behind the replacements, line by line
            if kind != "say":
                continue
            for j in range(n):
                line = layout.lineAt(j)
                s, e = max(a, line.textStart()), min(b, line.textStart() + line.textLength())
                if s >= e:
                    continue
                x0, x1 = line.cursorToX(s)[0], line.cursorToX(e)[0]
                cy = top + line.y() + line.ascent() - fm.ascent() + (fm.ascent() + fm.descent()) / 2
                h = fm.ascent() + fm.descent() + t.px(2)
                path = QPainterPath()
                path.addRoundedRect(QRectF(pad_x + x0 - t.px(3), cy - h / 2, x1 - x0 + t.px(6), h), t.px(4), t.px(4))
                p.fillPath(path, c["highlight"])
        p.setPen(c["text"])
        layout.draw(p, QPointF(pad_x, top), formats)


# --- The sheet -----------------------------------------------------------------------------------

@dataclass
class _Edit:
    mode: str = "add"                  # "add" | "change"
    index: int | None = None           # the entry being changed
    source: str = "list"               # "list" | "word"
    word: str = ""                     # the word it was opened for (from the reading box)
    context: str | None = None         # that word's sentence, as the voice gets it without the list
    case_touched: bool = False


class PronunciationDialog(Sheet):
    """The Pronunciations sheet. Call show_list(), show_add() or show_word() before the App opens it."""

    saved = Signal(str)   # "Pronunciation added" | "Pronunciation saved": the header note, when opened from a word

    def __init__(self, ctl, store: PronunciationStore, preview=None):
        super().__init__(ctl, name="Pronunciations", glyph=G_SPEECH, title=TITLE, natural=NATURAL)
        t, tips = self.theme, self.tips
        self.store = store
        self.preview = preview if preview is not None else getattr(ctl, "preview", None)
        self.settings = ctl.settings
        self.view = "list"
        self.edit = _Edit()
        self._size = QSize()
        self._keep_draft = False
        self._testing = False
        self._test_state = "idle"
        self._deleted: tuple[int, Entry] | None = None
        self._imported: list[Entry] = []
        self._dialog: QFileDialog | None = None

        # List view
        self.list_view = QWidget(self.body)
        lv = self.list_view
        self.row_on = ToggleRow(t, tips, "Use these pronunciations", lv, focusable=True,
                                tooltip="Turn the whole list on or off. Your words are kept either way.")
        self.find = _FindField(t, lv, style="control", name="Find a word", placeholder="Find a word")
        self.btn_add = TextButton(t, tips, G_PLUS, "Add a word", parent=lv, tooltip="Add a word (Ctrl+N)")
        self.well = _Well(t, lv)
        self.rows = _Rows(t, tips, self.well)
        self.btn_import = TextButton(t, tips, G_IMPORT, "Import…", parent=lv,
                                     tooltip="Add words from a Saylight pronunciation file")
        self.btn_export = TextButton(t, tips, G_EXPORT, "Export…", parent=lv,
                                     tooltip="Save your list as a file to share or back up")
        self.btn_undo = TextButton(t, tips, G_UNDO, "Undo", parent=lv, name="Undo the import",
                                   tooltip="Take out the words you just added (Ctrl+Z)")
        self.btn_undo.hide()

        # Edit view
        self.edit_view = QWidget(self.body)
        ev = self.edit_view
        self.btn_arrow = IconButton(t, tips, G_BACK, size=32, small_glyph=True, parent=self, focusable=True,
                                    name="Back to the list", tooltip="Back to the list (Esc)")
        self.lbl_word = FadeLabel(t, "body", "TextPrimary", ev)
        self.lbl_word.set_text("Word", animate=False)
        self.lbl_say = FadeLabel(t, "body", "TextPrimary", ev)
        self.lbl_say.set_text("Say it as", animate=False)
        self.f_word = LineField(t, ev, style="well", name="Word")
        self.f_word.setMaxLength(MAX_MATCH)
        self.f_say = LineField(t, ev, style="well", name="Say it as")
        self.f_say.setMaxLength(MAX_SAY)
        self.btn_test = _TestButton(t, tips, ev)
        self.lbl_preview = FadeLabel(t, "small", "TextSecondary", ev)
        self.lbl_preview.set_text("How it will be read", animate=False)
        self.preview_well = _Preview(t, ev)
        self.row_case = ToggleRow(t, tips, "Match capitals", ev, detail=case_detail("", True), focusable=True)
        self.row_whole = ToggleRow(t, tips, "Whole word only", ev, detail=whole_detail("", True), focusable=True)
        self.row_slot = ToggleRow(t, tips, "Use this pronunciation", ev, focusable=True)
        self.btn_primary = TextButton(t, tips, G_CHECK, "Add", parent=ev, tooltip="Save this pronunciation (Enter)")
        self.btn_delete = TextButton(t, tips, G_DELETE, "Delete", parent=ev,
                                     tooltip="Delete this pronunciation. You can undo this.")
        self.edit_view.hide()
        self.btn_arrow.hide()

        self._fades = {w: QGraphicsOpacityEffect(w) for w in (self.list_view, self.edit_view)}
        for w, effect in self._fades.items():
            effect.setEnabled(False)
            w.setGraphicsEffect(effect)
        self._swap = Tween(self, t, 1.0, MEDIUM, decelerate())
        self._swap.on_change = self._on_swap
        self._leaving: QWidget | None = None
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self._end_list_note)

        chain = [self.row_on, self.find, self.btn_add, self.rows, self.btn_import, self.btn_export, self.btn_undo,
                 self.f_word, self.f_say, self.btn_test, self.row_case, self.row_whole, self.row_slot,
                 self.btn_primary, self.btn_delete, self.btn_arrow, self.btn_back]
        for a, b in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, b)

        self.row_on.toggled.connect(self._set_enabled)
        self.find.textChanged.connect(lambda _s: self.refresh())
        self.btn_add.clicked.connect(self.start_add)
        self.rows.addRequested.connect(self.start_add)
        self.rows.editRequested.connect(self.start_change)
        self.rows.deleteRequested.connect(self.delete_entry)
        self.rows.testRequested.connect(self._test_entry)
        self.rows.undoRequested.connect(self.undo)
        self.rows.undoClosed.connect(self._undo_closed)
        self.find.toList.connect(self._find_to_list)
        self.btn_import.clicked.connect(self._import)
        self.btn_export.clicked.connect(self._export)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_arrow.clicked.connect(self.back_to_list)
        self.f_word.textChanged.connect(lambda _s: self._sync_edit())
        self.f_say.textChanged.connect(lambda _s: self._sync_edit())
        self.btn_test.clicked.connect(self._test)
        self.row_case.toggled.connect(self._case_toggled)
        self.row_whole.toggled.connect(lambda _on: self._sync_edit())
        self.row_slot.toggled.connect(lambda _on: self._sync_edit())
        self.btn_primary.clicked.connect(self.primary)
        self.btn_delete.clicked.connect(lambda: self.delete_entry(self.edit.index))
        store.changed.connect(self.refresh)
        if self.preview is not None and hasattr(self.preview, "stateChanged"):
            self.preview.stateChanged.connect(self._on_preview_state)
        if hasattr(self.settings, "changed"):
            self.settings.changed.connect(lambda key: self._sync_on() if key == "pronunciations.enabled" else None)
        for keys, action in ((QKeySequence.StandardKey.Find, self._focus_find), ("Ctrl+N", self.start_add),
                             (QKeySequence.StandardKey.Undo, self.undo)):
            sc = QShortcut(QKeySequence(keys), self.list_view)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(action)
        self._sync_on()
        self.refresh()
        self._enter_list(animate=False)

    # --- Opening (the App calls one of these, then open_at) -------------------------------------

    def show_list(self) -> None:
        """Open on the list, or on an edit the last light dismiss left unsaved."""
        if self._keep_draft and self.view == "edit":
            return
        self._keep_draft = False
        self.find.clear()
        self._enter_list(animate=False)

    def show_add(self) -> None:
        self._keep_draft = False
        self._start_edit(_Edit("add", None, "list"), "", "")

    def show_word(self, word: str, context: str | None = None) -> None:
        """Open straight on the edit view for a word from the reading box. `context` is its sentence as the voice
        gets it, without the list (pronunciations.base_spoken), for the preview."""
        word = " ".join(word.split())[:MAX_MATCH]
        if self._keep_draft and self.view == "edit" and self.edit.source == "word" and self.edit.word == word:
            self._keep_draft = False
            return
        self._keep_draft = False
        i = self.store.find_word(word)
        if i is not None:
            e = self.store.entries()[i]
            self._start_edit(_Edit("change", i, "word", word, context), e.match, e.say, e)
        else:
            self._start_edit(_Edit("add", None, "word", word, context), word, "")

    def close_animated(self, reason: str) -> None:
        self._keep_draft = reason in KEEP_DRAFT and self.view == "edit"
        self._stop_test()
        self._close_file_dialog()
        super().close_animated(reason)

    # --- Sheet hooks ---------------------------------------------------------------------------

    def focus_target(self) -> QWidget | None:
        if self.view == "list":
            return self.find if self.find.isVisible() or self.store.entries() else self.btn_add
        if self.edit.mode == "add" and self.edit.source == "list":
            return self.f_word
        return self.f_say

    def primary(self) -> None:
        if self.view == "list":
            item = self.rows.selected_entry() if self.rows.hasFocus() else None
            if item is None and self.find.hasFocus():  # Enter in Find opens the first match
                item = next((it for it in self.rows.items if it.kind == "entry"), None)
            if item is not None and item.kind == "entry":
                self.start_change(item.index)
            return
        if self.btn_primary.isEnabled():
            self._save()

    def reject(self) -> None:
        if self.view == "edit" and self.edit.source == "list":
            self.back_to_list()
            return
        if self.view == "list" and self.find.text():
            self.find.clear()
            return
        super().reject()

    def layout_content(self, W: int, H: int) -> None:
        t = self.theme
        px = t.px
        for v in (self.list_view, self.edit_view):
            v.setGeometry(0, 0, W, H)
        # List view
        self.row_on.place(QRect(px(12), px(44), W - px(24), px(40)))
        add = text_button_size(t, ("Add a word",))
        self.btn_add.place(QRect(W - px(12) - add.width(), px(91), add.width(), add.height()))
        self.find.setGeometry(px(12), px(90), W - px(12) - add.width() - px(8) - px(12), px(34))
        well_bottom = H - px(80)
        self.well.setGeometry(px(12), px(132), W - px(24), well_bottom - px(132))
        self.rows.sheet_w = W
        self.rows.setGeometry(1, 1, self.well.width() - 2, self.well.height() - 2)
        self._size = QSize(W, H)  # the window may not have this size yet
        foot = sheet_chrome(t, W, H)["footer"]
        y = foot.center().y() - px(16) + 1
        imp, exp = text_button_size(t, ("Import…",)), text_button_size(t, ("Export…",))
        self.btn_import.place(QRect(px(12), y, imp.width(), imp.height()))
        self.btn_export.place(QRect(px(12) + imp.width() + px(8), y, exp.width(), exp.height()))
        undo = text_button_size(t, ("Undo",))
        self.btn_undo.place(QRect(W - px(12) - undo.width(), y, undo.width(), undo.height()))
        # Edit view
        test = text_button_size(t, ("Test", "Stop"))
        self.lbl_word.setGeometry(px(22), px(48) + px(11), px(104), px(20))
        self.f_word.setGeometry(px(132), px(52), W - px(22) - px(132), px(34))
        self.lbl_say.setGeometry(px(22), px(90) + px(11), px(104), px(20))
        self.f_say.setGeometry(px(132), px(94), W - px(104) - px(132), px(34))
        self.btn_test.place(QRect(W - px(96), px(95), test.width(), test.height()))
        self.lbl_preview.setGeometry(px(22), px(140), W - px(44), px(16))
        self.preview_well.setGeometry(px(12), px(160), W - px(24), px(74))
        self.row_case.place(QRect(px(12), px(242), W - px(24), self.row_case.row_height()))
        self.row_whole.place(QRect(px(12), px(296), W - px(24), self.row_whole.row_height()))
        self.row_slot.place(QRect(px(12), px(350), W - px(24), self.row_slot.row_height()))
        prim = text_button_size(t, ("Add", "Save", "Replace"))
        self.btn_primary.place(QRect(px(12), y, prim.width(), prim.height()))
        dele = text_button_size(t, ("Delete",))
        self.btn_delete.place(QRect(W - px(12) - dele.width(), y, dele.width(), dele.height()))
        self.btn_arrow.place(QRect(px(14), px(8), px(32), px(32)))
        self._place_header()

    def _place_header(self) -> None:
        """The edit view from the list has ← at 14 and its title at 50 (the settings-page idiom)."""
        t = self.theme
        arrow = self.view == "edit" and self.edit.source == "list"
        self.btn_arrow.setVisible(arrow)
        self.glyph.setVisible(not arrow)
        size = self._size if self._size.isValid() else self.size()
        lay = sheet_chrome(t, size.width(), size.height(), self.info.content_width() if self.info.parts else 0.0)
        title = lay["title"]
        if arrow:
            title.setLeft(t.px(50))
        self.title.setGeometry(title)

    def _set_info(self, text: str) -> None:
        self.set_info(text)
        self._place_header()

    # --- The list ------------------------------------------------------------------------------

    def refresh(self) -> None:
        entries = self.store.entries()
        q = self.find.text().strip().casefold()
        shown = [(i, e) for i, e in enumerate(entries)
                 if not q or q in e.match.casefold() or q in e.say.casefold()]
        shown.sort(key=lambda ie: (ie[1].match.casefold(), ie[1].match))
        items = [_Item("entry", e, i) for i, e in shown]
        if self._deleted is not None:
            i, e = self._deleted
            old = next((it for it in self.rows.items if it.kind == "deleted"), None)
            at = sum(1 for it in items if (it.entry.match.casefold(), it.entry.match) < (e.match.casefold(), e.match))
            items.insert(at, _Item("deleted", e, i, old.height if old else 1.0))
        self.rows.set_items(items, query=self.find.text().strip(), empty=not entries)
        self.find.setVisible(bool(entries) or bool(q))
        if self.view == "list":
            self._set_info(count_label(len(entries)))
        if self.view == "edit":
            self._sync_edit()

    def _sync_on(self) -> None:
        on = bool(self.settings.get("pronunciations.enabled"))
        self.row_on.set_checked(on)
        if self.view == "list":
            self.set_hint([("text", HINT_LIST if on else HINT_OFF)])

    def _set_enabled(self, on: bool) -> None:
        self.settings.set("pronunciations.enabled", bool(on))
        self._sync_on()

    def _focus_find(self) -> None:
        if self.view == "list" and self.find.isVisible():
            self.find.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.find.selectAll()

    def list_note(self, text: str, *, undo: bool = False) -> None:
        """A note in place of the hint for 10 s; with `undo`, the footer offers Undo as long."""
        self.show_note(text)
        self.btn_undo.setVisible(undo)
        self._note_timer.start(NOTE_MS)

    def _end_list_note(self) -> None:
        self.end_note()
        self.btn_undo.hide()
        self._imported = []

    def delete_entry(self, index: int | None) -> None:
        if index is None or not (0 <= index < len(self.store)):
            return
        self._stop_test()
        if self._deleted is not None:
            self._deleted = None  # one Undo at a time: the older one is final
            self.rows.items = [it for it in self.rows.items if it.kind != "deleted"]
        e = self.store.entries()[index]
        self._deleted = (index, e)
        self.store.remove(index)  # refresh() puts the "deleted" row in its place
        self.rows.start_undo_window()
        if self.view == "edit":
            self._enter_list()

    def undo(self) -> None:
        if self.view != "list":
            return
        if self._deleted is not None:
            i, e = self._deleted
            self._deleted = None
            self.store.insert(i, e)
            self.rows.flash(e.key)
            return
        if self._imported:
            self.store.remove_entries(self._imported)
            self._imported = []
            self._note_timer.stop()
            self._end_list_note()

    def _undo_closed(self) -> None:
        self._deleted = None

    def _find_to_list(self) -> None:
        if self.rows.items:
            self.rows.setFocus(Qt.FocusReason.TabFocusReason)

    # --- Import and export ---------------------------------------------------------------------

    def _file_dialog(self, save: bool) -> QFileDialog:
        d = QFileDialog(self, "Export pronunciations" if save else "Import pronunciations",
                        os.path.join(_documents(), EXPORT_NAME) if save else _documents(), FILE_FILTER)
        if save:
            d.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            d.setFileMode(QFileDialog.FileMode.AnyFile)
            d.setDefaultSuffix("json")
        else:
            d.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
            d.setFileMode(QFileDialog.FileMode.ExistingFile)
        d.setWindowModality(Qt.WindowModality.WindowModal)
        return d

    def _run_dialog(self, save: bool, done) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            return
        d = self._file_dialog(save)

        def finished(result: int) -> None:
            if self._dialog is not d:
                return
            self._dialog = None
            files = d.selectedFiles() if result == QDialog.DialogCode.Accepted.value else []
            d.deleteLater()
            self.activate()
            if files and files[0]:
                done(os.path.normpath(files[0]))
        d.finished.connect(finished)
        self._dialog = d
        d.open()

    def _close_file_dialog(self) -> None:
        d, self._dialog = self._dialog, None
        if d is not None:
            d.reject()
            d.deleteLater()

    def _import(self) -> None:
        self._run_dialog(False, self.import_path)

    def _export(self) -> None:
        self._run_dialog(True, self.export_path)

    def import_path(self, path: str) -> None:
        try:
            incoming = read_list(path)
        except (OSError, ValueError):
            self.list_note(NOTES["bad_file"])
            return
        added, full = self.store.import_entries(incoming)
        self._imported = added
        if full:
            self.list_note(f"Your list is full, so {words(full)} weren't added.", undo=bool(added))
        else:
            self.list_note(f"Added {words(len(added))}. Words already in your list were kept.", undo=bool(added))
        for e in added[:1]:
            self.rows.flash(e.key)

    def export_path(self, path: str) -> None:
        try:
            self.store.export_file(path)
        except OSError:
            self.list_note(NOTES["write"])
            return
        self.list_note(f"Saved {words(len(self.store))} to the file.")

    # --- The edit view -------------------------------------------------------------------------

    def start_add(self) -> None:
        if self.view == "list":
            self._start_edit(_Edit("add", None, "list"), "", "")

    def start_change(self, index: int) -> None:
        entries = self.store.entries()
        if 0 <= index < len(entries):
            e = entries[index]
            self._start_edit(_Edit("change", index, "list"), e.match, e.say, e)

    def _start_edit(self, edit: _Edit, word: str, say: str, entry: Entry | None = None) -> None:
        self._stop_test()
        self.edit = edit
        for f, text in ((self.f_word, word), (self.f_say, say)):
            f.blockSignals(True)
            f.setText(text)
            f.blockSignals(False)
        e = entry or Entry(word or "x", say)
        edit.case_touched = entry is not None and entry.match_case is not None
        self.row_case.set_checked(e.case_sensitive if word else False)
        self.row_whole.set_checked(e.whole_word)
        if edit.mode == "change":
            self.row_slot.set_checked(e.enabled)
        else:
            self.row_slot.set_checked(False)
        if edit.source == "word":
            self.f_say.selectAll()
        self._enter_edit()
        self._sync_edit(animate=False)

    def back_to_list(self) -> None:
        if self.view == "edit":
            self._stop_test()
            self._enter_list()

    def _case_toggled(self, _on: bool) -> None:
        self.edit.case_touched = True
        self._sync_edit()

    def current_entry(self) -> Entry:
        word = " ".join(self.f_word.text().split())
        say = " ".join(self.f_say.text().split())
        mc = self.row_case.checked if self.edit.case_touched else None
        if mc is not None and mc == any(ch.isupper() for ch in word):
            mc = None  # the same as smart case: keep the file simple
        enabled = self.row_slot.checked if self.edit.mode == "change" else True
        return Entry(word, say, mc, self.row_whole.checked, enabled)

    def _conflict(self, e: Entry) -> int | None:
        if not e.match:
            return None
        i = self.store.index_of(e)
        if i is None or (self.edit.mode == "change" and i == self.edit.index):
            return None
        return i

    def _sync_edit(self, animate: bool = True) -> None:
        if self.view != "edit":
            return
        e = self.current_entry()
        word = e.match
        if not self.edit.case_touched and word:
            want = any(ch.isupper() for ch in word)
            if want != self.row_case.checked:
                self.row_case.set_checked(want)
        self.row_case.set_detail(case_detail(word, self.row_case.checked))
        self.row_whole.set_detail(whole_detail(word, self.row_whole.checked))
        if self.edit.mode == "add":
            plural = plural_of(word) if e.say else None
            self.row_slot.setVisible(plural is not None)
            if plural:
                self._relabel(self.row_slot, f"Also add “{cut(plural, 24)}”, said “{cut(e.say + 's', 32)}”")
        else:
            self.row_slot.setVisible(True)
            self._relabel(self.row_slot, "Use this pronunciation")
        conflict = self._conflict(e)
        full = self.edit.mode == "add" and conflict is None and self.store.index_of(e) is None and self.store.is_full()
        label = "Replace" if conflict is not None else ("Add" if self.edit.mode == "add" else "Save")
        self.btn_primary.set_content(G_CHECK, label)
        self.btn_primary.setEnabled(bool(word) and not full)
        self.btn_primary.set_tooltip(FULL_TIP if full else "Save this pronunciation (Enter)")
        self.btn_delete.setVisible(self.edit.mode == "change")
        if self.edit.source == "word":
            self.set_title("Change how a word is said")
        elif self.edit.mode == "add":
            self.set_title("Add a word")
        else:
            original = self.store.entries()[self.edit.index].match if self.edit.index is not None and \
                self.edit.index < len(self.store) else word
            self.set_title(f"Change “{cut(original, 32)}”")
        self._place_header()
        # How it will be read
        ctx = self.edit.context
        if ctx:
            parts = preview_parts(ctx, replace(e, enabled=True)) if word else [("text", ctx)]
            if word and not any(k != "text" for k, _ in parts):
                where = " as a whole word" if e.whole_word else ""
                self.preview_well.set_content([], f"“{cut(word, 24)}” isn't in this sentence{where}.")
            else:
                self.preview_well.set_content(parts)
        elif e.say:
            self.preview_well.set_content([("say", e.say)])
        elif word:
            self.preview_well.set_content([("skip", word)])
        else:
            self.preview_well.set_content([], "Type a word, then how to say it.")
        self.btn_test.setEnabled(bool(self._test_text(e)) and self.preview is not None
                                 and self._test_state != "loading")
        # The hint row: a note wins over the keys
        if conflict is not None:
            self.show_note(f"You already have “{cut(word, 24)}”. Saving replaces it.")
        elif word and not e.say:
            self.show_note(NOT_SAID)
        elif self.hint.note:
            self.end_note()
        back = "goes back to the list" if self.edit.source == "list" else "goes back to reading"
        self.set_hint([("key", "Enter"), ("text", " saves  ·  "), ("key", "Esc"), ("text", f" {back}")])

    @staticmethod
    def _relabel(row: ToggleRow, label: str) -> None:
        if row.label != label:
            row.label = label
            row.setAccessibleName(f"{label}: {'on' if row.checked else 'off'}")
            row.update()

    def _save(self) -> None:
        e = self.current_entry()
        if not e.match:
            return
        self._stop_test()
        try:
            if self.edit.mode == "change" and self.edit.index is not None and self.edit.index < len(self.store):
                self.store.update(self.edit.index, e)
                note = "Pronunciation saved"
            else:
                replacing = self.store.index_of(e) is not None
                self.store.add(e)
                note = "Pronunciation saved" if replacing else "Pronunciation added"
                plural = plural_of(e.match) if e.say else None
                if plural and not self.row_slot.isHidden() and self.row_slot.checked:
                    try:
                        self.store.add(replace(e, match=plural, say=e.say + "s"))
                    except ValueError:
                        pass  # the list filled up: the word itself is in
        except ValueError:
            return
        if self.edit.source == "word":
            self.saved.emit(note)
            self.closeRequested.emit("saved")
            return
        self._enter_list()
        self.rows.flash(e.key)

    # --- Test ----------------------------------------------------------------------------------

    def _test_text(self, e: Entry) -> str:
        ctx = self.edit.context
        if ctx and e.match:
            text, spans = preview_spoken(ctx, replace(e, enabled=True))
            if spans:
                return text
        return e.say

    def _voice(self) -> tuple[str, float]:
        voice = self.settings.get("voice") or ""
        speed = self.ctl.speed() if hasattr(self.ctl, "speed") else 1.0
        return voice, speed

    def _play(self, text: str) -> None:
        if self.preview is None or not text:
            return
        if self._testing and getattr(self.preview, "state", "idle") in ("loading", "playing"):
            self._stop_test()
            return
        voice, speed = self._voice()
        self._testing = True
        self.preview.play(voice, speed, text)

    def _test(self) -> None:
        self._play(self._test_text(self.current_entry()))

    def _test_entry(self, index: int) -> None:
        entries = self.store.entries()
        if 0 <= index < len(entries):
            self._play(entries[index].say)

    def _stop_test(self) -> None:
        if self._testing and self.preview is not None:
            self._testing = False
            self.preview.stop()
        self._testing = False
        self._set_test_state("idle")

    def _set_test_state(self, state: str) -> None:
        """Test while idle, a disabled ring while the sample is made, Stop while it plays."""
        self._test_state = state
        self.btn_test.set_state(state)
        e = self.current_entry() if self.view == "edit" else None
        self.btn_test.setEnabled(state != "loading" and self.preview is not None
                                 and (e is None or bool(self._test_text(e))))

    def _on_preview_state(self, state: str) -> None:
        if not self._testing:
            return
        if state == "failed":
            self._testing = False
            self._set_test_state("idle")
            kind = parse_voice_id(self._voice()[0])[0]
            text = NOTES["offline"] if kind == "edge" and _offline() else NOTES["failed"]
            if self.view == "edit":
                self.show_note(text)
            else:
                self.list_note(text)
            return
        if state == "idle":
            self._testing = False
        self._set_test_state(state if state in ("loading", "playing") else "idle")

    # --- Views ---------------------------------------------------------------------------------

    def _enter_list(self, animate: bool = True) -> None:
        self.view = "list"
        self.edit = _Edit()
        self.set_title(TITLE)
        self.end_note()
        self._sync_on()
        self.refresh()
        self._switch(self.list_view, self.edit_view, animate)
        if self.isVisible() and self.is_foreground():
            target = self.rows if self.rows.items else self.focus_target()
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _enter_edit(self) -> None:
        animate = self.view == "list" and self.isVisible()
        self.view = "edit"
        self._set_info("")
        self._switch(self.edit_view, self.list_view, animate)
        if self.isVisible():
            target = self.focus_target()
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _switch(self, show: QWidget, hide: QWidget, animate: bool) -> None:
        self._place_header()
        if not animate or not self.isVisible() or self.theme.reduced_motion:
            self._leaving = None
            hide.hide()
            show.move(0, 0)
            show.show()
            for effect in self._fades.values():
                effect.setEnabled(False)
            self._swap.snap(1.0)
            return
        self._leaving = hide
        show.show()
        show.raise_()
        self._swap.snap(0.0)
        self._swap.to(1.0)

    def _on_swap(self, v: float) -> None:
        show = self.edit_view if self.view == "edit" else self.list_view
        hide = self._leaving
        rise = round(self.theme.px(4) * (1.0 - v))
        show.move(0, rise)
        fx = self._fades[show]
        fx.setEnabled(v < 1.0)
        fx.setOpacity(v)
        if hide is not None:
            hx = self._fades[hide]
            hx.setEnabled(True)
            hx.setOpacity(1.0 - v)
            if v >= 1.0:
                hide.hide()
                hx.setEnabled(False)
                self._leaving = None

