"""The sentence and word marked on the screen itself while reading from it.

One click-through window over the captured area, left out of screen captures. The marks hide by themselves
when the page under them moves (a once-a-second thumbnail check, in memory).

The same window first shows the selection ring as a frame while the text is found; the marks replace it when the
first word is spoken. The marks follow the card: a soft band behind the sentence and a stronger mark on the word,
in the card pill's hue on a light page and Soft dark's on a dark one. High contrast draws outlines, never fills.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPainter, QPainterPath, QPen, QScreen
from PySide6.QtWidgets import QWidget

from .. import screen_capture, win32_overlay
from ..ocr.model import ScreenText
from ..theme import SCREEN_MARK_ALPHA
from .motion import FAST, MEDIUM, Tween, decelerate, with_alpha
from .region_overlay import paint_ring

log = logging.getLogger("saylight.screen_highlight")

MARK_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
              | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
MARGIN = 8           # DIP around the area: room for the ring, which is drawn outside it
LINE_PAD = 3         # DIP above and below a line's ink
WORD_PAD = 3         # DIP either side of a word
BAND_PAD = 4         # DIP either side of a line's words
WORD_RADIUS, BAND_RADIUS = 3, 5
CHECK_MS = 1000

Word = tuple[int, int, QRectF]   # lo, hi (offsets in the sentence, as in Engine.position) and the box


# --- Geometry (pure) ----------------------------------------------------------------------------------------------

def visual_lines(rects: list[QRectF]) -> list[list[int]]:
    """Word indices grouped into visual lines: a word joins the line so far when they overlap vertically by at
    least half the smaller height."""
    lines: list[list[int]] = []
    top = bottom = 0.0
    for i, r in enumerate(rects):
        if lines:
            overlap = min(bottom, r.bottom()) - max(top, r.top())
            if overlap >= 0.5 * min(bottom - top, r.height()):
                lines[-1].append(i)
                top, bottom = min(top, r.top()), max(bottom, r.bottom())
                continue
        lines.append([i])
        top, bottom = r.top(), r.bottom()
    return lines


def find_word(words: list[Word], lo: int, hi: int) -> int | None:
    """The word the engine is on: the first whose range meets [lo, hi)."""
    if lo < 0 or hi <= lo:
        return None
    for i, (a, b, _r) in enumerate(words):
        if a < hi and b > lo:
            return i
    return None


@dataclass
class MarkGeometry:
    """One sentence's marks: each visual line's band rect, the merged band, and each word's mark rect (grown
    across, with its line's height) with the line it is on."""
    lines: list[QRectF] = field(default_factory=list)
    band: QPainterPath = field(default_factory=QPainterPath)
    words: list[QRectF] = field(default_factory=list)
    word_line: list[int] = field(default_factory=list)


def mark_geometry(t, words: list[Word], origin: QPoint = QPoint(0, 0)) -> MarkGeometry:
    """The band and word marks for a sentence's words (global logical px), relative to `origin`."""
    geo = MarkGeometry()
    rects = [QRectF(r).translated(-origin.x(), -origin.y()) for _a, _b, r in words]
    if not rects:
        return geo
    line_pad, word_pad, band_pad = t.k * LINE_PAD, t.k * WORD_PAD, t.k * BAND_PAD
    geo.words = [QRectF()] * len(rects)
    geo.word_line = [0] * len(rects)
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    for n, members in enumerate(visual_lines(rects)):
        top = min(rects[i].top() for i in members) - line_pad
        bottom = max(rects[i].bottom() for i in members) + line_pad
        left = min(rects[i].left() for i in members)
        right = max(rects[i].right() for i in members)
        line = QRectF(left - band_pad, top, right - left + 2 * band_pad, bottom - top)
        geo.lines.append(line)
        path.addRoundedRect(line, t.px(BAND_RADIUS), t.px(BAND_RADIUS))
        for i in members:
            r = rects[i]
            geo.words[i] = QRectF(r.left() - word_pad, top, r.width() + 2 * word_pad, bottom - top)
            geo.word_line[i] = n
    geo.band = path.simplified()
    return geo


def word_path(t, rect: QRectF) -> QPainterPath:
    path = QPainterPath()
    if not rect.isEmpty():
        path.addRoundedRect(rect, t.px(WORD_RADIUS), t.px(WORD_RADIUS))
    return path


