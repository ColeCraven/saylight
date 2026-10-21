"""The reading card: follow-along text with a sentence band and a word pill.

Design, based on reading research with dyslexic readers:
* a short continuous window of the text, with the current line held on row 2 (row 3 when expanded), so the
  reader can look back (Schotter 2014) and read ahead of the voice (Schneps 2019);
* a soft band behind the current sentence (keeps your place) and a pill behind the current word;
* earlier sentences dimmed to ~5:1, words not yet read never dimmed, no bold/underline/size changes;
* text that is shown but not read aloud (citations, notes, a reference list) in the `skipped` colour: as light
  as the earlier sentences but with the opposite hue cast, so it never looks already read, and never italic;
* the wheel scrolls back through the text (a "Back to current" chip returns at once; otherwise auto-follow
  resumes 4 s after the last scroll, waiting while the pointer is over the card), Ctrl+wheel changes text size,
  and a click on any sentence reads from there (the panel forwards clicks, so the card never takes focus);
* with no text yet, the whole card is one click target that opens Type or paste; message lines can hold action
  chips ("Type or paste text", "Open a file", "Undo"), which are hit-tested here;
* in text mode, a chip in the top-right corner (over row 1, which holds text already read; at the very start of a
  text it waits rather than cover anything unread) and a chip on the blank row before the notes and before the
  reference list ("Read the notes too").

Each paragraph has a layout of its own, laid out in order only as far as needed and the rest while idle, and
only the rows on screen are painted: a 160-page document shows in a few milliseconds and paints in under one.
"""

from __future__ import annotations

import bisect
import re
import time
import unicodedata
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QTextLayout,
                           QTextLine, QTextOption)
from PySide6.QtWidgets import QWidget

from ..theme import composite
from .controls import ACTION_PARTS, ActionState, Part, action_label, hc_outline, measure_parts, paint_parts
from .motion import FAST, MEDIUM, Tween, decelerate, mix, with_alpha

FOLLOW_RESUME_MS = 4000
SIZES = ("small", "medium", "large", "xlarge")
CHIP_TEXT = "Back to current"
LABEL_FADE_MS = 120  # a chip whose label changes crossfades

# Text that is shown but not read aloud (Document.regions kinds), and what each says when pointed at.
DIMMED = ("skip", "aside", "refs")
REGION_TIPS = {
    "skip": "Shown but not read aloud. You can change what's skipped in Settings, on the Voice tab.",
    "aside": "A note, caption or table: shown but not read aloud. Choose Read the notes too to hear them.",
    "refs": "The reference list: shown but not read aloud. Choose Read the references too to hear it.",
}
SAID_TIP = "Read aloud as “{spoken}”."
# Section chips: (label, tooltip) while the section is skipped, then while it is read.
SECTION_CHIPS = {
    "notes": (("Read the notes too", "Read the notes after the main text, for this file only."),
              ("Skip the notes", "Skip them again, for this file.")),
    "refs": (("Read the references too", "Read the reference list after the main text, for this file only."),
             ("Skip the references", "Skip it again, for this file.")),
}
_REGION_SECTION = {"aside": "notes", "refs": "refs"}

# Spaces that must not break a row: around relations in statistics ("p = .018", "(n = 42)"), inside
# "F(2, 57)", "et al." and figure, table and equation references. They become no-break spaces in the layout
# only (same length, so every offset holds; Qt still applies word spacing to them), and the spoken text is
# untouched. Number lists are not glued: "2, 57" only when a ")" follows.
_GLUE = re.compile(r" (?=[=<>≤≥≈±×] )|(?<= [=<>≤≥≈±×]) |(?<=\d,) (?=\d+\))|(?<=\bet) (?=al\.)"
                   r"|(?<=\bFig\.) (?=\d)|(?<=\bFigure) (?=\d)|(?<=\bTable) (?=\d)|(?<=\bEq\.) (?=\d)")


def _x(line, pos: int) -> float:
    v = line.cursorToX(pos)
    return float(v[0] if isinstance(v, tuple) else v)


def word_bounds(text: str, pos: int) -> tuple[int, int] | None:
    """The word around `pos` in `text` (a run without spaces), with leading and trailing punctuation trimmed, as
    (start, end); None on a space or on punctuation alone. Inner punctuation stays: "e.g", "don't", "COVID-19"."""
    if not (0 <= pos < len(text)) or text[pos].isspace():
        return None
    a = b = pos
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    while b < len(text) and not text[b].isspace():
        b += 1
    while a < b and unicodedata.category(text[a]).startswith("P"):
        a += 1
    while b > a and unicodedata.category(text[b - 1]).startswith("P"):
        b -= 1
    return (a, b) if a < b and a <= pos < b else None


def color_runs(layers: list[tuple[int, int, QColor]], lo: int, hi: int) -> list[tuple[int, int, QColor]]:
    """Non-overlapping (start, end, colour) runs within [lo, hi) from colour layers, later layers winning: a
    skipped citation inside an earlier (dimmed) sentence becomes a run of its own."""
    cuts = sorted({lo, hi} | {x for a, b, _ in layers for x in (a, b) if lo < x < hi})
    runs: list[tuple[int, int, QColor]] = []
    for a, b in zip(cuts, cuts[1:]):
        color = None
        for start, end, c in layers:
            if start <= a and b <= end:
                color = c
        if color is None:
            continue
        if runs and runs[-1][1] == a and runs[-1][2] == color:
            runs[-1] = (runs[-1][0], b, color)
        else:
            runs.append((a, b, color))
    return runs


@dataclass
class _Section:
    name: str      # "notes" | "refs"
    offset: int    # where its heading starts in the shown text
    read: bool     # read aloud (for this file) rather than skipped


def document_sections(doc) -> list[_Section]:
    """The notes and the reference list of a document, from its skipped regions or, when it says where they are,
    its `sections` mapping (name -> offset; a section listed there without a region is read aloud)."""
    found: dict[str, _Section] = {}
    for r in getattr(doc, "regions", None) or ():
        name = _REGION_SECTION.get(r.kind)
        if name is not None and (name not in found or r.start < found[name].offset):
            found[name] = _Section(name, r.start, False)
    sections = getattr(doc, "sections", None)
    for name, offset in (dict(sections) if sections else {}).items():
        if name in SECTION_CHIPS and name not in found:
            found[name] = _Section(name, int(offset), True)
    return sorted(found.values(), key=lambda s: s.offset)


