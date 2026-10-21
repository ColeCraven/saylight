"""The Type or paste box: a focusable editor window that covers the panel and grows out of it.

The panel never takes focus, so typing needs a window of its own. This one is unowned (an owned window would
hand activation to the panel when it hides), its Qt flags never change, and it holds the foreground only while
it is open. The text uses the reading card's font, size, spacing, line pitch and tint, so what you type looks
like what you will hear. Typed text lives only in the QTextDocument: it is never saved, logged or put on the
clipboard by Saylight.

The window's geometry is final from its first frame: the window fades in while the box grows inside it
(GrowOverlay), so the document is never laid out again during the motion.

What won't be read aloud (citations, links) is dimmed in the reading card's skipped colour (SkipHighlighter),
the box never fills itself from an opened file, and its context menu can change how a word is said.
"""

from __future__ import annotations

import bisect
import contextlib
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QRectF, QRegularExpression, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtGui import (QAction, QContextMenuEvent, QFontMetricsF, QGuiApplication, QKeyEvent, QKeySequence,
                           QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QSyntaxHighlighter,
                           QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextDocumentFragment,
                           QTextOption)
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect, QScrollBar, QStyle, QStyleOptionSlider,
                               QTextEdit, QWidget)

from .. import academic_text, win32
from ..alignment import Aligned, diff_edits
from ..engine import MAX_TEXT_CHARS, PAUSED
from .controls import (G_CLEAR, G_COLLAPSE, G_PASTE, G_TYPE, G_UNDO, FadeLabel, IconButton, Part, PillButton,
                       TextButton, draw_glyph, paint_parts, text_button_size)
from .motion import FAST, MEDIUM, SLOW, Tween, decelerate, exit_curve, with_alpha
from .panel import EXPANDED_W, MA_NOACTIVATE, WM_MOUSEACTIVATE, WPM_AT_1X
from .sentence_view import word_bounds

log = logging.getLogger("saylight.compose")

COMPOSE_FLAGS = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
WM_ACTIVATEAPP, WM_NCACTIVATE = 0x001C, 0x0086

G_READ = "\uf5b0"   # PlaySolid: the Read aloud pill
G_NOTE = "\ue946"   # Info: starts a note in the hint row

TITLE = "Type or paste"
READ_LABEL = "Read aloud"
PLACEHOLDER = "Type or paste text here."
TARGET_LINES = 12
CLOSE_MS = 120
NOTE_MS = 6000
COUNT_DELAY_MS = 150
AWAY_SETTLE_MS = 50  # light dismiss judges the foreground once it settles (Win+. passes it through the shell)
LIMIT_BEEP_S = 2.0
INK_SAMPLE = "Hdlbgjpqy"  # the reading card centres this ink in each row
SKIP_DELAY_MS = 400          # what won't be read aloud is dimmed this long after the last edit
SKIP_WORKER_CHARS = 20_000   # longer text is checked on a worker thread
PRONOUNCE_MAX = 100          # a selection up to this long can have its pronunciation changed
PRONOUNCE_LABEL = "Change how “{word}” is said…"

HINT: list[Part] = [("key", "Ctrl"), ("key", "Enter"), ("text", " reads it aloud  ·  "), ("key", "Esc"),
                    ("text", " goes back")]
HINT_PARTIAL: list[Part] = [("key", "Ctrl"), ("key", "Enter"), ("text", " reads the highlighted part  ·  "),
                            ("key", "Esc"), ("text", " goes back")]
NOTES = {  # kind: (text, beep)
    "empty": ("Type or paste some text first.", True),
    "nothing": ("Saylight found nothing to read in this text.", True),
    "limit": ("The box holds up to 100,000 characters, so the rest wasn't added.", True),
    "activation": ("Click in the box to start typing.", False),
}

# Why the box closed, and what that closing does (the App acts on these).
FOCUS_REASONS = frozenset({"read", "esc", "back", "hotkey", "screen", "screen_read"})  # hand focus back
RESUME_REASONS = frozenset({"esc", "back", "away", "tray", "hotkey", "screen"})      # resume a pause it caused
# Input pop-ups that come to the front while you type (clipboard history, emoji panel, IME candidates) and must
# not close the box. Shell windows are already ignored.
TRANSIENT_INPUT_CLASSES: frozenset[str] = frozenset()


# --- Pure helpers ------------------------------------------------------------------------------

def utf16_len(s: str) -> int:
    """The length of `s` in UTF-16 code units, which is how QTextDocument counts positions."""
    return len(s.encode("utf-16-le")) // 2


def chrome_height(theme) -> int:
    """Everything in the box but its text rows: header, the well's padding, hint row and footer (144 DIP at k = 1).
    The same sums as the panel's, so at the panel's rows the box is exactly the panel's height."""
    px = theme.px
    return px(44) + 2 * px(10) + px(8) + px(16) + px(4) + px(40) + px(12)


def compose_rect(rest: QRect, area: QRect, size: QSize, *, snap_x: str | None, margin: int,
                 bottom_margin: int) -> QRect:
    """Where the box goes: over the panel's resting rect, sharing its anchored edges, kept inside the work area's
    margins (or the panel's own rect, so it never uncovers the panel)."""
    W, H = size.width(), size.height()
    if snap_x == "left":
        x = rest.left()
    elif snap_x == "right":
        x = rest.right() + 1 - W
    else:  # centred; splitting the difference keeps an equally wide box exactly over the panel
        x = rest.left() + (rest.width() - W) // 2
    up = rest.center().y() > area.center().y()  # the panel's own anchoring test
    y = rest.bottom() + 1 - H if up else rest.top()
    allowed = area.adjusted(margin, margin, -margin, -bottom_margin).united(rest)
    x = max(allowed.left(), min(x, allowed.right() + 1 - W))
    y = max(allowed.top(), min(y, allowed.bottom() + 1 - H))
    return QRect(x, y, W, H)


