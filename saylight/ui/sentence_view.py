"""The reading card: follow-along text with a sentence band and a word pill.

Design, based on reading research with dyslexic readers:
* a short continuous window of the text, with the current line held on row 2 (row 3 when expanded), so the
  reader can look back (Schotter 2014) and read ahead of the voice (Schneps 2019);
* a soft band behind the current sentence (keeps your place) and a pill behind the current word;
* earlier sentences dimmed to ~5:1, words not yet read never dimmed, no bold/underline/size changes;
* the wheel scrolls back through the text (a "Back to current" chip returns at once; otherwise auto-follow
  resumes 4 s after the last scroll, waiting while the pointer is over the card), Ctrl+wheel changes text size,
  and a click on any sentence reads from there (the panel forwards clicks, so the card never takes focus);
* with no text yet, the whole card is one click target that opens Type or paste; message lines can hold action
  chips ("Type or paste text", "Undo"), which are hit-tested here.
"""

from __future__ import annotations

import bisect
import re

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen,
                           QTextCharFormat, QTextLayout, QTextOption)
from PySide6.QtWidgets import QWidget

from .controls import ACTION_PARTS, ActionState, Part, measure_parts, paint_parts
from .motion import FAST, MEDIUM, Tween, decelerate, with_alpha

LINE_SEP = " "
FOLLOW_RESUME_MS = 4000
SIZES = ("small", "medium", "large", "xlarge")
CHIP_TEXT = "Back to current"

# Spaces that must not break a row: around relations in statistics ("p = .018", "(n = 42)"), inside
# "F(2, 57)", "et al." and figure, table and equation references. They become no-break spaces in the layout
# only (same length, so every offset holds; Qt still applies word spacing to them), and the spoken text is
# untouched. Number lists are not glued: "2, 57" only when a ")" follows.
_GLUE = re.compile(r" (?=[=<>≤≥≈±×] )|(?<= [=<>≤≥≈±×]) |(?<=\d,) (?=\d+\))|(?<=\bet) (?=al\.)"
                   r"|(?<=\bFig\.) (?=\d)|(?<=\bFigure) (?=\d)|(?<=\bTable) (?=\d)|(?<=\bEq\.) (?=\d)")


def _x(line, pos: int) -> float:
    v = line.cursorToX(pos)
    return float(v[0] if isinstance(v, tuple) else v)