class SentenceView(QWidget):
    """Paints its own rounded card; the panel positions it and forwards drag/click gestures."""

    sizeStepRequested = Signal(int)
    pronunciationRequested = Signal(str)  # a word to change how it is said (see request_pronunciation)

    def __init__(self, theme, settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.settings = settings
        self.doc = None
        self.sentence = -1
        self.lo = self.hi = 0
        self.style = "active"          # active | muted | paused | stopped | finished
        self.expanded = False
        self.messages: list[list[Part]] | None = [[]]
        self._secondary_from = 1  # message lines from this index on are drawn in the secondary colour
        self._layout: _Lines | None = None
        self._layout_key = None
        self._starts: list[int] = []
        self._dim: list[tuple[int, int, str]] = []    # skipped regions, sorted by start
        self._dim_starts: list[int] = []
        self._said: list[tuple[int, int]] = []
        self._sections: list[_Section] = []
        self._hover_sentence: int | None = None
        self._card_hover = False
        self._hits: dict[str, QRectF] = {}  # action chips on the current message lines, as last painted
        self._labels: dict[str, str] = {}   # action chip label overrides (set_action_label)
        self._action_hover = {key: Tween(self, theme, 0.0, FAST) for key in ACTION_PARTS}
        self._action_press = {key: Tween(self, theme, 0.0, FAST) for key in ACTION_PARTS}
        self._action_enabled = dict.fromkeys(ACTION_PARTS, True)
        self._chip_hover: dict[str, Tween] = {}   # text-mode chips: "corner:<key>", "section:<name>"
        self._chip_press: dict[str, Tween] = {}
        self._chip_enabled: dict[str, bool] = {}
        self._corner: tuple[str, str, bool, str] | None = None  # (key, label, clickable, tooltip)
        self._corner_prev = ""
        self._corner_fade = Tween(self, theme, 1.0, LABEL_FADE_MS)
        self._section_prev: dict[str, str] = {}
        self._section_fade: dict[str, Tween] = {}
        self._manual = False
        self.scroll = Tween(self, theme, 0.0, 160, decelerate())
        self.pill_x = Tween(self, theme, 0.0, FAST, decelerate())
        self.pill_w = Tween(self, theme, 0.0, FAST, decelerate())
        self.pill_a = Tween(self, theme, 0.0, FAST)
        self.swap = Tween(self, theme, 1.0, MEDIUM, decelerate())
        self._pill_line = -1
        self._prev_messages: list[list[Part]] | None = None
        self._prev_secondary_from = 1
        self._follow_timer = QTimer(self)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.timeout.connect(self._resume_follow)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(0)
        self._layout_timer.timeout.connect(self._layout_step)
        theme.changed.connect(self.invalidate)
        settings.changed.connect(self._on_setting)

    # --- Geometry ----------------------------------------------------------------------------

    def _on_setting(self, key: str) -> None:
        if key in ("text_size", "reading_tint", "reading_font", "spacing", "highlight"):
            self.invalidate()

    def line_height(self) -> int:
        return self.theme.line_height("reading")

    def pad_x(self) -> int:
        return self.theme.px(14)

    def pad_y(self) -> int:
        return self.theme.px(10)

    def visible_lines(self) -> int:
        return 8 if self.expanded else 4

    def focus_row(self) -> int:
        """Which visible row holds the current line (0-based)."""
        return 2 if self.expanded else 1

    def preferred_height(self) -> int:
        return self.visible_lines() * self.line_height() + 2 * self.pad_y()

    def text_width(self) -> float:
        return max(10.0, self.width() - 2 * self.pad_x())

    def set_expanded(self, on: bool) -> None:
        if on != self.expanded:
            self.expanded = on
            self.invalidate()

    def invalidate(self) -> None:
        """Repaint after a theme, setting or size change; a new layout only when the text's font or width
        changed."""
        self._ensure_layout()
        self._update_targets(animate=False)
        self.update()

    def resizeEvent(self, event) -> None:
        self.invalidate()
        super().resizeEvent(event)

    # --- Content ------------------------------------------------------------------------------

    def set_document(self, doc) -> None:
        """Show a document. One with the same shown text as the current one (a rebuild after a change to what is
        read aloud) keeps the place, the scroll and the layout; section chips whose label changes crossfade."""
        rebuilt = doc is not None and self.doc is not None and doc is not self.doc and doc.text == self.doc.text
        old_sections = {s.name: s for s in self._sections}
        self.doc = doc
        self._starts = [s.start for s in doc.sentences] if doc else []
        self._index_regions()
        if rebuilt:
            for s in self._sections:
                was = old_sections.get(s.name)
                if was is not None and was.read != s.read:
                    self._section_prev[s.name] = SECTION_CHIPS[s.name][was.read][0]
                    fade = self._section_fade.setdefault(s.name, Tween(self, self.theme, 1.0, LABEL_FADE_MS))
                    fade.snap(0.0)
                    fade.to(1.0)
            if not (0 <= self.sentence < len(doc.sentences)):
                self.sentence = -1
            self._describe()
            self.update()
            return
        self.sentence = -1
        self.lo = self.hi = 0
        self._manual = False
        self.scroll.snap(0.0)
        self.pill_a.snap(0.0)
        self._ensure_layout()
        self._describe()

    def _index_regions(self) -> None:
        doc = self.doc
        regions = list(getattr(doc, "regions", None) or ()) if doc is not None else []
        if not regions and doc is not None and callable(getattr(doc, "skipped", None)):
            regions = [_Region(a, b, "skip") for a, b in doc.skipped()]
        self._dim = sorted((r.start, r.end, r.kind) for r in regions if r.kind in DIMMED and r.end > r.start)
        self._dim_starts = [a for a, _, _ in self._dim]
        self._said = sorted((r.start, r.end) for r in regions if r.kind == "said")
        self._sections = document_sections(doc) if doc is not None else []

    def show_messages(self, lines: list[list[Part]], secondary_from: int = 1) -> None:
        """Show message lines; lines before `secondary_from` use the text colour, the rest the secondary one."""
        if lines == self.messages and secondary_from == self._secondary_from:
            return
        self._prev_messages = self.messages if self.messages else None
        self._prev_secondary_from = self._secondary_from
        self.messages = lines
        self._secondary_from = secondary_from
        self._hits = {}
        self.swap.snap(0.0)
        self.swap.to(1.0)
        self._describe()
        self.update()

    def show_text(self) -> None:
        if self.messages is not None:
            self._prev_messages = self.messages or None
            self._prev_secondary_from = self._secondary_from
            self.messages = None
            self._hits = {}
            self._ensure_layout()
            self._update_targets(animate=False)
            self.swap.snap(0.0)
            self.swap.to(1.0)
            self._describe()
            self.update()

    def set_action_label(self, key: str, text: str) -> None:
        """Give an action chip on the message lines its label for now (the Continue chip names its file)."""
        if self._labels.get(key) != text:
            self._labels[key] = text
            self._hits = {}
            self._describe()
            self.update()

    def set_corner_chip(self, key: str | None, label: str = "", *, clickable: bool = True, tooltip: str = "") -> None:
        """The chip in the top-right corner (text mode only): "Start from the beginning", "Back to page 14",
        "Saylight will remember your place" (not clickable). It appears and goes at once; a new label on a
        chip already showing crossfades. None removes it. The panel decides which chip has the slot."""
        new = (key, label, clickable, tooltip) if key else None
        if new == self._corner:
            return
        if new is not None and self._corner is not None and label != self._corner[1]:
            self._corner_prev = self._corner[1]
            self._corner_fade.snap(0.0)
            self._corner_fade.to(1.0)
        else:
            self._corner_prev = ""
            self._corner_fade.snap(1.0)
        self._corner = new
        self._describe()
        self.update()

    def set_style(self, style: str) -> None:
        if style != self.style:
            self.style = style
            self._update_targets(animate=True)
            self.update()

    def set_position(self, sentence: int, lo: int, hi: int) -> None:
        if self.doc is None or not (0 <= sentence < len(self.doc.sentences)):
            return
        if lo < 0 or hi < 0:  # no word timing at this moment
            lo = hi = 0
        if (sentence, lo, hi) == (self.sentence, self.lo, self.hi):
            return
        self.sentence, self.lo, self.hi = sentence, lo, hi
        self._ensure_layout()
        self._update_targets(animate=True)
        self.update()

    def _describe(self) -> None:
        """The chips on screen, for screen readers: "Actions: Type or paste text, Open a file."."""
        labels = []
        if self.messages is not None:
            labels = [action_label(s, self._labels) for line in self.messages for kind, s in line if kind == "action"]
        elif self.doc is not None:
            if self._corner is not None and self._corner[2]:
                labels.append(self._corner[1])
            labels += [self._section_label(s) for s in self._sections]
        self.setAccessibleDescription(f"Actions: {', '.join(labels)}." if labels else "")

    # --- Hit testing and gestures (called by the panel) ------------------------------------------

    def _text_pos(self, pos: QPointF, edge=QTextLine.CursorPosition.CursorBetweenCharacters) -> int | None:
        """The text position under `pos` on a text row (None off the text), with Qt's cursor `edge` rule."""
        if self.messages is not None or self._layout is None or self.doc is None:
            return None
        lay = self._layout
        y = pos.y() - self.pad_y() + self.scroll.value
        x = pos.x() - self.pad_x()
        row = int(y // self.line_height())
        if row < 0:
            return None
        lay.ensure_rows(row + 1)
        if row >= len(lay.rows):
            return None
        line = lay.lineAt(row)
        if x < -4 or x > line.naturalTextWidth() + 4:
            return None
        c = line.xToCursor(max(0.0, x), edge)
        return c[0] if isinstance(c, tuple) else c

    def _char_at(self, pos: QPointF) -> int | None:
        """The character whose glyph is under `pos`, or None."""
        c = self._text_pos(pos, QTextLine.CursorPosition.CursorOnCharacter)
        if c is None or not (0 <= c < len(self.doc.text)):
            return None
        line = self._layout.lineAt(self._line_of(c))
        x = pos.x() - self.pad_x()
        x1, x2 = sorted((_x(line, c), _x(line, c + 1)))
        return c if x1 - 1 <= x <= x2 + 1 else None

    def _region_at(self, c: int) -> tuple[int, int, str] | None:
        i = bisect.bisect_right(self._dim_starts, c) - 1
        if i >= 0 and self._dim[i][0] <= c < self._dim[i][1]:
            return self._dim[i]
        return None

    def sentence_at(self, pos: QPointF) -> int | None:
        """The sentence a click at `pos` reads from. Not on dimmed notes or references, which are never read
        (citations inside a sentence belong to it), and not past the last sentence read aloud."""
        if not self._starts:
            return None
        c = self._text_pos(pos)
        if c is None:
            return None
        region = self._region_at(min(c, len(self.doc.text) - 1)) if self.doc.text else None
        if region is not None and region[2] != "skip":
            return None
        if c > self.doc.sentences[-1].end:
            return None
        return max(0, bisect.bisect_right(self._starts, c) - 1)

    def word_at(self, pos: QPointF) -> tuple[int, int] | None:
        """The shown offsets of the word under the pointer, punctuation trimmed; None in message mode or off a
        word. The context sentence is doc.sentences[doc.sentence_at_offset(start)]."""
        c = self._char_at(pos)
        return word_bounds(self.doc.text, c) if c is not None else None

    def request_pronunciation(self, pos: QPointF) -> bool:
        """Emit pronunciationRequested with the word under `pos`; False when there is none."""
        span = self.word_at(pos)
        if span is None:
            return False
        self.pronunciationRequested.emit(self.doc.text[span[0]:span[1]])
        return True

    def tip_at(self, pos: QPointF) -> str:
        """The tooltip for what is under `pos`: a text-mode chip, or text that is dimmed or read differently."""
        key = self.action_at(pos)
        if key is not None and key.startswith("corner:"):
            return self._corner[3]
        if key is not None and key.startswith("section:"):
            section = next(s for s in self._sections if s.name == key[8:])
            return SECTION_CHIPS[section.name][section.read][1]
        c = self._char_at(pos)
        if c is None:
            return ""
        region = self._region_at(c)
        if region is not None:
            return REGION_TIPS[region[2]]
        spoken = self._said_at(c)
        return SAID_TIP.format(spoken=spoken) if spoken else ""

    def _said_at(self, c: int) -> str:
        """What is said instead of the shown text at offset `c`, from the sentence's alignment edits."""
        i = bisect.bisect_right(self._said, (c, float("inf"))) - 1
        if i < 0 or not (self._said[i][0] <= c < self._said[i][1]) or not self._starts:
            return ""
        s = self.doc.sentences[max(0, bisect.bisect_right(self._starts, c) - 1)]
        for edit in getattr(getattr(s, "align", None), "edits", None) or ():
            if edit.kind in ("say", "pron") and s.start + edit.start <= c < s.start + edit.end:
                return edit.spoken.strip()
        return ""

    def set_hover(self, sentence: int | None, allow_current: bool = False) -> None:
        """Outline the sentence a click would read from (the current one only when `allow_current`)."""
        if sentence == self.sentence and not allow_current:
            sentence = None
        self.setCursor(Qt.CursorShape.PointingHandCursor if sentence is not None else Qt.CursorShape.ArrowCursor)
        if sentence != self._hover_sentence:
            self._hover_sentence = sentence
            self.update()

    def track_pointer(self, pos: QPointF | None, *, can_seek: bool = True, allow_current: bool = False) -> str | None:
        """Text-mode hover in one call: a chip under `pos` takes hover and the pointing hand; otherwise the
        sentence a click reads from is outlined (when `can_seek`). Returns the chip's key, "sentence" or None;
        None for `pos` clears every hover."""
        chip = self.action_at(pos) if pos is not None and self.messages is None else None
        if chip is not None and chip not in self._chip_hover:
            self.set_action_state(chip, False, False, True)
        for key in list(self._chip_hover):
            self.set_action_state(key, key == chip, key == chip and self._chip_press[key].target > 0,
                                  self._chip_enabled.get(key, True))
        if chip is not None:
            self.set_hover(None)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            return chip
        target = self.sentence_at(pos) if pos is not None and can_seek else None
        self.set_hover(target, allow_current=allow_current)
        return "sentence" if self._hover_sentence is not None else None

    def action_at(self, pos: QPointF) -> str | None:
        """The chip under `pos`: on message lines an action key ("type", "open", "undo"…); in text mode
        "corner:<key>" or "section:notes" / "section:refs". None elsewhere."""
        if self.messages is not None:
            return next((key for key, r in self._hits.items() if r.contains(pos)), None)
        corner = self.corner_rect()
        if corner is not None and corner.contains(pos):
            return f"corner:{self._corner[0]}" if self._corner[2] else None
        for section, rect in self._section_rects():
            if rect.contains(pos):
                return f"section:{section.name}"
        return None

    def set_action_state(self, key: str, hover: bool, pressed: bool, enabled: bool) -> None:
        """Show a chip hovered or pressed; a disabled one takes neither. Any chip action_at() returns."""
        if key in ACTION_PARTS:
            hovers, presses, states = self._action_hover, self._action_press, self._action_enabled
        else:
            hovers, presses, states = self._chip_hover, self._chip_press, self._chip_enabled
            if key not in hovers:
                hovers[key], presses[key] = Tween(self, self.theme, 0.0, FAST), Tween(self, self.theme, 0.0, FAST)
        if enabled != states.get(key, True):
            states[key] = enabled
            self.update()
        for tween, on in ((hovers[key], hover), (presses[key], pressed)):
            if enabled:
                tween.to(1.0 if on else 0.0)
            else:
                tween.snap(0.0)

    def set_card_hover(self, on: bool) -> None:
        """Outline the whole card, with an I-beam, while a click on it opens Type or paste."""
        if on:
            self.setCursor(Qt.CursorShape.IBeamCursor)
        elif self._card_hover:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if on != self._card_hover:
            self._card_hover = on
            self.update()

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120
        if not steps:
            event.ignore()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.sizeStepRequested.emit(1 if steps > 0 else -1)
            event.accept()
            return
        if self._layout is None or self.messages is not None:
            event.ignore()
            return
        lh = self.line_height()
        max_scroll = max(0.0, (self._layout.lineCount() - self.visible_lines()) * lh)
        target = max(0.0, min(max_scroll, self.scroll.target - round(steps) * lh))
        event.accept()
        if target == self.scroll.target:  # nothing to scroll to: keep following, no "Back to current" chip
            return
        self._manual = True
        self.scroll.to(target)
        self._follow_timer.start(FOLLOW_RESUME_MS)

    def _resume_follow(self) -> None:
        if self.underMouse():  # still re-reading: wait for the pointer to leave
            self._follow_timer.start(FOLLOW_RESUME_MS)
            return
        self.follow_now()

    def follow_now(self) -> None:
        """Scroll back to the current line and follow the voice again."""
        self._follow_timer.stop()
        self._manual = False
        self._update_targets(animate=True)
        self.update()

    def chip_visible(self) -> bool:
        """The "Back to current" chip shows while the reader has scrolled away from the voice."""
        return self._manual and self.messages is None and self.doc is not None

    def chip_rect(self) -> QRectF:
        t = self.theme
        w = QFontMetricsF(t.font("small")).horizontalAdvance(CHIP_TEXT) + 2 * t.px(10)
        h = t.px(24)
        return QRectF(self.width() - t.px(8) - w, self.height() - t.px(8) - h, w, h)

    def corner_rect(self) -> QRectF | None:
        """Where the top-right chip is, 8 DIP in from the card's edges; None when there is none. The top row holds
        text already read, except at the very start of a text: then the chip waits while it would cover any."""
        if self._corner is None or self.messages is not None or self.doc is None:
            return None
        t = self.theme
        w = QFontMetricsF(t.font("small")).horizontalAdvance(self._corner[1]) + 2 * t.px(10)
        rect = QRectF(self.width() - t.px(8) - w, t.px(8), w, t.px(24))
        return None if self._covers_unread(rect) else rect

    def _covers_unread(self, rect: QRectF) -> bool:
        """Whether `rect` lies over text still to be read (from the current word, or the current sentence)."""
        lay, doc = self._layout, self.doc
        if lay is None or self.style == "finished":
            return False
        if 0 <= self.sentence < len(doc.sentences):
            unread = self._word_range()[0] if self.hi > self.lo else doc.sentences[self.sentence].start
        else:
            unread = 0
        lh = self.line_height()
        first = int((rect.top() - self.pad_y() + self.scroll.value) // lh)
        last = int((rect.bottom() - self.pad_y() + self.scroll.value) // lh)
        for row in range(max(0, first), last + 1):
            lay.ensure_rows(row + 1)
            if row >= len(lay.rows):
                break
            line = lay.lineAt(row)
            if line.textStart() + line.textLength() <= unread:
                continue
            x1 = self.pad_x() + line.cursorToX(max(unread, line.textStart()))
            x2 = self.pad_x() + line.naturalTextWidth()
            if x1 < rect.right() and x2 > rect.left():
                return True
        return False

    # --- Sections -------------------------------------------------------------------------------

    def _section_label(self, section: _Section) -> str:
        return SECTION_CHIPS[section.name][section.read][0]

    def _section_row(self, section: _Section) -> int:
        """The blank row before the section's heading (the heading's own row if there is no blank one)."""
        line = self._line_of(section.offset)
        if line > 0 and self._layout.lineAt(line - 1).textLength() <= 1:
            return line - 1
        return line

    def _section_rects(self, rise: float = 0.0, *, drawn: bool = False) -> list[tuple[_Section, QRectF]]:
        """The section chips on screen: right-aligned to the text, centred on their row. One under a corner chip
        is left out, and so is one only partly on screen (unless `drawn`: painting includes those)."""
        if self.messages is not None or self._layout is None or not self._sections:
            return []
        t = self.theme
        lh = self.line_height()
        fm = QFontMetricsF(t.font("small"))
        h = min(t.px(24), lh - t.px(2))
        origin = self._origin(rise)
        inner = self._inner_rect()
        blockers = [r for r in (self.corner_rect(), self.chip_rect() if self.chip_visible() else None) if r]
        out = []
        reach = self._visible_range()[1]
        for section in self._sections:
            if section.offset > reach:  # below the rows on screen (and maybe not laid out yet)
                break
            row = self._section_row(section)
            w = fm.horizontalAdvance(self._section_label(section)) + 2 * t.px(10)
            top = origin.y() + row * lh + (lh - h) / 2
            rect = QRectF(self.width() - self.pad_x() - w, top, w, h)
            if any(rect.intersects(b) for b in blockers):
                continue
            on_screen = rect.top() >= inner.top() - 0.5 and rect.bottom() <= inner.bottom() + 0.5
            if on_screen or (drawn and rect.intersects(inner)):
                out.append((section, rect))
        return out

    # --- Layout ---------------------------------------------------------------------------------

    def _ensure_layout(self) -> None:
        if self.doc is None:
            self._layout = None
            return
        s = self.settings
        font = self.theme.font("reading")
        key = (self.doc.text, round(self.text_width()), self.theme.k, self.line_height(), font.toString(),
               s.get("text_size"), s.get("reading_font"), s.get("spacing"))
        if key == self._layout_key and self._layout is not None:
            return
        self._layout_key = key
        self._layout = _Lines(self.doc.text, font, self.text_width(), self.line_height())
        self._layout_timer.start()  # the rest of the document is laid out while idle

    def _line_of(self, pos: int) -> int:
        return self._layout.line_of(pos) if self._layout is not None else 0

    def _layout_step(self) -> None:
        if self._layout is not None and not self._layout.done:
            if self._layout.step():
                self.update()  # the bottom fade and the scroll range are exact now
            else:
                self._layout_timer.start()

    def _word_range(self) -> tuple[int, int]:
        s = self.doc.sentences[self.sentence]
        return s.start + self.lo, s.start + self.hi

    def _update_targets(self, animate: bool) -> None:
        lay = self._layout
        if lay is None or self.doc is None or self.sentence < 0:
            return
        lh = self.line_height()
        w0, w1 = self._word_range()
        anchor = w0 if self.hi > self.lo else self.doc.sentences[self.sentence].start
        cur_line = self._line_of(anchor)
        lay.ensure_rows(cur_line + self.visible_lines() + 1)  # the rows below it, so the clamp is exact
        n, vis = lay.lineCount(), self.visible_lines()
        top = max(0, min(cur_line - self.focus_row(), n - vis))
        if not self._manual:
            if animate:
                self.scroll.to(float(top * lh))
            else:
                self.scroll.snap(float(top * lh))
        show_pill = (self.settings.get("highlight") == "word" and self.hi > self.lo
                     and self.style in ("active", "paused"))
        if not show_pill:
            self.pill_a.to(0.0)
            return
        line = lay.lineAt(cur_line)
        x1 = _x(line, w0)
        x2 = _x(line, min(w1, line.textStart() + line.textLength()))
        pad = self.theme.k * 3
        if cur_line != self._pill_line or not animate or self.pill_a.value == 0:
            self.pill_x.snap(x1 - pad)
            self.pill_w.snap(max(4.0, x2 - x1 + 2 * pad))
            self.pill_a.snap(0.0)
            self.pill_a.to(1.0)
        else:
            self.pill_x.to(x1 - pad)
            self.pill_w.to(max(4.0, x2 - x1 + 2 * pad))
            self.pill_a.to(1.0)
        self._pill_line = cur_line

    def _visible_range(self) -> tuple[int, int]:
        """The text on the rows on screen, with a row of margin either side (for the scroll and the rise)."""
        lay = self._layout
        n = lay.lineCount()
        if n == 0:
            return 0, 0
        lh = self.line_height()
        first = max(0, int(self.scroll.value // lh) - 1)
        last = min(n - 1, first + self.visible_lines() + 2)
        end = lay.lineAt(last)
        return lay.lineAt(first).textStart(), end.textStart() + end.textLength()

    # --- Painting -------------------------------------------------------------------------------

    def _inner_rect(self) -> QRectF:
        inset = self.pad_y()
        return QRectF(0, inset, self.width(), self.height() - 2 * inset)

    def _card(self) -> QPainterPath:
        path = QPainterPath()
        r = self.theme.px(6)
        path.addRoundedRect(QRectF(self.rect()), r, r)
        return path

    def paintEvent(self, _event) -> None:
        t = self.theme
        colors = t.reading_colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        card = self._card()
        p.fillPath(card, colors["background"])
        p.setClipPath(card)
        s = self.swap.value
        if s < 1.0 and self._prev_messages:
            p.setOpacity(max(0.0, 1.0 - 2 * s))
            self._paint_messages(p, colors, self._prev_messages, 0.0, self._prev_secondary_from)
        p.setOpacity(s if s < 1.0 else 1.0)
        rise = (1.0 - s) * t.k * 4
        if self.messages is not None:
            self._hits = {}
            self._paint_messages(p, colors, self.messages, rise, self._secondary_from, self._hits)
        else:
            # Clip to the text rows so a partly scrolled line never peeks into the padding.
            p.setClipRect(self._inner_rect(), Qt.ClipOperation.IntersectClip)
            self._paint_text(p, colors, rise)
            self._paint_sections(p, colors, rise, s if s < 1.0 else 1.0)
            p.setOpacity(1.0)
            self._paint_fades(p, colors)
            p.setOpacity(s if s < 1.0 else 1.0)
            self._paint_outlines(p, colors, rise)
        if s >= 1.0:
            self._prev_messages = None
        p.setClipping(False)
        p.setOpacity(1.0)
        if self.chip_visible():  # appears and disappears instantly
            chip = self.chip_rect()
            p.setPen(QPen(colors["keycap_stroke"], 0))
            p.setBrush(colors["keycap_fill"])
            p.drawRoundedRect(chip.adjusted(0.5, 0.5, -0.5, -0.5), t.px(4), t.px(4))
            p.setFont(t.font("small"))
            p.setPen(colors["keycap_text"])
            p.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), CHIP_TEXT)
        corner = self.corner_rect()
        if corner is not None:  # appears and disappears instantly; a new label crossfades
            key, label, clickable, _tip = self._corner
            self._paint_chip(p, colors, corner, f"corner:{key}", label, self._corner_prev,
                             self._corner_fade.value, 1.0, clickable)
        # The hairline goes last so the fades never erase it.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(colors["edge"], 0))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), t.px(6), t.px(6))
        if self._card_hover:  # the same idiom as the sentence hover outline, shown instantly
            pen = (QPen(t.color("HoverOutline"), 0) if t.high_contrast
                   else QPen(with_alpha(colors["text"], 0.35), max(1.0, t.k)))
            inset = max(0.5, pen.widthF() / 2)
            p.setPen(pen)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), t.px(6), t.px(6))

    def _paint_chip(self, p: QPainter, colors: dict, rect: QRectF, key: str, label: str, prev: str, fade: float,
                    opacity: float, clickable: bool = True) -> None:
        """A text-mode chip: keycap colours, its own hover and press (an outline in high contrast), the label in
        the small role, and a crossfade from `prev` while `fade` < 1."""
        t = self.theme
        fill, stroke, ink = colors["keycap_fill"], colors["keycap_stroke"], colors["keycap_text"]
        hover = self._chip_hover[key].value if clickable and key in self._chip_hover else 0.0
        press = self._chip_press[key].value if clickable and key in self._chip_press else 0.0
        face = fill
        if not t.high_contrast:
            face = mix(mix(fill, composite(ink, 0.06, fill), hover), composite(ink, 0.10, fill), press)
        p.setOpacity(opacity)
        p.setPen(QPen(stroke, 0))
        p.setBrush(face)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), t.px(4), t.px(4))
        hc_outline(p, t, rect, t.px(4), max(hover, press))
        p.setFont(t.font("small"))
        p.setPen(ink)
        align = int(Qt.AlignmentFlag.AlignCenter)
        if prev and fade < 1.0:
            p.setOpacity(opacity * (1.0 - fade))
            p.drawText(rect, align, prev)
            p.setOpacity(opacity * fade)
        p.drawText(rect, align, label)
        p.setOpacity(opacity)

    def _paint_sections(self, p: QPainter, colors: dict, rise: float, opacity: float) -> None:
        """The 48 DIP rule and the chip on the blank row before the notes and the reference list."""
        if self._layout is None or not self._sections:
            return
        t = self.theme
        lh = self.line_height()
        origin = self._origin(rise)
        inner = self._inner_rect()
        reach = self._visible_range()[1]
        for section in self._sections:
            if section.offset > reach:
                break
            row = self._section_row(section)
            y = origin.y() + row * lh + lh / 2
            if inner.top() - lh < y < inner.bottom() + lh:
                p.setOpacity(opacity)
                p.setPen(QPen(colors["secondary" if section.read else "skipped"], max(1.0, t.k)))
                p.drawLine(QPointF(origin.x(), y), QPointF(origin.x() + t.px(48), y))
        for section, rect in self._section_rects(rise, drawn=True):
            fade = self._section_fade.get(section.name)
            value = fade.value if fade is not None else 1.0
            self._paint_chip(p, colors, rect, f"section:{section.name}", self._section_label(section),
                             self._section_prev.get(section.name, ""), value, opacity)
        p.setOpacity(opacity)

    def _message_role(self, messages: list[list[Part]]) -> str:
        """The message role when every line fits the width and all of them fit the rows; else the body role."""
        t = self.theme
        avail_w = self.width() - 2 * self.pad_x()
        avail_h = self.height() - 2 * self.pad_y()
        return ("message" if len(messages) * (t.line_height("message") + t.px(4)) <= avail_h
                and all(measure_parts(t, m, role="message", labels=self._labels) <= avail_w for m in messages)
                else "body")

    def _paint_messages(self, p: QPainter, colors: dict, messages, rise: float, secondary_from: int,
                        hits: dict[str, QRectF] | None = None) -> None:
        t = self.theme
        role = self._message_role(messages)
        lh = t.line_height(role) + t.px(4)
        total = len(messages) * lh
        avail = self.height() - 2 * self.pad_y()
        y0 = self.pad_y() + max(0.0, (avail - total) / 2) + rise
        caps = (colors["keycap_fill"], colors["keycap_stroke"], colors["keycap_text"])
        actions = {key: ActionState(self._action_hover[key].value, self._action_press[key].value,
                                    self._action_enabled[key]) for key in ACTION_PARTS}
        disabled = t.color("TextDisabled") if t.high_contrast else colors["secondary"]
        for i, parts in enumerate(messages):
            if parts:
                color = colors["text"] if i < secondary_from else colors["secondary"]
                paint_parts(p, t, self.pad_x(), y0 + i * lh, lh, parts, color, role=role, caps=caps,
                            actions=actions, disabled=disabled, hits=hits, labels=self._labels)

    def _band_path(self, origin: QPointF, a: int, b: int) -> QPainterPath:
        """Rounded rectangles behind the text range [a, b), one per line, merged."""
        lay, t = self._layout, self.theme
        lh = self.line_height()
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        first, last = self._line_of(a), self._line_of(max(a, b - 1))
        pad = t.k * 4
        for i in range(first, last + 1):
            line = lay.lineAt(i)
            start = max(a, line.textStart())
            end = min(b, line.textStart() + line.textLength())
            x1, x2 = _x(line, start), _x(line, end)
            if end >= line.textStart() + line.textLength():
                x2 = min(x2, line.naturalTextWidth())
            r = QRectF(origin.x() + x1 - pad, origin.y() + i * lh + t.k, x2 - x1 + 2 * pad, lh - 2 * t.k)
            path.addRoundedRect(r, t.px(5), t.px(5))
        return path.simplified()

    def _origin(self, rise: float) -> QPointF:
        """Where the text layout is drawn: the padding, minus the scroll, plus the message-swap rise."""
        return QPointF(self.pad_x(), self.pad_y() - self.scroll.value + rise)

    def _band_shown(self) -> bool:
        """Whether the current sentence is marked (a band, or a ring in high contrast)."""
        return (0 <= self.sentence < len(self.doc.sentences) and self.settings.get("highlight") != "off"
                and self.style in ("active", "paused", "muted", "stopped"))

    def _paint_text(self, p: QPainter, colors: dict, rise: float) -> None:
        lay, t, doc = self._layout, self.theme, self.doc
        if lay is None or doc is None:
            return
        lh = self.line_height()
        origin = self._origin(rise)
        cur = self.sentence if 0 <= self.sentence < len(doc.sentences) else -1

        # Sentence band (current sentence). High contrast rings it instead (see _paint_outlines).
        if self._band_shown() and not t.high_contrast:
            s = doc.sentences[cur]
            p.fillPath(self._band_path(origin, s.start, s.end), colors["band"])

        # Word pill.
        if self.pill_a.value > 0 and self._pill_line >= 0:
            color = colors["highlight_paused"] if self.style == "paused" else colors["highlight"]
            pill = QRectF(origin.x() + self.pill_x.value, origin.y() + self._pill_line * lh + t.k * 2,
                          self.pill_w.value, lh - 4 * t.k)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha(color, self.pill_a.value))
            p.drawRoundedRect(pill, t.px(3), t.px(3))

        # Text colours, later layers winning: earlier sentences dimmed, then text that is not read aloud in the
        # skipped colour (in every style, so it never looks already read), then the current word's text. Only
        # the rows on screen get ranges, and only they are drawn.
        va, vb = self._visible_range()
        layers = []
        if cur >= 0 and self.style != "finished":
            layers.append((0, doc.sentences[cur].start, colors["secondary"]))
        i = max(0, bisect.bisect_right(self._dim_starts, va) - 1)
        while i < len(self._dim) and self._dim[i][0] < vb:
            layers.append((self._dim[i][0], self._dim[i][1], colors["skipped"]))
            i += 1
        if cur >= 0 and self.style != "finished" and self.pill_a.value > 0.5:
            layers.append((*self._word_range(), colors["highlight_text"]))
        lay.draw(p, origin, color_runs(layers, va, vb), colors["text"], self._inner_rect())

    def _paint_outlines(self, p: QPainter, colors: dict, rise: float) -> None:
        """The high-contrast sentence ring and the hover outline (click target), drawn above the fades so a
        sentence on the top or bottom row keeps its whole outline. High contrast rings the current sentence in
        WindowText and the hover in Highlight, so the filled Highlight stays the word pill's."""
        t, doc = self.theme, self.doc
        if self._layout is None or doc is None:
            return
        origin = self._origin(rise)
        if self._band_shown() and t.high_contrast:
            s = doc.sentences[self.sentence]
            ring = QPen(t.color("TextPrimary"), max(1.0, 1.5 * t.k))
            p.strokePath(self._band_path(origin, s.start, s.end), ring)
        hv = self._hover_sentence
        if hv is not None and 0 <= hv < len(doc.sentences):
            s = doc.sentences[hv]
            pen = (QPen(t.color("HoverOutline"), 0) if t.high_contrast
                   else QPen(with_alpha(colors["text"], 0.35), max(1.0, t.k)))
            p.strokePath(self._band_path(origin, s.start, s.end), pen)

    def _paint_fades(self, p: QPainter, colors: dict) -> None:
        """A 6 DIP fade at the inner edges hints that there is more text above or below."""
        if self._layout is None:
            return
        lh = self.line_height()
        content_h = self._layout.lineCount() * lh
        inner = self._inner_rect()
        fade = self.theme.px(6)
        bg = colors["background"]
        if self.scroll.value > 0.5:
            g = QLinearGradient(0, inner.top(), 0, inner.top() + fade)
            g.setColorAt(0, bg)
            g.setColorAt(1, with_alpha(bg, 0.0))
            p.fillRect(QRectF(0, inner.top(), self.width(), fade), g)
        if self.scroll.value + self.visible_lines() * lh < content_h - 0.5:
            g = QLinearGradient(0, inner.bottom() - fade, 0, inner.bottom())
            g.setColorAt(0, with_alpha(bg, 0.0))
            g.setColorAt(1, bg)
            p.fillRect(QRectF(0, inner.bottom() - fade, self.width(), fade), g)