def compose_lines(rest: QRect, area: QRect, *, lh: int, chrome: int, panel_lines: int, margin: int,
                  bottom_margin: int, target: int = TARGET_LINES) -> int:
    """How many text rows the box shows: `target`, fewer if the screen has no room, never fewer than the panel's."""
    up = rest.center().y() > area.center().y()
    room = rest.bottom() + 1 - (area.top() + margin) if up else area.bottom() + 1 - bottom_margin - rest.top()
    return max(panel_lines, min(target, (room - chrome) // lh))


def row_shift(theme) -> int:
    """δ: the card's first baseline minus the editor's, so the box's rows sit exactly where the card's do."""
    font = theme.font("reading")
    lh = theme.line_height("reading")
    key = (font.toString(), lh)
    if key not in _ROW_SHIFT:
        ink = QFontMetricsF(font).tightBoundingRect(INK_SAMPLE)
        card = (lh - ink.height()) / 2 - ink.top()
        doc = QTextDocument()
        doc.setDefaultFont(font)
        doc.setDocumentMargin(0)
        doc.setTextWidth(1000)
        doc.setPlainText("Hg")
        QTextCursor(doc).mergeBlockFormat(line_format(lh))
        block = doc.firstBlock()
        top = doc.documentLayout().blockBoundingRect(block).top()  # lays the block out
        layout = block.layout()
        if layout.lineCount() == 0:
            return 0
        line = layout.lineAt(0)
        _ROW_SHIFT[key] = round(card - (top + layout.position().y() + line.y() + line.ascent()))
    return _ROW_SHIFT[key]


_ROW_SHIFT: dict[tuple[str, int], int] = {}


def line_format(lh: int) -> QTextBlockFormat:
    """Every block's format: the card's fixed line pitch."""
    fmt = QTextBlockFormat()
    fmt.setLineHeight(lh, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
    return fmt


def compose_layout(W: int, L: int, theme) -> dict[str, QRect]:
    """Every part of the box for width W and L text rows, in window coordinates (DIP x k).

    The header, well and transport rows use the panel's own sums, so the layout for the panel's width and rows
    lands on the panel's header, card and transport row. "window" is the whole box."""
    px = theme.px
    lh = theme.line_height("reading")
    edge, pad_x, pad_y = px(12), px(14), px(10)
    back = QRect(W - edge - px(32), px(8), px(32), px(32))  # the panel's Hide slot
    title = QRect(px(46), px(14), int(QFontMetricsF(theme.font("status")).horizontalAdvance(TITLE) + 2), px(20))
    info_x = title.right() + 1 + px(12)
    info = QRect(info_x, px(14), max(0, back.left() - px(8) - info_x), px(20))
    well = QRect(edge, px(44), W - 2 * edge, L * lh + 2 * pad_y)
    # The viewport is exactly the card's text width; the 6 DIP scrollbar sits in the right padding.
    text = QRect(edge + pad_x, well.top() + pad_y + row_shift(theme), well.width() - 2 * pad_x + px(6), L * lh)
    paste = QRect(QPoint(edge + pad_x, well.top() + pad_y + lh + px(4)), text_button_size(theme, ("Paste",)))
    hint_y = well.bottom() + 1 + px(8)
    yt = hint_y + px(16) + px(4)
    label_w = QFontMetricsF(theme.font("status")).horizontalAdvance(READ_LABEL)
    pill = QRect(edge, yt, math.ceil(px(34) + label_w + px(16)), px(40))  # glyph at +10, label at +34
    clear_size = text_button_size(theme, ("Clear", "Undo"))
    clear = QRect(QPoint(W - edge - clear_size.width(), yt + px(4)), clear_size)
    return {"glyph": QRect(px(22), px(16), px(16), px(16)), "title": title, "info": info, "back": back,
            "well": well, "text": text, "paste": paste, "hint": QRect(px(22), hint_y, W - 2 * px(22), px(16)),
            "pill": pill, "clear": clear, "window": QRect(0, 0, W, yt + px(40) + edge)}


def play_circle(theme, yt: int) -> QRect:
    """The panel's Play circle on the transport row at `yt`: where the Read aloud pill grows from."""
    px = theme.px
    inset = (px(48) - px(40)) // 2
    return QRect(px(48) + inset, yt - px(4) + inset, px(40), px(40))


_BREAKS = re.compile("\r\n?|[\u2028\u2029]")
_PASTE_TABLE = {c: None for c in range(0x20) if c != 0x0A}
_PASTE_TABLE.update({0x09: " ", 0xAD: None, 0x200B: None, 0x200C: None, 0x200D: None, 0x2060: None, 0xFEFF: None})


def sanitize_paste(s: str) -> str:
    """Pasted or dropped text made plain: one kind of line break, tabs as spaces, and no soft hyphens, zero-width
    characters, byte-order marks or other control codes. No-break spaces stay."""
    return _BREAKS.sub("\n", s).translate(_PASTE_TABLE)


def cap_insert(existing_len: int, incoming: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    """The part of `incoming` that fits beside `existing_len` characters, and whether it was cut. A cut ends at the
    last whitespace that fits, or mid-word when there is none."""
    room = max(0, limit - existing_len)
    if len(incoming) <= room:
        return incoming, False
    head = incoming[:room + 1]  # a space just past the room is a clean cut too
    for i in range(len(head) - 1, -1, -1):
        if head[i].isspace():
            return incoming[:i], True
    return incoming[:room], True


def _duration(minutes: float) -> str:
    if minutes < 1:
        return "under a minute"
    hours, mins = divmod(round(minutes), 60)
    if not hours:
        return f"about {mins} min"
    return f"about {hours} h {mins} min" if mins else f"about {hours} h"


def word_count(text: str) -> int:
    """The words the header counts: runs of non-space that hold a letter or digit (so "(((" or "…" is none)."""
    return sum(1 for w in text.split() if any(c.isalnum() for c in w))


def count_label(words: int, speed: float, highlighted: bool = False) -> str:
    """The header's count: "47 words · under a minute", "12 words highlighted · about 2 min"; "" for none."""
    if words <= 0:
        return ""
    what = f"{words:,} {'word' if words == 1 else 'words'}" + (" highlighted" if highlighted else "")
    return f"{what} · {_duration(words / (WPM_AT_1X * max(0.1, speed)))}"


def read_scope(selected: str, full: str) -> tuple[str, str]:
    """What a read gesture reads: (text, "selection") for a partial highlight, else (full, "all")."""
    if selected.strip() and selected.strip() != full.strip():
        return selected, "selection"
    return full, "all"


def prefill_plan(user_text: bool, current: str, box: tuple[str, int] | None, *,
                 source_kind: str = "") -> tuple[str, str, int]:
    """What the box shows as it opens.

    ("keep", "", 0) keeps the text you typed as you left it; ("load", text, pos) loads the reading's text with
    `pos` its current sentence's start in UTF-16 units; ("empty", "", 0) starts empty. `box` is the reading's
    text and the offset of its current sentence, or None. A file's text never fills the box (it can run to
    hundreds of pages, and would become a draft): with `source_kind` "file" it keeps your text or starts empty."""
    if user_text and current.strip():
        return "keep", "", 0
    if box is not None and box[0] and source_kind != "file":
        text, start = box
        return "load", text, utf16_len(text[:max(0, min(start, len(text)))])
    return "empty", "", 0


def reading_prefill(doc, sentence: int) -> tuple[str, int] | None:
    """The `prefill` for open_at(): the reading's text and its current sentence's start, or None with no reading
    and for a document opened from a file (the box is never filled from a file)."""
    if doc is None or not doc.sentences or getattr(getattr(doc, "source", None), "kind", "") == "file":
        return None
    return doc.text, doc.sentences[max(0, min(sentence, len(doc.sentences) - 1))].start


def skipped_spans(text: str, *, skip_citations: bool = True,
                  skip_urls: bool = True) -> list[tuple[int, int]] | None:
    """The parts of `text` that won't be read aloud (citations, links), as (start, end) offsets into it, or None
    when that can't be told for sure, so nothing is dimmed.

    The spoken text is academic_text.normalize()'s, the one a reading gets; diff_edits() compares it with `text`
    word by word, and its result is used only when it rebuilds the spoken text exactly."""
    spoken = academic_text.normalize(text, skip_citations=skip_citations, skip_urls=skip_urls)
    try:
        edits = diff_edits(text, spoken)
        if Aligned(text, edits).spoken != spoken:
            return None
    except ValueError:
        return None
    spans = []
    for e in edits:
        a, b = e.start, e.end
        if e.kind != "skip":
            continue
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            spans.append((a, b))
    return spans


def to_utf16(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Spans in code points as UTF-16 units (QTextDocument positions): they differ after an emoji or the like."""
    if not text or max(text) < "\U00010000":
        return list(spans)
    units = [0]
    for c in text:
        units.append(units[-1] + (2 if ord(c) > 0xFFFF else 1))
    return [(units[a], units[b]) for a, b in spans]


def pronounce_label(word: str) -> str:
    """The context-menu item for `word`, a long selection shortened in the middle."""
    if len(word) > 40:
        word = word[:19] + "…" + word[-19:]
    return PRONOUNCE_LABEL.format(word=word)


def should_resume(paused_doc, document, state: str, reason: str) -> bool:
    """Whether closing for `reason` resumes the reading the box paused as it opened: only when that same reading
    is still loaded and still paused."""
    return paused_doc is not None and paused_doc is document and state == PAUSED and reason in RESUME_REASONS


def is_light_dismiss(phase: str, was_active: bool, hwnd: int, pid: int, class_name: str, *, own_pid: int) -> bool:
    """Whether a new foreground window closes the box: another app's window, never the shell or our own, and only
    once the box is fully open and has had the foreground (a refused activation never closes it)."""
    return (phase == "open" and was_active and bool(hwnd) and pid not in (0, own_pid)
            and class_name not in win32.SHELL_CLASSES and class_name not in TRANSIENT_INPUT_CLASSES)


def spoken(parts: list[Part]) -> str:
    """A keycap hint as a screen reader should say it: keys pressed together joined by "+", and each "·" ending a
    sentence ("Ctrl+Enter reads it aloud. Esc goes back.")."""
    text, prev = "", ""
    for kind, s in parts:
        text += ("+" if kind == prev == "key" else "") + s
        prev = kind
    return " ".join(f"{piece.strip()}." for piece in text.split("·"))


def whole_rows(well: QRect, text: QRect, lh: int, scroll: int) -> QRect:
    """The text rows lying wholly inside `well` (the growing box), as one rect; empty while the well is still
    narrower than the text. `text` is the text field's rect and `scroll` its scroll position."""
    if well.left() > text.left() or well.right() < text.right():
        return QRect()
    origin = text.top() - scroll % lh  # where the row grid starts
    top, bottom = max(well.top(), text.top()), min(well.bottom(), text.bottom()) + 1
    first, last = -(-(top - origin) // lh), (bottom - origin) // lh
    if last <= first:
        return QRect()
    return QRect(text.left(), origin + first * lh, text.width(), (last - first) * lh)


def _lerp_rect(a: QRect, b: QRect, p: float) -> QRect:
    def f(u: int, v: int) -> int:
        return round(u + (v - u) * p)
    return QRect(f(a.x(), b.x()), f(a.y(), b.y()), f(a.width(), b.width()), f(a.height(), b.height()))


@dataclass
class ComposeSession:
    """One opening of the box. The App sets `source` and `paused_doc`; the window keeps the rest."""
    phase: str = "closed"       # closed | opening | open | closing
    return_to: int = 0          # the window focus goes back to
    paused_doc: object = None   # the reading the box paused as it opened
    was_active: bool = False    # the box has had the foreground (light dismiss waits for it)
    source: str = ""            # button | card | tray | hotkey


@dataclass(frozen=True)
class ComposeSnapshot:
    """The box at a Clear, kept in memory for the undo window."""
    text: str
    position: int
    anchor: int
    scroll: int
    user_text: bool


# --- The text field ----------------------------------------------------------------------------

class ThinScrollBar(QScrollBar):
    """6 DIP wide, no groove or arrows: a rounded thumb in the text colour that thickens from 2 to 6 DIP under the
    pointer. Nothing is drawn when there is nothing to scroll, and its width never changes, so text never reflows."""

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self.theme = theme
        self.hover = Tween(self, theme, 0.0, FAST)
        self.sliderReleased.connect(lambda: self.hover.to(1.0 if self.underMouse() else 0.0))

    def sizeHint(self) -> QSize:
        return QSize(self.theme.px(6), super().sizeHint().height())

    def enterEvent(self, event) -> None:
        self.hover.to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self.isSliderDown():
            self.hover.to(0.0)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        if self.maximum() <= self.minimum():
            return
        t = self.theme
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        r = QRectF(self.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, opt,
                                               QStyle.SubControl.SC_ScrollBarSlider, self))
        h = 1.0 if self.isSliderDown() else self.hover.value
        w = t.k * (2 + 4 * h)
        thumb = QRectF(r.center().x() - w / 2, r.top(), w, r.height())
        color = (t.color("TextPrimary") if t.high_contrast
                 else with_alpha(t.reading_colors()["text"], 0.30 + 0.25 * h))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(thumb, w / 2, w / 2)


class ComposeEdit(QTextEdit):
    """The text field: plain text only, the reading card's typography, and the box's keys.

    Qt drops a block's line height on pasted paragraphs, clears and loads, so a deferred enforcer merges it back
    onto every changed block (joined to the edit that caused it, so one Ctrl+Z undoes both) and trims anything
    typed past the limit."""

    readKey = Signal()
    escapeKey = Signal()
    undoKey = Signal()
    sizeStep = Signal(int)
    limitHit = Signal()
    edited = Signal()
    pronunciationRequested = Signal(str)  # a word or a short selection, from the context menu

    _NON_SPACE = QRegularExpression(r"\S", QRegularExpression.PatternOption.UseUnicodePropertiesOption)

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.undo_mode = False  # Ctrl+Z in an empty box undoes the Clear
        self._quiet_depth = 0
        self._dirty: tuple[int, int] | None = None
        self._fmt = line_format(theme.line_height("reading"))
        # All of this before the first show: the style keeps its own background otherwise.
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.viewport().setAutoFillBackground(True)
        self.document().setDocumentMargin(0)
        self.setTabChangesFocus(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBar(ThinScrollBar(theme, self))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Painted here rather than with setPlaceholderText: Qt draws its placeholder at the top of the field,
        # ignoring the line pitch, so it would sit above where the first typed line appears.
        self.placeholder = PLACEHOLDER
        self.setAccessibleName("Text to read aloud")
        self.setAccessibleDescription("Type or paste text. Control Enter reads it aloud. Escape goes back.")
        self._enforce_timer = QTimer(self)
        self._enforce_timer.setSingleShot(True)
        self._enforce_timer.setInterval(0)
        self._enforce_timer.timeout.connect(self._enforce)
        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(0)
        self._snap_timer.timeout.connect(self._snap_rows)
        self.cursorPositionChanged.connect(self._snap_timer.start)
        self.document().contentsChange.connect(self._on_change)
        self.apply_theme()
        self.load("")  # the first block takes the line pitch too

    @contextlib.contextmanager
    def _quiet(self) -> Iterator[None]:
        """Changes made here are ours, not the user's: no `edited`, no enforcer pass."""
        self._quiet_depth += 1
        try:
            yield
        finally:
            self._quiet_depth -= 1

    # --- Theme and loading

    def apply_theme(self) -> None:
        """Font, colours, caret and line pitch from the theme; every block takes the new pitch."""
        t = self.theme
        c = t.reading_colors()
        with self._quiet():
            self.setFont(t.font("reading"))
        pal = QPalette(self.palette())
        hc = t.high_contrast
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            pal.setColor(group, QPalette.ColorRole.Base, c["background"])
            pal.setColor(group, QPalette.ColorRole.Text, c["text"])
            pal.setColor(group, QPalette.ColorRole.PlaceholderText, t.color("TextDisabled") if hc else c["secondary"])
            pal.setColor(group, QPalette.ColorRole.HighlightedText, c["highlight_text"])
            pal.setColor(group, QPalette.ColorRole.Highlight,
                         c["highlight_paused"] if group == QPalette.ColorGroup.Inactive and not hc else c["highlight"])
        self.setPalette(pal)
        self.setCursorWidth(max(t.px(2), win32.caret_width_px()))
        lh = t.line_height("reading")
        bar = self.verticalScrollBar()
        bar.setSingleStep(lh)
        bar.updateGeometry()
        if self._fmt.lineHeight() != lh:
            self._fmt = line_format(lh)
            self._merge_all()
        bar.update()

    def _merge_all(self) -> None:
        """Give every block the current pitch, joined to the last edit (or off the undo stack if there is none)."""
        doc = self.document()
        with self._quiet():
            if doc.isUndoAvailable():
                c = QTextCursor(doc)
                c.joinPreviousEditBlock()
                c.select(QTextCursor.SelectionType.Document)
                c.mergeBlockFormat(self._fmt)
                c.endEditBlock()
            else:
                doc.setUndoRedoEnabled(False)
                c = QTextCursor(doc)
                c.select(QTextCursor.SelectionType.Document)
                c.mergeBlockFormat(self._fmt)
                doc.setUndoRedoEnabled(True)

    def load(self, text: str) -> None:
        """Replace the text without an undo step (the undo history starts again from here)."""
        doc = self.document()
        self._enforce_timer.stop()
        self._dirty = None
        with self._quiet():
            doc.setUndoRedoEnabled(False)
            self.setPlainText(text)
            c = QTextCursor(doc)
            c.select(QTextCursor.SelectionType.Document)
            c.mergeBlockFormat(self._fmt)
            doc.setUndoRedoEnabled(True)

    def is_blank(self) -> bool:
        return self.document().find(self._NON_SPACE).isNull()

    def row_of(self, pos: int) -> int:
        """The text row (0-based) that holds document position `pos`."""
        doc = self.document()
        if doc.textWidth() <= 0:  # not laid out yet while hidden
            doc.setTextWidth(self.viewport().width())
        pos = max(0, min(pos, doc.characterCount() - 1))
        block = doc.findBlock(pos)
        top = doc.documentLayout().blockBoundingRect(block).top()  # lays the document out up to this block
        layout = block.layout()
        if layout is None or layout.lineCount() == 0:
            return 0
        line = layout.lineForTextPosition(pos - block.position())
        y = top + (line.y() if line.isValid() else 0.0)
        return max(0, round(y / self.theme.line_height("reading")))

    def _snap_rows(self) -> None:
        """Keep the view on whole rows. Qt scrolls the caret into view by its ink, which can leave the view a pixel
        or two off the row grid; finish the move toward the caret."""
        bar, lh = self.verticalScrollBar(), self.theme.line_height("reading")
        off = bar.value() % lh
        if not off or bar.isSliderDown() or QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return  # a drag (of the thumb or a selection) scrolls where the pointer says
        down = self.cursorRect().center().y() > self.viewport().height() / 2
        bar.setValue(min(bar.maximum(), bar.value() + (lh - off if down else -off)))

    # --- Placeholder

    def placeholder_origin(self) -> QPointF | None:
        """Where the placeholder's baseline starts: on the first row, exactly where typing will appear; None when
        it isn't shown (the box has text, or an IME composition is under way)."""
        doc = self.document()
        if not doc.isEmpty():
            return None
        block = doc.firstBlock()
        top = doc.documentLayout().blockBoundingRect(block).top()  # lays the block out
        layout = block.layout()
        if layout is None or layout.lineCount() == 0 or layout.preeditAreaText():
            return None
        line = layout.lineAt(0)
        return QPointF(line.x(), top + line.y() + line.ascent() - self.verticalScrollBar().value())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        origin = self.placeholder_origin()
        if origin is None:
            return
        font = self.font()
        text = QFontMetricsF(font).elidedText(self.placeholder, Qt.TextElideMode.ElideRight, self.viewport().width())
        p = QPainter(self.viewport())
        p.setFont(font)
        p.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
        p.drawText(origin, text)

    # --- Enforcer

    def _on_change(self, pos: int, _removed: int, added: int) -> None:
        if self._quiet_depth:
            return
        lo, hi = (pos, pos + added) if self._dirty is None else (min(self._dirty[0], pos),
                                                                  max(self._dirty[1], pos + added))
        self._dirty = (lo, hi)
        self._enforce_timer.start()
        self.edited.emit()

    def _length(self) -> int:
        doc = self.document()
        units = doc.characterCount() - 1
        return units if units <= MAX_TEXT_CHARS else len(self.toPlainText())  # code points <= UTF-16 units

    def _formatted(self, block) -> bool:
        fmt = block.blockFormat()
        return (fmt.lineHeightType() == QTextBlockFormat.LineHeightTypes.FixedHeight.value
                and fmt.lineHeight() == self._fmt.lineHeight())

    def _enforce(self) -> None:
        if self._dirty is None:
            return
        doc = self.document()
        end = doc.characterCount() - 1
        lo, hi = (max(0, min(v, end)) for v in self._dirty)
        self._dirty = None
        over = self._length() - MAX_TEXT_CHARS
        trimmed = over > 0 and hi > lo  # typed, dropped or composed past the limit: drop the end of that insert
        with self._quiet():
            cursor = QTextCursor(doc)
            cursor.joinPreviousEditBlock()
            if trimmed:
                cursor.setPosition(lo)
                cursor.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
                inserted = cursor.selection().toPlainText()
                keep = utf16_len(inserted[:max(0, len(inserted) - over)])
                cursor.setPosition(lo + keep)
                cursor.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                hi = lo + keep
            block, last = doc.findBlock(lo), doc.findBlock(hi)
            while block.isValid():
                if not self._formatted(block):
                    cursor.setPosition(block.position())
                    cursor.mergeBlockFormat(self._fmt)
                if block == last:
                    break
                block = block.next()
            cursor.endEditBlock()
        if trimmed:
            self.limitHit.emit()

    # --- Paste and drop: plain text only, capped

    def canInsertFromMimeData(self, source) -> bool:
        return source.hasText() or source.hasHtml()

    def insertFromMimeData(self, source) -> None:
        if source.hasText():
            text = source.text()
        elif source.hasHtml():
            text = QTextDocumentFragment.fromHtml(source.html()).toPlainText()
        else:
            return  # images and files
        cursor = self.textCursor()
        existing = len(self.toPlainText()) - len(cursor.selection().toPlainText())
        piece, cut = cap_insert(existing, sanitize_paste(text))
        cursor.insertText(piece)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        log.info("Compose: pasted %s characters (cut: %s)", f"{len(piece):,}", "yes" if cut else "no")
        if cut:
            self.limitHit.emit()

    # --- Keys and wheel

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and ctrl:
            self.readKey.emit()
        elif key == Qt.Key.Key_QuoteLeft and ctrl:  # arrives only while the global shortcut isn't registered
            self.readKey.emit()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods & Qt.KeyboardModifier.ShiftModifier:
            # A normal paragraph, never Qt's U+2028 line separator.
            super().keyPressEvent(QKeyEvent(event.type(), key, Qt.KeyboardModifier.NoModifier, event.text(),
                                            event.isAutoRepeat(), event.count()))
        elif key == Qt.Key.Key_Escape:
            self.escapeKey.emit()
        elif event.matches(QKeySequence.StandardKey.Undo) and self.undo_mode and self.document().isEmpty():
            self.undoKey.emit()
        elif ctrl and (key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus) or event.matches(QKeySequence.StandardKey.ZoomIn)):
            self.sizeStep.emit(1)
        elif ctrl and (key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore)
                       or event.matches(QKeySequence.StandardKey.ZoomOut)):
            self.sizeStep.emit(-1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # --- Context menu: change how a word is said

    def pronounce_target(self) -> str:
        """The word at the caret, or the selection when it is 1 to PRONOUNCE_MAX characters ("" for none)."""
        c = self.textCursor()
        if c.hasSelection():
            words = c.selectedText().replace("\u2029", " ").split()
            text = " ".join(words)
            return text if 1 <= len(text) <= PRONOUNCE_MAX else ""
        line = c.block().text()
        i = len(line.encode("utf-16-le")[:2 * c.positionInBlock()].decode("utf-16-le", "ignore"))
        span = word_bounds(line, i) or word_bounds(line, i - 1)  # in the word, or just after it
        return line[span[0]:span[1]] if span else ""

    def context_menu(self, pos: QPoint):
        """Qt's own menu for the text, starting with "Change how “word” is said…" when there is a word."""
        menu = self.createStandardContextMenu(pos)
        word = self.pronounce_target()
        if word:
            first = menu.actions()[0] if menu.actions() else None
            action = QAction(pronounce_label(word), menu)
            action.triggered.connect(lambda: self.pronunciationRequested.emit(word))
            menu.insertAction(first, action)
            menu.insertSeparator(first)
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if event.reason() == QContextMenuEvent.Reason.Mouse:  # a right-click outside the selection moves the caret
            hit, c = self.cursorForPosition(event.pos()), self.textCursor()
            if not (c.hasSelection() and c.selectionStart() <= hit.position() <= c.selectionEnd()):
                self.setTextCursor(hit)
        menu = self.context_menu(event.pos())
        menu.exec(event.globalPos())
        menu.deleteLater()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:  # text size, never QTextEdit's own zoom
            steps = event.angleDelta().y()
            if steps:
                self.sizeStep.emit(1 if steps > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


class _Relay(QObject):
    """Carries a worker thread's result to the GUI thread (a queued signal)."""
    done = Signal(int, object)


class SkipHighlighter(QSyntaxHighlighter):
    """Dims what won't be read aloud in the box, in the reading card's skipped colour, so typed or pasted text looks
    the way the card will show it.

    The text is checked SKIP_DELAY_MS after the last edit, on a worker thread when it is longer than
    SKIP_WORKER_CHARS; until then the dimmed spans move with the edits, and one that is edited into is dropped. A
    check that can't be trusted (skipped_spans() gives None) dims nothing. The formats live in the layout only:
    they never reach the text, the undo history or `edited`."""

    checked = Signal()  # a check's result is showing

    def __init__(self, edit: "ComposeEdit", theme, settings, *,
                 analyse: Callable[..., list[tuple[int, int]] | None] | None = None):
        super().__init__(None)  # attached to the document last, so a span moves before its block is redrawn
        self.setParent(edit)
        self._theme, self._settings = theme, settings
        self._analyse = analyse or skipped_spans
        self._spans: list[tuple[int, int]] = []  # in UTF-16 units, sorted
        self._starts: list[int] = []
        self._text = ""                          # the text the spans belong to
        self._rev = 0
        self._busy = False
        self._fmt = QTextCharFormat()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SKIP_DELAY_MS)
        self._timer.timeout.connect(self.check_now)
        self._relay = _Relay(self)
        self._relay.done.connect(self._on_result)
        doc = edit.document()
        doc.contentsChange.connect(self._on_change)
        theme.changed.connect(self._on_theme)
        settings.changed.connect(self._on_setting)
        self._on_theme()
        self.setDocument(doc)
        self._text = doc.toPlainText()

    @property
    def spans(self) -> list[tuple[int, int]]:
        """The dimmed spans, as document positions."""
        return list(self._spans)

    def _set_spans(self, spans: list[tuple[int, int]]) -> None:
        self._spans = sorted(spans)
        self._starts = [a for a, _ in self._spans]

    def _on_change(self, pos: int, removed: int, added: int) -> None:
        text = self.document().toPlainText()
        if text == self._text:  # a format change (the line pitch), not an edit
            return
        self._text = text
        self._rev += 1
        moved = added - removed
        self._set_spans([(a, b) if b <= pos else (a + moved, b + moved) for a, b in self._spans
                         if b <= pos or a >= pos + removed])
        self._timer.start()

    def check_now(self) -> None:
        """Check the text now (the delay after an edit ends here)."""
        self._timer.stop()
        if self._busy:  # the running check's answer is stale already: check again when it comes
            return
        text, rev = self._text, self._rev
        if not text.strip():
            self._show(rev, text, [])
            return
        options = {"skip_citations": self._setting("skip_citations"), "skip_urls": self._setting("skip_urls")}
        if len(text) <= SKIP_WORKER_CHARS:
            self._show(rev, text, self._run(text, options))
            return
        self._busy = True
        threading.Thread(target=lambda: self._relay.done.emit(rev, (text, self._run(text, options))),
                         name="saylight-compose-check", daemon=True).start()

    def _setting(self, key: str) -> bool:
        value = self._settings.get(key)
        return True if value is None else bool(value)

    def _run(self, text: str, options: dict) -> list[tuple[int, int]] | None:
        try:
            return self._analyse(text, **options)
        except Exception as e:  # never break typing over a check
            log.warning("Compose: skip check failed (%s)", type(e).__name__)
            return None

    def _on_result(self, rev: int, result) -> None:
        self._busy = False
        text, spans = result
        if rev == self._rev:
            self._show(rev, text, spans)
        elif not self._timer.isActive():
            self.check_now()

    def _show(self, rev: int, text: str, spans: list[tuple[int, int]] | None) -> None:
        if rev != self._rev:
            return
        old = set(self._spans)
        self._set_spans(to_utf16(text, spans or []))
        doc = self.document()
        for a, b in old ^ set(self._spans):  # redraw only the paragraphs whose dimming changed
            block, last = doc.findBlock(a), doc.findBlock(max(a, b - 1))
            while block.isValid():
                self.rehighlightBlock(block)
                if block == last:
                    break
                block = block.next()
        self.checked.emit()

    def _on_theme(self) -> None:
        self._fmt = QTextCharFormat()
        self._fmt.setForeground(self._theme.reading_colors()["skipped"])
        if self._spans and self.document() is not None:
            self.rehighlight()

    def _on_setting(self, key: str) -> None:
        if key == "reading_tint":
            self._on_theme()
        elif key in ("skip_citations", "skip_urls"):
            self._rev += 1
            self.check_now()

    def highlightBlock(self, text: str) -> None:
        if not self._spans:
            return
        block = self.currentBlock()
        start, end = block.position(), block.position() + block.length() - 1
        i = max(0, bisect.bisect_right(self._starts, start) - 1)
        while i < len(self._spans) and self._spans[i][0] < end:
            a, b = self._spans[i]
            a, b = max(a, start), min(b, end)
            if b > a:
                self.setFormat(a - start, b - a, self._fmt)
            i += 1


class WellFades(QWidget):
    """6 DIP fades at the top and bottom of the text when there is more to scroll that way, as on the card."""

    def __init__(self, theme, edit: ComposeEdit, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.bar = edit.verticalScrollBar()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.bar.valueChanged.connect(self.update)
        self.bar.rangeChanged.connect(self.update)

    def paintEvent(self, _event) -> None:
        bar = self.bar
        if bar.maximum() <= bar.minimum():
            return
        bg = self.theme.reading_colors()["background"]
        fade = self.theme.px(6)
        p = QPainter(self)
        if bar.value() > bar.minimum():
            g = QLinearGradient(0, 0, 0, fade)
            g.setColorAt(0, bg)
            g.setColorAt(1, with_alpha(bg, 0.0))
            p.fillRect(QRectF(0, 0, self.width(), fade), g)
        if bar.value() < bar.maximum():
            g = QLinearGradient(0, self.height() - fade, 0, self.height())
            g.setColorAt(0, with_alpha(bg, 0.0))
            g.setColorAt(1, bg)
            p.fillRect(QRectF(0, self.height() - fade, self.width(), fade), g)


class GrowOverlay(QWidget):
    """The box growing out of the panel's card, painted over the final well while it grows: Surface where the well
    has not grown yet, the well's background over the text (fading away with the content, but only over rows the
    well already holds whole, so no row shows cut in half), and the hairline at the well's current edge."""

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.well = QRect()
        self.rows = QRect()
        self.q = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_state(self, final_well: QRect, well: QRect, q: float, rows: QRect) -> None:
        """`well` is the well as far as it has grown and `rows` the text rows it holds whole, both in the window's
        coordinates, like `final_well`."""
        self.setGeometry(final_well)
        self.well = well.translated(-final_well.topLeft())
        self.rows = rows.translated(-final_well.topLeft())
        self.q = q
        self.update()

    def paintEvent(self, _event) -> None:
        t = self.theme
        c = t.reading_colors()
        radius = t.px(6)
        well = QRectF(self.well)
        current, inner, everything, rows = QPainterPath(), QPainterPath(), QPainterPath(), QPainterPath()
        current.addRoundedRect(well, radius, radius)
        inner.addRoundedRect(well.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        everything.addRect(QRectF(self.rect()))
        rows.addRect(QRectF(self.rows))
        rows = current.intersected(rows)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Surface reaches a pixel into the well, so its antialiased edge never meets the well's own and lets the
        # hidden text show through the seam; the hairline drawn last covers that pixel.
        p.fillPath(everything.subtracted(inner), t.color("Surface"))
        if self.q < 1.0:
            p.fillPath(current.subtracted(rows), c["background"])
            p.fillPath(rows, with_alpha(c["background"], 1.0 - self.q))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(c["edge"], 0))
        p.drawRoundedRect(QRectF(self.well).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


class _Glyph(QWidget):
    """The header's 16 DIP glyph on the 22 DIP column."""

    def __init__(self, theme, glyph: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.glyph = glyph
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme.changed.connect(self.update)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_glyph(p, QRectF(self.rect()), self.glyph, self.theme.font("icon16"), self.theme.color("TextSecondary"))


class _HintRow(QWidget):
    """The keycap hint under the box, or a note in its place (a Caution glyph, then the text); changes crossfade."""

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.content: tuple[str, object] = ("hint", HINT)
        self._prev: tuple[str, object] | None = None
        self.fade = Tween(self, theme, 1.0, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme.changed.connect(self.update)
        self._name()

    @property
    def note(self) -> bool:
        return self.content[0] == "note"

    def set_content(self, kind: str, value: object) -> None:
        if (kind, value) == self.content:
            return
        self._prev, self.content = self.content, (kind, value)
        self.fade.snap(0.0)
        self.fade.to(1.0)
        self._name()
        self.update()

    def _name(self) -> None:
        kind, value = self.content
        self.setAccessibleName(value if kind == "note" else spoken(value))

    def _paint(self, p: QPainter, content: tuple[str, object], opacity: float) -> None:
        if opacity <= 0:
            return
        t = self.theme
        p.setOpacity(opacity)
        kind, value = content
        if kind == "hint":
            paint_parts(p, t, 0, 0, self.height(), value, t.color("TextSecondary"), role="small")
            return
        g = t.px(16)
        draw_glyph(p, QRectF(0, (self.height() - g) / 2, g, g), G_NOTE, t.font("icon16"), t.color("Caution"))
        font = t.font("small")
        fm = QFontMetricsF(font)
        x = t.px(24)  # the text on the 46 DIP column
        text = fm.elidedText(value, Qt.TextElideMode.ElideRight, self.width() - x)
        p.setFont(font)
        p.setPen(t.color("TextPrimary"))
        p.drawText(QRectF(x, 0, self.width() - x, self.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        f = self.fade.value
        if f < 1.0 and self._prev:
            self._paint(p, self._prev, 1.0 - f)
        self._paint(p, self.content, f)


# --- The window --------------------------------------------------------------------------------

class ComposeWindow(QWidget):
    """The Type or paste window. Created hidden at start-up; the App opens and closes it."""

    readRequested = Signal(str, str)   # text, "selection" or "all"
    clearRequested = Signal()
    undoRequested = Signal()
    pronunciationRequested = Signal(str)  # context menu: the App closes the box ("switch") and opens Pronunciations
    closeRequested = Signal(str)       # esc | back | away | screen
    sizeStepRequested = Signal(int)
    textEdited = Signal()
    closed = Signal(str)

    def __init__(self, ctl):
        super().__init__(None, COMPOSE_FLAGS)  # always unowned, and the flags never change
        self.ctl = ctl
        t = self.theme = ctl.theme
        tips = ctl.tips
        self.session = ComposeSession()
        self.user_text = False  # changed by the user since it was loaded or cleared
        self._speed = 1.0
        self._undo_mode = False
        self._panel_visible = True
        self._panel_lines = 4
        self._lines = TARGET_LINES
        self._covered = False
        self._rect = QRect()
        self._slide_dir = 1
        self._scroll_to: tuple[str, int] | None = None
        self._note_kind = ""
        self._limit_beep_at = float("-inf")
        self._reason = ""
        self.setWindowTitle("Type or paste – Saylight")
        self.hwnd = int(self.winId())

        # Stacking, bottom to top: the text, its fades, the grow overlay, then everything else.
        self.edit = ComposeEdit(t, self)
        self.highlighter = SkipHighlighter(self.edit, t, ctl.settings)
        self.fades = WellFades(t, self.edit, self)
        self.overlay = GrowOverlay(t, self)
        self.overlay.hide()
        self.glyph = _Glyph(t, G_TYPE, self)
        self.title = FadeLabel(t, "status", "TextPrimary", self)
        self.title.set_text(TITLE, animate=False)
        self.info = FadeLabel(t, "small", "TextSecondary", self, align=Qt.AlignmentFlag.AlignRight)
        self.btn_back = IconButton(t, tips, G_COLLAPSE, size=32, small_glyph=True, parent=self,
                                   tooltip=self._back_tip, name="Back to the panel", focusable=True)
        self.btn_paste = TextButton(t, tips, G_PASTE, "Paste", style="chip", parent=self,
                                    tooltip="Paste the text you copied (Ctrl+V)")
        self.btn_paste.hide()
        self.hint = _HintRow(t, self)
        self.pill = PillButton(t, tips, G_READ, READ_LABEL, self, tooltip="Read it aloud (Ctrl+Enter)")
        self.btn_clear = TextButton(t, tips, G_CLEAR, "Clear", parent=self, name="Clear text",
                                    tooltip="Clear text. Stops any reading. You can undo this for 10 seconds.")
        self._faders = {w: QGraphicsOpacityEffect(w)
                        for w in (self.title, self.info, self.hint, self.btn_paste, self.btn_clear)}
        for w, effect in self._faders.items():
            effect.setEnabled(False)
            w.setGraphicsEffect(effect)
        QWidget.setTabOrder(self.edit, self.btn_paste)
        QWidget.setTabOrder(self.btn_paste, self.pill)
        QWidget.setTabOrder(self.pill, self.btn_clear)
        QWidget.setTabOrder(self.btn_clear, self.btn_back)

        # Motion: one 0-1 clock per open or close; opacity, grow (p), content (q) and slide derive from it.
        self._decel, self._exit = decelerate(), exit_curve()
        self._motion = ""
        self._start: dict[str, QRect] = {}
        self._t = Tween(self, t, 1.0, SLOW)
        self._t.on_change = self._on_motion

        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.timeout.connect(self._refresh)
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self._end_note)
        self._away_timer = QTimer(self)
        self._away_timer.setSingleShot(True)
        self._away_timer.timeout.connect(self._check_away)

        e = self.edit
        e.readKey.connect(self.read_from_shortcut)
        e.escapeKey.connect(lambda: self.closeRequested.emit("esc"))
        e.undoKey.connect(self.undoRequested.emit)
        e.pronunciationRequested.connect(self.pronunciationRequested.emit)
        e.sizeStep.connect(self.sizeStepRequested.emit)
        e.limitHit.connect(lambda: self.show_note("limit"))
        e.edited.connect(self._on_edited)
        e.selectionChanged.connect(lambda: self._count_timer.start(COUNT_DELAY_MS))
        e.document().contentsChanged.connect(self._on_contents)
        self.pill.clicked.connect(self.read_from_shortcut)
        self.btn_clear.clicked.connect(lambda: (self.undoRequested if self._undo_mode else self.clearRequested).emit())
        self.btn_back.clicked.connect(lambda: self.closeRequested.emit("back"))
        self.btn_paste.clicked.connect(self._paste)
        QGuiApplication.clipboard().dataChanged.connect(self._sync_paste)
        t.changed.connect(self._on_theme)

        self._apply_window_theme()
        self._layout_at(t.px(EXPANDED_W), TARGET_LINES)
        self._on_contents()
        self._refresh()

    # --- Opening and closing -------------------------------------------------------------------

    def open_at(self, rest: QRect, *, panel_visible: bool, panel_lines: int, prefill: tuple[str, int] | None,
                return_to: int) -> None:
        """Show the box over the panel's resting rect and take the foreground. Call it synchronously in the click
        or hotkey handler: Windows lets the window come to the front only then.

        `prefill` is the reading's text and the offset of its current sentence (None with no reading), and
        `return_to` the window to hand focus back to. On a box that is already open it just activates again."""
        s = self.session
        if s.phase in ("opening", "open"):
            self.activate()
            return
        if s.phase == "closing":
            self._finish_close()
            s = self.session
        s.phase, s.return_to = "opening", return_to
        self._panel_visible, self._panel_lines = panel_visible, panel_lines
        self._load_prefill(prefill)
        rect, lines, _fits = self._geometry(rest)
        self._rect = rect
        self._layout_at(rect.width(), lines)
        self.setGeometry(rect)
        self.edit.setReadOnly(False)
        self._start = self._grow_start(rest)
        self._motion = "open"
        self.setWindowOpacity(0.0)
        self._t.snap(0.0)  # frame 0: the box at the panel's size, the content hidden
        self.show()
        self._apply_scroll()
        s.was_active = self.activate()
        if not s.was_active:
            self.show_note("activation")
        self._t.to(1.0, SLOW)

    def close_animated(self, reason: str) -> None:
        """Close the box and keep its text: fade out, then hide and emit `closed(reason)`. "quit" hides at once."""
        if self.session.phase in ("closed", "closing"):
            return
        if self._motion == "open":
            self._t.snap(1.0)  # finish the opening first; the close starts from the open box
        self.session.phase = "closing"
        self._reason = reason
        self.edit.setReadOnly(True)
        self._cover(False)
        log.info("Compose closed (%s)", reason)
        if reason == "quit":
            self._finish_close()
            return
        if self._panel_visible:
            self._start = self._grow_start(self.ctl.panel.rest_rect())
        self._motion = "close"
        self._t.snap(0.0)
        self._t.to(1.0, CLOSE_MS)

    def _finish_close(self) -> None:
        self._motion = ""
        self.hide()
        self._grow(1.0, 1.0)
        self._end_note()
        reason, self.session = self._reason, ComposeSession()
        self.closed.emit(reason)

    def activate(self) -> bool:
        """Bring the box to the front with the caret in the text; True if Windows let it (trust only that)."""
        self.activateWindow()
        self.edit.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        win32.set_topmost(self.hwnd)
        if not self.is_foreground():
            win32.set_foreground_window(self.hwnd)
        return self.is_foreground()

    def is_foreground(self) -> bool:
        return win32.get_foreground_window() == self.hwnd

    def is_dismissed_by(self, hwnd: int) -> bool:
        """Whether `hwnd` coming to the front should close the box (light dismiss)."""
        return is_light_dismiss(self.session.phase, self.session.was_active, hwnd, win32.get_window_pid(hwnd),
                                win32.get_class_name(hwnd), own_pid=os.getpid())

    def reanchor(self, rest: QRect) -> None:
        """Fit the open box to the panel's resting rect at once, with no motion (after a screen, DPI, text size or
        theme change). If not even the panel's rows fit, ask to close; the text stays."""
        if self.session.phase not in ("opening", "open"):
            return
        rect, lines, fits = self._geometry(rest)
        if not fits:
            self.closeRequested.emit("screen")
            return
        if self._motion:
            self._t.snap(1.0)
        self._rect = rect
        self._layout_at(rect.width(), lines)
        self.setGeometry(rect)

    def _geometry(self, rest: QRect) -> tuple[QRect, int, bool]:
        """The box's rect over `rest`, its rows, and whether even the panel's rows fit on that screen."""
        t, panel = self.theme, self.ctl.panel
        screen = QGuiApplication.screenAt(rest.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        margin, bottom = t.px(12), panel._bottom_margin(screen)
        lh, chrome = t.line_height("reading"), chrome_height(t)
        lines = compose_lines(rest, area, lh=lh, chrome=chrome, panel_lines=self._panel_lines, margin=margin,
                              bottom_margin=bottom)
        width = max(panel.width(), panel.reading_width(True))
        snap_x = (self.ctl.settings.get("panel.position") or {}).get("snap_x")
        rect = compose_rect(rest, area, QSize(width, chrome + lines * lh), snap_x=snap_x, margin=margin,
                            bottom_margin=bottom)
        self._slide_dir = 1 if rest.center().y() > area.center().y() else -1
        fits = area.intersects(rest) and chrome + self._panel_lines * lh <= area.height()
        return rect, lines, fits

    def _grow_start(self, rest: QRect) -> dict[str, QRect]:
        """Frame 0 of the grow: the panel's own layout where the panel is, with the pill as the Play circle."""
        start = compose_layout(self.ctl.panel.width(), self._panel_lines, self.theme)
        start["pill"] = play_circle(self.theme, start["pill"].top())
        offset = rest.topLeft() - self._rect.topLeft()
        return {name: r.translated(offset) for name, r in start.items()}

    # --- Motion --------------------------------------------------------------------------------

    def _on_motion(self, v: float) -> None:
        if self._motion == "open":
            ms = v * SLOW
            opacity = self._decel.valueForProgress(min(1.0, ms / MEDIUM))
            if self._panel_visible:
                self._grow(self._decel.valueForProgress(v), min(1.0, max(0.0, (ms - FAST) / (SLOW - FAST))))
                if ms >= MEDIUM:
                    self._cover(True)
            else:  # the panel's own show: a fade and an 8 DIP slide from the nearer vertical edge
                self._slide(1.0 - self._decel.valueForProgress(v))
            if v >= 1.0:
                self._motion = ""
                if self.session.phase == "opening":
                    self.session.phase = "open"
                    if not self.is_foreground():  # you may have clicked away while it grew, which it had to ignore
                        self._away_timer.start(AWAY_SETTLE_MS)
        elif self._motion == "close":
            u = self._exit.valueForProgress(v)
            opacity = 1.0 - u
            if self._panel_visible:
                self._grow(1.0 - u, 1.0 - min(1.0, v * CLOSE_MS / FAST))
            else:  # the panel's own hide: a fade and a 4 DIP drift
                self._slide(0.5 * u)
            if v >= 1.0:
                self._finish_close()
                return
        else:
            return
        self.setWindowOpacity(1.0 if opacity >= 0.999 else max(0.0, opacity))

    def _grow(self, p: float, q: float) -> None:
        """Lay the parts out p of the way from the panel's layout to the box's, with the content at opacity q."""
        if p >= 1.0 and q >= 1.0:
            self._place(self._final)
            self.overlay.hide()
            for effect in self._faders.values():
                effect.setEnabled(False)
            self.pill.set_label_opacity(1.0)
            return
        lay = {name: _lerp_rect(self._start[name], self._final[name], p) for name in self._start}
        self._place(lay)
        text = self._final["text"].adjusted(0, 0, -self.theme.px(6), 0)  # the rows, not the scrollbar beside them
        rows = whole_rows(lay["well"], text, self.theme.line_height("reading"), self.edit.verticalScrollBar().value())
        self.overlay.set_state(self._final["well"], lay["well"], q, rows)
        self.overlay.show()
        for effect in self._faders.values():
            effect.setOpacity(q)
            effect.setEnabled(True)
        self.pill.set_label_opacity(q)

    def _slide(self, amount: float) -> None:
        self.move(self._rect.x(), self._rect.y() + round(self.theme.px(8) * amount) * self._slide_dir)

    def _cover(self, on: bool) -> None:
        if on != self._covered:
            self._covered = on
            self.ctl.panel.set_covered(on)

    # --- Layout --------------------------------------------------------------------------------

    def _layout_at(self, W: int, L: int) -> None:
        self._lines = L
        self._final = lay = compose_layout(W, L, self.theme)
        text = lay["text"]
        self.edit.setGeometry(text)
        self.fades.setGeometry(text.x(), text.y(), text.width() - self.theme.px(6), text.height())
        self._place(lay)
        self.update()

    def _place(self, lay: dict[str, QRect]) -> None:
        self.glyph.setGeometry(lay["glyph"])
        self.title.setGeometry(lay["title"])
        self.info.setGeometry(lay["info"])
        self.hint.setGeometry(lay["hint"])
        self.btn_back.place(lay["back"])
        self.btn_paste.place(lay["paste"])
        self.pill.place(lay["pill"])
        self.btn_clear.place(lay["clear"])

    # --- Text, prefill and the undo window ------------------------------------------------------

    def _load_prefill(self, prefill: tuple[str, int] | None) -> None:
        kind, text, pos = prefill_plan(self.user_text, self.edit.toPlainText(), prefill)
        if kind == "keep":
            return
        self.edit.load(text)
        self.user_text = False
        self._scroll_to = None
        if kind == "load":  # all of it highlighted, caret at the start: typing or pasting replaces it
            c = QTextCursor(self.edit.document())
            c.movePosition(QTextCursor.MoveOperation.End)
            c.setPosition(0, QTextCursor.MoveMode.KeepAnchor)
            self.edit.setTextCursor(c)
            self._scroll_to = ("row", pos)

    def _apply_scroll(self) -> None:
        """Scroll once the text is laid out at the box's width: a prefill puts its current sentence on row 3."""
        target, self._scroll_to = self._scroll_to, None
        if target is None:
            return
        kind, value = target
        if kind == "row":
            value = max(0, self.edit.row_of(value) - 2) * self.theme.line_height("reading")
        self.edit.verticalScrollBar().setValue(value)

    def has_draft(self) -> bool:
        """Text the user typed or changed is in the box (prefilled text left alone is not)."""
        return self.user_text and not self.edit.is_blank()

    def snapshot(self) -> ComposeSnapshot:
        c = self.edit.textCursor()
        return ComposeSnapshot(self.edit.toPlainText(), c.position(), c.anchor(),
                               self.edit.verticalScrollBar().value(), self.user_text)

    def restore_snapshot(self, snap: ComposeSnapshot) -> None:
        self.edit.load(snap.text)
        end = self.edit.document().characterCount() - 1
        c = QTextCursor(self.edit.document())
        c.setPosition(min(snap.anchor, end))
        c.setPosition(min(snap.position, end), QTextCursor.MoveMode.KeepAnchor)
        self.edit.setTextCursor(c)
        self.user_text = snap.user_text
        self._scroll_to = ("value", snap.scroll)
        if self.isVisible():
            self._apply_scroll()

    def clear_text(self) -> None:
        """Empty the box and its undo history."""
        self.edit.load("")
        self.user_text = False
        self._scroll_to = None
        self._end_note()

    def set_undo_mode(self, on: bool) -> None:
        """During the undo window the Clear button reads Undo and the header says "Text cleared"."""
        if on == self._undo_mode:
            return
        self._undo_mode = self.edit.undo_mode = on
        if on:
            self.btn_clear.set_content(G_UNDO, "Undo", "Undo clear")
            self.btn_clear.set_tooltip("Undo clear (Ctrl+Z)")
        else:
            self.btn_clear.set_content(G_CLEAR, "Clear", "Clear text")
            self.btn_clear.set_tooltip("Clear text. Stops any reading. You can undo this for 10 seconds.")
        self._sync_actions()
        self._refresh()

    def read_from_shortcut(self) -> None:
        """Read the highlighted part, or all of it (Ctrl+`, Ctrl+Enter and the pill)."""
        full = self.edit.toPlainText()
        if not full.strip():
            self.show_note("empty")
            return
        self.readRequested.emit(*read_scope(self.edit.textCursor().selection().toPlainText(), full))

    def on_speed(self, v: float, _note: str = "") -> None:
        self._speed = v
        self._refresh()

    def _paste(self) -> None:
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.edit.paste()

    def _on_edited(self) -> None:
        self.user_text = True
        if self._note_kind:  # a note lasts until the next edit
            self._end_note()
        self.textEdited.emit()

    def _on_contents(self) -> None:
        self._sync_actions()
        self._count_timer.start(COUNT_DELAY_MS)

    def _sync_actions(self) -> None:
        self.pill.setEnabled(not self.edit.is_blank())
        self.btn_clear.setEnabled(self._undo_mode or not self.edit.document().isEmpty())
        self._sync_paste()

    def _sync_paste(self) -> None:
        """The Paste chip shows while the box is empty and the clipboard has text (only its format is checked)."""
        show = self.edit.document().isEmpty()
        if show:
            mime = QGuiApplication.clipboard().mimeData()
            show = mime is not None and mime.hasText()
        self.btn_paste.setVisible(show)

    def _refresh(self) -> None:
        """The header count, the hint's wording and the pill's tooltip, for all the text or the highlighted part."""
        self._count_timer.stop()
        full = self.edit.toPlainText()
        text, scope = read_scope(self.edit.textCursor().selection().toPlainText(), full)
        partial = scope == "selection"
        if self._undo_mode:
            self.info.set_text("Text cleared")
        elif self._note_kind == "nothing":
            self.info.set_text("")  # no count beside "nothing to read", whatever page numbers the text holds
        else:
            self.info.set_text(count_label(word_count(text), self._speed, partial))
        if not self._note_kind:
            self.hint.set_content("hint", HINT_PARTIAL if partial else HINT)
        self.pill.set_tooltip("Read the highlighted part aloud (Ctrl+Enter)" if partial
                              else "Read it aloud (Ctrl+Enter)")

    # --- Notes ---------------------------------------------------------------------------------

    def show_note(self, kind: str) -> None:
        """Replace the hint with a note for 6 s or until the next edit, beeping where the note calls for it:
        "empty", "nothing", "limit" (at most one beep every 2 s) or "activation"."""
        text, beep = NOTES[kind]
        self._note_kind = kind
        self.hint.set_content("note", text)
        self._note_timer.start(NOTE_MS)
        if kind == "nothing":
            self._refresh()
        if beep and kind == "limit":
            now = time.monotonic()
            beep, self._limit_beep_at = now - self._limit_beep_at >= LIMIT_BEEP_S, now
        if beep:
            win32.message_beep("ok")

    def _end_note(self) -> None:
        self._note_timer.stop()
        self._note_kind = ""
        self._refresh()  # the note crossfades straight into the hint the selection calls for

    def _back_tip(self) -> str:
        if self._panel_visible:
            return "Back to the panel (Esc). Your text stays here."
        return "Close (Esc). Your text stays here."

    # --- Theme ---------------------------------------------------------------------------------

    def _apply_window_theme(self) -> None:
        t = self.theme
        win32.dwm_set_corner(self.hwnd)
        win32.dwm_set_dark(self.hwnd, t.dark)
        win32.dwm_disable_transitions(self.hwnd)
        ink = t.color("TextPrimary")
        win32.dwm_set_border_color(self.hwnd, (ink.red(), ink.green(), ink.blue()) if t.high_contrast else None)

    def _on_theme(self) -> None:
        self._apply_window_theme()
        self.edit.apply_theme()
        for w in self.findChildren(QWidget):
            relayout = getattr(w, "relayout", None)
            if relayout:
                relayout()
        if self.session.phase in ("opening", "open"):
            self.reanchor(self.ctl.panel.rest_rect())
        else:
            self._layout_at(self._final["window"].width(), self._lines)
        self._refresh()

    # --- Events --------------------------------------------------------------------------------

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._on_front()
            else:  # backstop for light dismiss: see who has the foreground once activation settles
                self._away_timer.start(AWAY_SETTLE_MS)
        super().changeEvent(event)

    def nativeEvent(self, event_type, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_MOUSEACTIVATE and self.session.phase == "closing":
            return True, MA_NOACTIVATE  # a click on the fading box never brings it back to the front
        if msg.message in (WM_NCACTIVATE, WM_ACTIVATEAPP) and msg.wParam:
            # After a refused activation Qt already counts the box as active, so the click that brings it to the
            # front changes nothing in Qt; ask Windows once the activation has settled.
            QTimer.singleShot(0, self._on_front)
        return super().nativeEvent(event_type, message)

    def _on_front(self) -> None:
        """The box may have come to the front: from then on light dismiss applies and the activation note goes."""
        s = self.session
        if s.phase in ("opening", "open") and self.is_foreground():
            s.was_active = True
            if self._note_kind == "activation":
                self._end_note()

    def _check_away(self) -> None:
        if self.is_dismissed_by(win32.get_foreground_window()):
            self.closeRequested.emit("away")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Keys a focused button passes on.
        if event.key() == Qt.Key.Key_Escape:
            self.closeRequested.emit("esc")
        elif (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
              and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.read_from_shortcut()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mousePressEvent(self, event) -> None:
        if self._final["well"].contains(event.position().toPoint()):  # the well's padding belongs to the text
            self.edit.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()

    def closeEvent(self, event) -> None:
        if self.session.phase in ("opening", "open"):
            event.ignore()  # Alt+F4 is Back
            self.closeRequested.emit("back")
        else:  # already on its way out: never hold up the close Qt sends every window when the app quits
            event.accept()

    def paintEvent(self, _event) -> None:
        t = self.theme
        c = t.reading_colors()
        radius = t.px(6)
        well = QRectF(self._final["well"])
        p = QPainter(self)
        p.fillRect(self.rect(), t.color("Surface"))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(well, radius, radius)
        p.fillPath(path, c["background"])
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(c["edge"], 0))
        p.drawRoundedRect(well.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