def paint_marks(p: QPainter, t, mode: str, light: bool, band: QPainterPath, word: QRectF | None, *,
                paused_mix: float = 0.0, word_opacity: float = 1.0) -> None:
    """The band and the word as the card draws them, for `mode` ("word" or "sentence"). `paused_mix` blends the
    word from its playing to its paused strength."""
    if mode not in ("word", "sentence") or band.isEmpty():
        return
    show_word = mode == "word" and word is not None and not word.isEmpty() and word_opacity > 0.0
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if t.high_contrast:
        p.strokePath(band, QPen(t.color("TextPrimary"), max(1.0, 1.5 * t.k)))
        if show_word:
            pen = QPen(with_alpha(t.color("Accent"), word_opacity), 2 * t.k)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            inset = t.k
            p.drawRoundedRect(word.adjusted(inset, inset, -inset, -inset), t.px(WORD_RADIUS), t.px(WORD_RADIUS))
        p.restore()
        return
    color = t.screen_mark_color(light)
    band_alpha = SCREEN_MARK_ALPHA["band" if mode == "word" else "band_sentence"]
    shape = band.subtracted(word_path(t, word)) if show_word else band
    p.fillPath(shape, with_alpha(color, band_alpha))
    if show_word:
        playing, paused = SCREEN_MARK_ALPHA["word"], SCREEN_MARK_ALPHA["word_paused"]
        strength = playing + (paused - playing) * paused_mix
        p.fillPath(word_path(t, word), with_alpha(color, strength * word_opacity))
        # where the word fades out, the band shows through instead of a hole
        if word_opacity < 1.0:
            p.fillPath(word_path(t, word), with_alpha(color, band_alpha * (1.0 - word_opacity)))
    p.restore()


# --- The window ---------------------------------------------------------------------------------------------------

class MarkWindow(QWidget):
    def __init__(self, owner: ScreenHighlight):
        super().__init__(None, MARK_FLAGS)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.excluded = win32_overlay.exclude_from_capture(int(self.winId()))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        self.owner.paint(p)
        p.end()