class _Line:
    """One row of a paragraph layout, in document offsets (the QTextLine API the view uses)."""

    __slots__ = ("base", "line")

    def __init__(self, base: int, line: QTextLine):
        self.base, self.line = base, line

    def textStart(self) -> int:
        return self.base + self.line.textStart()

    def textLength(self) -> int:
        return self.line.textLength()

    def naturalTextWidth(self) -> float:
        return self.line.naturalTextWidth()

    def cursorToX(self, pos: int) -> float:
        return _x(self.line, pos - self.base)

    def xToCursor(self, x: float, edge=QTextLine.CursorPosition.CursorBetweenCharacters) -> int:
        c = self.line.xToCursor(x, edge)
        return self.base + (c[0] if isinstance(c, tuple) else c)


class _Lines:
    """The document laid out one paragraph (one line of the shown text) per QTextLayout, rows numbered through
    the whole document.

    Paragraphs are laid out in order, only as far as something asks for (a row, an offset, the rows on screen),
    and the rest in short idle steps (step()), so a long document shows at once instead of after the whole
    layout (0.2 s at 160 pages). Rows already laid out never move, since everything before them is laid out
    too. Small layouts also keep every call cheap: in one 550k-character layout each cursorToX took about 2 ms
    and a paint of a 160-page document 14 ms; by paragraph a paint takes under 1 ms."""

    def __init__(self, text: str, font, width: float, lh: int):
        self._pieces = text.split("\n")
        self._font, self._width, self._lh = font, width, lh
        self._opt = QTextOption()
        self._opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        # Centre the ink (cap height to descender), not the font's ascent box, so every family sits mid-row.
        ink = QFontMetricsF(font).tightBoundingRect("Hdlbgjpqy")
        self._offset = (lh - ink.height()) / 2 - ink.top()
        self._chars = len(text)
        self.layouts: list[QTextLayout] = []
        self.bases: list[int] = []        # each laid-out paragraph's offset in the text
        self.starts: list[int] = []       # each row's first offset in the text
        self.rows: list[tuple[int, int]] = []  # (paragraph, line in it) for each row
        self._base = 0                    # where the next paragraph to lay out starts

    @property
    def done(self) -> bool:
        return len(self.layouts) == len(self._pieces)

    def _lay_out_next(self) -> None:
        k = len(self.layouts)
        piece = self._pieces[k]
        lay = QTextLayout(_GLUE.sub("\u00a0", piece), self._font)
        lay.setTextOption(self._opt)
        lay.beginLayout()
        row, j = len(self.rows), 0
        while True:
            line = lay.createLine()
            if not line.isValid():
                break
            line.setLineWidth(self._width)
            line.setPosition(QPointF(0, (row + j) * self._lh + self._offset - line.ascent()))
            self.starts.append(self._base + line.textStart())
            self.rows.append((k, j))
            j += 1
        lay.endLayout()
        self.layouts.append(lay)
        self.bases.append(self._base)
        self._base += len(piece) + 1

    def ensure_rows(self, n: int) -> None:
        """Lay out until there are at least `n` rows, or everything is laid out."""
        while len(self.rows) < n and not self.done:
            self._lay_out_next()

    def ensure_pos(self, pos: int) -> None:
        """Lay out up to the paragraph holding text offset `pos`."""
        while self._base <= pos and not self.done:
            self._lay_out_next()

    def step(self, budget_s: float = 0.008) -> bool:
        """Lay out more for about `budget_s` seconds; True once everything is laid out."""
        end = time.perf_counter() + budget_s
        while not self.done and time.perf_counter() < end:
            self._lay_out_next()
        return self.done

    def lineCount(self) -> int:
        """Rows laid out, plus an estimate for the rest (exact once done)."""
        if self.done or not self.rows:
            return len(self.rows) + (0 if self.done else max(1, (self._chars - self._base) // 60))
        per_row = max(1.0, self._base / len(self.rows))
        return len(self.rows) + max(1, int((self._chars - self._base) / per_row))

    def lineAt(self, i: int) -> _Line:
        self.ensure_rows(i + 1)
        k, j = self.rows[max(0, min(i, len(self.rows) - 1))]
        return _Line(self.bases[k], self.layouts[k].lineAt(j))

    def line_of(self, pos: int) -> int:
        """The row holding text offset `pos` (a paragraph break belongs to the row before it)."""
        self.ensure_pos(pos)
        return max(0, bisect.bisect_right(self.starts, max(0, pos)) - 1) if self.starts else 0

    def draw(self, p: QPainter, origin: QPointF, runs: list[tuple[int, int, QColor]], color: QColor,
             clip: QRectF) -> None:
        """Draw the rows inside `clip` (painter coordinates) at `origin`: text in `color`, and each (start, end,
        colour) run in its colour.

        Each colour is a pass clipped to its runs' rectangles, the way QTextLayout draws its selections; draw()'s
        own selections can't be used, since it leaves out a run whose rectangles touch one already drawn, so a
        citation right after dimmed text would come out in the full text colour."""
        lh = self._lh
        top = max(0, int((clip.top() - origin.y()) // lh))
        bottom = int((clip.bottom() - origin.y()) // lh)
        self.ensure_rows(bottom + 1)
        bottom = min(len(self.rows) - 1, bottom)
        if top > bottom:
            return
        first, last = self.rows[top][0], self.rows[bottom][0]
        lo, hi = self.bases[first], self.bases[last] + len(self.layouts[last].text())
        areas: dict[int, tuple[QColor, QPainterPath]] = {}
        for a, b, c in runs:
            a, b = max(a, lo), min(b, hi)
            if b <= a:
                continue
            path = areas.setdefault(c.rgba(), (c, QPainterPath()))[1]
            for row in range(self.line_of(a), self.line_of(b - 1) + 1):
                line = self.lineAt(row)
                x1 = line.cursorToX(max(a, line.textStart())) if a > line.textStart() else 0.0
                line_end = line.textStart() + line.textLength()
                x2 = line.cursorToX(b) if b < line_end else clip.right() - origin.x()
                path.addRect(QRectF(origin.x() + x1, origin.y() + row * lh, x2 - x1, lh))
        passes = [(color, None)] + list(areas.values())
        if len(passes) > 1:
            rest = QPainterPath()
            rest.addRect(clip)
            for _, path in areas.values():
                rest = rest.subtracted(path)
            passes[0] = (color, rest)
        for pen, area in passes:
            p.save()
            p.setClipRect(clip, Qt.ClipOperation.IntersectClip)
            if area is not None:
                p.setClipPath(area, Qt.ClipOperation.IntersectClip)
            p.setPen(pen)
            for k in range(first, last + 1):
                self.layouts[k].draw(p, origin, [], clip)
            p.restore()


@dataclass(frozen=True)
class _Region:
    """A skipped range from a document that lists them as (start, end) pairs only."""
    start: int
    end: int
    kind: str