class SentenceView(QWidget):
    """Paints its own rounded card; the panel positions it and forwards drag/click gestures."""

    sizeStepRequested = Signal(int)

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
        self._layout: QTextLayout | None = None
        self._layout_key = None
        self._starts: list[int] = []
        self._hover_sentence: int | None = None
        self._card_hover = False
        self._hits: dict[str, QRectF] = {}  # action chips on the current message lines, as last painted
        self._action_hover = {key: Tween(self, theme, 0.0, FAST) for key in ACTION_PARTS}
        self._action_press = {key: Tween(self, theme, 0.0, FAST) for key in ACTION_PARTS}
        self._action_enabled = dict.fromkeys(ACTION_PARTS, True)
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
        self._layout_key = None
        self._ensure_layout()
        self._update_targets(animate=False)
        self.update()

    def resizeEvent(self, event) -> None:
        self.invalidate()
        super().resizeEvent(event)

    # --- Content ------------------------------------------------------------------------------

    def set_document(self, doc) -> None:
        self.doc = doc
        self.sentence = -1
        self.lo = self.hi = 0
        self._starts = [s.start for s in doc.sentences] if doc else []
        self._layout_key = None
        self._manual = False
        self.scroll.snap(0.0)
        self.pill_a.snap(0.0)
        self._ensure_layout()

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

    # --- Hit testing and gestures (called by the panel) ------------------------------------------

    def sentence_at(self, pos: QPointF) -> int | None:
        if self.messages is not None or self._layout is None or not self._starts:
            return None
        lay = self._layout
        y = pos.y() - self.pad_y() + self.scroll.value
        x = pos.x() - self.pad_x()
        row = int(y // self.line_height())
        if not (0 <= row < lay.lineCount()):
            return None
        line = lay.lineAt(row)
        if x < -4 or x > line.naturalTextWidth() + 4:
            return None
        c = line.xToCursor(max(0.0, x))
        c = c[0] if isinstance(c, tuple) else c
        return max(0, bisect.bisect_right(self._starts, c) - 1)

    def set_hover(self, sentence: int | None, allow_current: bool = False) -> None:
        """Outline the sentence a click would read from (the current one only when `allow_current`)."""
        if sentence == self.sentence and not allow_current:
            sentence = None
        self.setCursor(Qt.CursorShape.PointingHandCursor if sentence is not None else Qt.CursorShape.ArrowCursor)
        if sentence != self._hover_sentence:
            self._hover_sentence = sentence
            self.update()

    def action_at(self, pos: QPointF) -> str | None:
        """The action chip ("type" or "undo") under `pos` on the message lines, or None."""
        if self.messages is None:
            return None
        return next((key for key, r in self._hits.items() if r.contains(pos)), None)

    def set_action_state(self, key: str, hover: bool, pressed: bool, enabled: bool) -> None:
        """Show an action chip hovered or pressed; a disabled one takes neither."""
        if enabled != self._action_enabled[key]:
            self._action_enabled[key] = enabled
            self.update()
        for tween, on in ((self._action_hover[key], hover), (self._action_press[key], pressed)):
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

    # --- Layout ---------------------------------------------------------------------------------

    def _ensure_layout(self) -> None:
        if self.doc is None:
            self._layout = None
            return
        s = self.settings
        key = (id(self.doc), round(self.text_width()), self.theme.k, s.get("text_size"), s.get("reading_font"),
               s.get("spacing"))
        if key == self._layout_key and self._layout is not None:
            return
        self._layout_key = key
        font = self.theme.font("reading")
        lay = QTextLayout(_GLUE.sub("\u00a0", self.doc.text).replace("\n", LINE_SEP), font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        lay.setTextOption(opt)
        lay.beginLayout()
        lh = self.line_height()
        # Centre the ink (cap height to descender), not the font's ascent box, so every family sits mid-row.
        ink = QFontMetricsF(font).tightBoundingRect("Hdlbgjpqy")
        i = 0
        while True:
            line = lay.createLine()
            if not line.isValid():
                break
            line.setLineWidth(self.text_width())
            baseline = i * lh + (lh - ink.height()) / 2 - ink.top()
            line.setPosition(QPointF(0, baseline - line.ascent()))
            i += 1
        lay.endLayout()
        self._layout = lay

    def _line_of(self, pos: int) -> int:
        lay = self._layout
        if lay is None or lay.lineCount() == 0:
            return 0
        line = lay.lineForTextPosition(max(0, min(pos, len(lay.text()))))
        return line.lineNumber() if line.isValid() else lay.lineCount() - 1

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

    def _message_role(self, messages: list[list[Part]]) -> str:
        """The message role when every line fits the width and all of them fit the rows; else the body role."""
        t = self.theme
        avail_w = self.width() - 2 * self.pad_x()
        avail_h = self.height() - 2 * self.pad_y()
        return ("message" if len(messages) * (t.line_height("message") + t.px(4)) <= avail_h
                and all(measure_parts(t, m, role="message") <= avail_w for m in messages) else "body")

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
                            actions=actions, disabled=disabled, hits=hits)

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

        # Text colours: earlier sentences dimmed; the current sentence and everything after at full contrast.
        formats = []

        def fmt(start: int, length: int, color: QColor) -> None:
            if length > 0:
                r = QTextLayout.FormatRange()
                r.start, r.length = start, length
                f = QTextCharFormat()
                f.setForeground(QBrush(color))
                r.format = f
                formats.append(r)

        if cur >= 0 and self.style != "finished":
            fmt(0, doc.sentences[cur].start, colors["secondary"])
            if self.pill_a.value > 0.5:
                w0, w1 = self._word_range()
                fmt(w0, w1 - w0, colors["highlight_text"])
        p.setPen(colors["text"])
        lay.draw(p, origin, formats)

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
        if self._layout is None:
            return
        """A 6 DIP fade at the inner edges hints that there is more text above or below."""
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