class ScreenHighlight(QObject):
    pageMoved = Signal(bool)   # True: hidden after the page changed; False: shown again
    shown = Signal()           # the window just appeared: Saylight's own windows go back on top of it

    def __init__(self, theme, settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.settings = settings
        self.own_rects: Callable[[], list[QRect]] = lambda: []
        self._window: MarkWindow | None = None
        self._screen: QScreen | None = None
        self._area = QRect()          # the captured area, global logical px
        self._source: ScreenText | None = None
        self._active = False          # show_for() called and not hidden since
        self._moved = False           # hidden because the page changed
        self._check_logged = False
        self._sentence = -1
        self._geo = MarkGeometry()
        self._word: int | None = None
        self._next_sentence: tuple[int, int | None] | None = None  # (sentence, word) waiting for the fade out
        self._next_word: int | None = None                         # a word on another line, likewise
        self._word_waiting = False
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_MS)
        self._timer.timeout.connect(self._check)
        settings.changed.connect(self._on_setting)
        theme.changed.connect(self._on_theme)
        app = QGuiApplication.instance()
        if app is not None:
            app.screenRemoved.connect(self._on_screen_removed)

    # --- public

    def set_own_rects(self, own_rects: Callable[[], list[QRect]]) -> None:
        """Saylight's visible windows (global logical px): the change check ignores what they cover."""
        self.own_rects = own_rects

    def show_frame(self, screen: QScreen, rect: QRect) -> None:
        """The selection ring, kept up as a frame while the text is found."""
        w = self._place(screen, rect)
        self._marks.snap(0.0)
        self._frame.snap(1.0)
        if not w.isVisible():
            w.show()
            self.shown.emit()
        w.update()

    def hide_frame(self, animate: bool = True) -> None:
        if self._window is None:
            return
        if animate:
            self._frame.to(0.0, MEDIUM)
        else:
            self._frame.snap(0.0)
        self._hide_if_clear()

    def show_for(self, source: ScreenText) -> None:
        """Mark this reading's words on the screen from its first spoken word on (with Highlight on screen on
        and Highlight not Off). Reading the same source again shows the marks again if the page hasn't moved."""
        same = source is self._source
        self._source = source
        self._active = True
        if not same:
            self._moved = False
            self._reset_marks()
        screen = next((s for s in QGuiApplication.screens() if s.name() == source.screen), None)
        if screen is None:
            self._active = False
            self.hide(animate=False)
            return
        if self._window is None or not self._window.isVisible() or self._area != source.region:
            self._place(screen, source.region)
        if not self._wanted():
            self.hide_frame()
        elif same and self._moved:
            self._check()
        self._sync_timer()

    def set_word(self, sentence: int, lo: int, hi: int) -> None:
        """Connected to Engine.position."""
        if not self._active or self._source is None or sentence < 0:
            return
        word = find_word(self._source.boxes.get(sentence, []), lo, hi)
        if sentence != self._sentence or self._next_sentence is not None:
            self._word_waiting = False
            self._word_a.snap(1.0)
            if self._sentence < 0 or self._marks.target == 0.0:
                self._next_sentence = None
                self._swap.snap(1.0)
                self._set_sentence(sentence, word)
            else:
                self._next_sentence = (sentence, word)
                self._swap.to(0.0, FAST)
        else:
            self._move_word(word)
        if self._wanted() and not self._moved and self._marks.target < 1.0:
            self._show_window()
            self._marks.to(1.0, FAST)   # first: with reduced motion the frame's fade ends at once and would
            self._frame.to(0.0, MEDIUM)  # hide the window before the marks had started

    def set_paused(self, on: bool) -> None:
        self._paused.to(1.0 if on else 0.0, MEDIUM)

    def hide(self, animate: bool = True) -> None:
        """Fade the marks (and any frame) out; the source is kept so the same reading can show them again."""
        self._active = False
        self._sync_timer()
        if self._window is None:
            return
        if animate:
            self._marks.to(0.0, MEDIUM)
            self._frame.to(0.0, MEDIUM)
        else:
            self._marks.snap(0.0)
            self._frame.snap(0.0)
        self._hide_if_clear()

    def forget(self) -> None:
        """Drop the source (a new reading, Clear): nothing about the old area stays."""
        self.hide()
        self._source = None
        self._moved = False
        self._reset_marks()

    def page_moved(self) -> bool:
        """True while the marks are hidden because the page under them changed."""
        return self._moved

    def is_visible(self) -> bool:
        return self._window is not None and self._window.isVisible()

    def window_id(self) -> int:
        return int(self._window.winId()) if self._window is not None else 0

    # --- painting

    def paint(self, p: QPainter) -> None:
        t, w = self.theme, self._window
        if w is None:
            return
        local = QRectF(self._area.translated(-w.geometry().topLeft()))
        if self._frame.value > 0.0:
            p.save()
            p.setOpacity(self._frame.value)
            paint_ring(p, t, local)
            p.restore()
        if self._marks.value <= 0.0 or self._geo.band.isEmpty() or not self._wanted():
            return
        p.save()
        p.setClipRect(local)
        p.setOpacity(self._marks.value * self._swap.value)
        word = None
        if self._word is not None:
            target = self._geo.words[self._word]
            word = QRectF(self._wx.value, target.top(), self._ww.value, target.height())
        paint_marks(p, t, self.settings.get("highlight"), self._source.light if self._source else True,
                    self._geo.band, word, paused_mix=self._paused.value, word_opacity=self._word_a.value)
        p.restore()

    # --- internals

    def _ensure_window(self) -> MarkWindow:
        if self._window is None:
            w = self._window = MarkWindow(self)
            self._frame = Tween(w, self.theme, 0.0, MEDIUM)
            self._marks = Tween(w, self.theme, 0.0, FAST)
            self._swap = Tween(w, self.theme, 1.0, FAST)
            self._word_a = Tween(w, self.theme, 1.0, FAST)
            self._wx = Tween(w, self.theme, 0.0, FAST, decelerate())
            self._ww = Tween(w, self.theme, 0.0, FAST, decelerate())
            self._paused = Tween(w, self.theme, 0.0, MEDIUM)
            for tween in (self._frame, self._marks):
                tween.on_change = lambda _v: self._hide_if_clear()
            self._swap.on_change = self._on_swap
            self._word_a.on_change = self._on_word_fade
        return self._window

    def _place(self, screen: QScreen, rect: QRect) -> MarkWindow:
        w = self._ensure_window()
        m = self.theme.px(MARGIN)
        geometry = win32_overlay.short_of_full_screen(rect.adjusted(-m, -m, m, m), screen.geometry())
        moved_screen = self._screen is not screen
        if moved_screen and w.isVisible():
            w.hide()
        self._screen, self._area = screen, QRect(rect)
        if moved_screen:
            # (QWidget.screen() isn't used: in PySide it ties the shared QScreen wrapper to the widget's life)
            w.setScreen(screen)
        if w.geometry() != geometry:
            w.setGeometry(geometry)
        self._geo = mark_geometry(self.theme, self._source.boxes.get(self._sentence, []),
                                  geometry.topLeft()) if self._source is not None and self._sentence >= 0 \
            else MarkGeometry()
        return w

    def _show_window(self) -> None:
        w = self._ensure_window()
        if not w.isVisible():
            w.show()
            self.shown.emit()

    def _hide_if_clear(self) -> None:
        w = self._window
        if w is not None and w.isVisible() and self._frame.value <= 0.0 and self._marks.value <= 0.0 \
                and self._frame.target == 0.0 and self._marks.target == 0.0:
            w.hide()

    def _set_sentence(self, sentence: int, word: int | None) -> None:
        self._sentence = sentence
        origin = self._window.geometry().topLeft() if self._window is not None else QPoint(0, 0)
        self._geo = mark_geometry(self.theme, self._source.boxes.get(sentence, []), origin)
        self._word = None
        self._snap_word(word)

    def _snap_word(self, word: int | None) -> None:
        self._word = word
        if word is not None:
            r = self._geo.words[word]
            self._wx.snap(r.left())
            self._ww.snap(r.width())
        self._word_a.snap(1.0)

    def _move_word(self, word: int | None) -> None:
        if self._word_waiting:
            self._next_word = word  # the fade out under way lands on the latest word
            return
        if word == self._word:
            return
        same_line = (word is not None and self._word is not None
                     and self._geo.word_line[word] == self._geo.word_line[self._word])
        if same_line:
            r = self._geo.words[word]
            self._word = word
            self._wx.to(r.left(), FAST, decelerate())
            self._ww.to(r.width(), FAST, decelerate())
        elif self._word is None or self._marks.value <= 0.0:
            self._snap_word(word)
        else:
            self._next_word, self._word_waiting = word, True
            self._word_a.to(0.0, FAST)

    def _on_swap(self, v: float) -> None:
        """The sentence changed: once the old marks have faded out, the new ones fade in."""
        if v <= 0.0 and self._swap.target == 0.0 and self._next_sentence is not None:
            sentence, word = self._next_sentence
            self._next_sentence = None
            self._set_sentence(sentence, word)
            self._swap.to(1.0, FAST)

    def _on_word_fade(self, v: float) -> None:
        """The word moved to another line: fade out, jump, fade in."""
        if v <= 0.0 and self._word_a.target == 0.0 and self._word_waiting:
            self._word_waiting = False
            self._snap_word(self._next_word)
            self._word_a.snap(0.0)
            self._word_a.to(1.0, FAST)

    def _reset_marks(self) -> None:
        self._sentence, self._word = -1, None
        self._next_sentence, self._next_word, self._word_waiting = None, None, False
        self._geo = MarkGeometry()

    def _on_theme(self) -> None:
        if self._source is not None and self._sentence >= 0 and self._window is not None:
            word = self._word
            self._set_sentence(self._sentence, word)
        self._repaint()

    def _wanted(self) -> bool:
        return (self._source is not None and bool(self.settings.get("screen.highlight"))
                and self.settings.get("highlight") != "off")

    def _sync_timer(self) -> None:
        on = self._active and self._wanted() and self._window is not None
        if on and not self._window.excluded:
            if not self._check_logged:
                log.info("Screen highlight: change check off (capture exclusion unavailable)")
                self._check_logged = True
            on = False
        if on and not self._timer.isActive():
            self._timer.start()
        elif not on:
            self._timer.stop()

    def _check(self) -> None:
        """Compare the area with the capture; hide the marks while the page has moved, and show them again when
        it is back."""
        source, screen = self._source, self._screen
        if source is None or screen is None or not source.thumb:
            return
        try:
            now = screen_capture.thumb(screen, source.region)
        except RuntimeError:  # the screen went away (Qt has deleted it): the page is gone too
            self._screen = None
            now = b""
        skip = screen_capture.cells_under(source.region, now, self.own_rects())
        moved = screen_capture.changed(source.thumb, now, skip=skip)
        if moved == self._moved:
            return
        self._moved = moved
        if moved:
            log.info("Screen highlight: hidden (page moved)")
            self._marks.to(0.0, MEDIUM)
            self._frame.to(0.0, MEDIUM)
        else:
            log.info("Screen highlight: shown again")
            if self._sentence >= 0 and self._active and self._wanted():
                self._show_window()
                self._marks.to(1.0, MEDIUM)
        self.pageMoved.emit(moved)

    def _on_screen_removed(self, screen: QScreen) -> None:
        """The monitor the marks are on was unplugged or turned off: they'd be over something else now."""
        if screen is not self._screen:
            return
        self._screen = None
        if self._source is not None and not self._moved:
            self._moved = True
            log.info("Screen highlight: hidden (page moved)")
            self.pageMoved.emit(True)
        self.hide(animate=False)

    def _on_setting(self, key: str) -> None:
        if key not in ("screen.highlight", "highlight", "reading_tint"):
            return
        if self._window is None:
            return
        if not self._wanted():
            self._marks.to(0.0, MEDIUM)
        elif self._active and not self._moved and self._sentence >= 0:
            self._show_window()
            self._marks.to(1.0, MEDIUM)
        self._sync_timer()
        self._repaint()

    def _repaint(self) -> None:
        if self._window is not None:
            self._window.update()
