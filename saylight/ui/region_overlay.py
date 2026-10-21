"""Choosing an area of the screen to read: one overlay window per screen.

The overlays never take focus (Tool | Frameless | StaysOnTop | WindowDoesNotAcceptFocus, shown without
activating), are left out of screen captures, and open and close without animation. The drag stays on the
screen where it started; areas under 5 x 5 px are ignored.

Each overlay dims its screen with the scrim and cuts the selection out of it, ringed by a two-tone line drawn
outside the box so it shows on white, on black and on the scrim. High contrast draws no scrim, only a frame along
each screen edge. The overlay under the pointer paints the instruction pill.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen, QScreen
from PySide6.QtWidgets import QWidget

from .. import win32_overlay
from .controls import G_NOTHING, G_SCREEN, Part, draw_glyph, icon, measure_parts, paint_parts
from .motion import Tween

log = logging.getLogger("saylight.region_overlay")

OVERLAY_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)

MIN_PX = 5             # a box under 5 x 5 physical px is a click, not a selection
IDLE_S = 30.0          # cancel after this long without pointer movement
COUNTDOWN_S = 5        # ... counting down in the pill over the last 5 s
HINT_S = 3.0           # how long the small-box hint stays
TICK_MS = 250
TEXT_FADE_MS = 120     # pill text crossfade
# The pill, in DIP: height, corner radius, distance from the work area's top (or bottom) and sides, how close the
# pointer may come before it moves to the bottom, and how far around it a selection pushes it away.
PILL_H, PILL_RADIUS, PILL_EDGE, PILL_NEAR, PILL_GROW = 40, 8, 24, 48, 16
HC_FRAME = 3           # the high-contrast frame along each screen edge, DIP

PILL_TEXT = "Drag over the text to read"
HINT_TEXT = "Drag to draw a box around the text"
ACCESSIBLE_NAME = "Read from screen: drag over the text you want to read. Press Escape to cancel."
CANCEL_REASONS = ("esc", "right_click", "timeout", "screens_changed", "hotkey", "app")

# A clear pixel in a layered window lets clicks through to the app below, so the cut-out (and the whole screen in
# high contrast, which has no scrim) is filled with this instead: invisible, but it keeps the pointer.
CATCH = QColor(0, 0, 0, 1)


# --- Geometry (pure, so it can be tested without windows) ------------------------------------------------------

def selection_rect(origin: QPoint, pos: QPoint, bounds: QRect) -> QRect:
    """The box from the press point to the pointer, with the pointer clamped to `bounds` (the press screen)."""
    x = max(bounds.left(), min(pos.x(), bounds.left() + bounds.width()))
    y = max(bounds.top(), min(pos.y(), bounds.top() + bounds.height()))
    return QRect(min(origin.x(), x), min(origin.y(), y), abs(x - origin.x()), abs(y - origin.y()))


def too_small(rect: QRect, dpr: float) -> bool:
    """A box under MIN_PX x MIN_PX physical px is treated as a click."""
    return rect.width() * dpr < MIN_PX or rect.height() * dpr < MIN_PX


def pill_rect(t, work: QRect, centre_x: float, width: float, low: bool) -> QRectF:
    """Where the pill sits on a screen's work area (both in the overlay's own coordinates): centred on
    `centre_x`, PILL_EDGE inside the work area, at the top, or at the bottom once it has moved away."""
    w, h, edge = float(width), float(t.px(PILL_H)), t.px(PILL_EDGE)
    x = max(work.left() + edge, min(centre_x - w / 2, work.left() + work.width() - edge - w))
    y = work.top() + work.height() - edge - h if low else work.top() + edge
    return QRectF(x, y, w, h)


def pill_should_move(t, pill: QRectF, pointer: QPointF | None, selection: QRect | None) -> bool:
    """True when the pointer comes within PILL_NEAR of the pill, or the selection meets it grown by PILL_GROW."""
    if pointer is not None:
        dx = max(pill.left() - pointer.x(), 0.0, pointer.x() - pill.right())
        dy = max(pill.top() - pointer.y(), 0.0, pointer.y() - pill.bottom())
        if math.hypot(dx, dy) <= t.px(PILL_NEAR):
            return True
    if selection is not None and not selection.isEmpty():
        g = t.px(PILL_GROW)
        return pill.adjusted(-g, -g, g, g).intersects(QRectF(selection))
    return False


# --- The pill -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PillContent:
    glyph: str
    glyph_token: str
    label: str
    detail: tuple[Part, ...] = field(default_factory=tuple)


def detail_parts(detail: str, seconds_left: int = 0) -> tuple[Part, ...]:
    """The pill's detail after the label: "esc" ([Esc] cancels), "right_click" or the countdown."""
    if seconds_left > 0:
        return (("text", f"Closes in {seconds_left} s"),)
    if detail == "right_click":
        return (("text", "Right-click cancels"),)
    return (("key", "Esc"), ("text", "cancels"))


def pill_content(detail: str, *, hint: bool = False, seconds_left: int = 0) -> PillContent:
    parts = detail_parts(detail, seconds_left)
    if hint:
        return PillContent(G_NOTHING, "Caution", HINT_TEXT, parts)
    return PillContent(G_SCREEN, "TextSecondary", PILL_TEXT, parts)


def _detail_width(t, parts: tuple[Part, ...]) -> float:
    if parts and parts[0][0] == "key":  # [Esc], 4, then the text
        rest = measure_parts(t, list(parts[1:]), role="small") if len(parts) > 1 else 0.0
        return measure_parts(t, [parts[0]], role="small") + (t.px(4) + rest if rest else 0.0)
    return measure_parts(t, list(parts), role="small")


def pill_width(t, content: PillContent) -> float:
    """Glyph at 12, label at 36, 12, the detail, then 14 of padding."""
    label = QFontMetricsF(t.font("status")).horizontalAdvance(content.label)
    return t.px(36) + label + t.px(12) + _detail_width(t, content.detail) + t.px(14)


def _paint_pill_content(p: QPainter, t, rect: QRectF, content: PillContent, opacity: float) -> None:
    if opacity <= 0.0:
        return
    p.save()
    p.setOpacity(opacity)
    color = t.color(content.glyph_token)
    draw_glyph(p, QRectF(rect.left() + t.px(12), rect.top() + t.px(12), t.px(16), t.px(16)), icon(t, content.glyph),
               t.font("icon16"), color)
    font = t.font("status")
    fm = QFontMetricsF(font)
    baseline = rect.top() + (rect.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
    p.setFont(font)
    p.setPen(t.color("TextPrimary"))
    x = rect.left() + t.px(36)
    p.drawText(QPointF(x, baseline), content.label)
    x += fm.horizontalAdvance(content.label) + t.px(12)
    parts = list(content.detail)
    secondary = t.color("TextSecondary")
    if parts and parts[0][0] == "key":
        x = paint_parts(p, t, x, rect.top(), rect.height(), parts[:1], secondary, role="small")
        parts = parts[1:]
        x += t.px(4)
    if parts:
        paint_parts(p, t, x, rect.top(), rect.height(), parts, secondary, role="small")
    p.restore()


def paint_pill(p: QPainter, t, rect: QRectF, content: PillContent, previous: PillContent | None = None,
               fade: float = 1.0) -> None:
    """The instruction pill: Surface fill, a 1 px ControlStroke border, and its content (crossfading from
    `previous` while `fade` < 1)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    radius = t.px(PILL_RADIUS)
    p.setPen(QPen(t.color("ControlStroke"), 0))
    p.setBrush(t.color("Surface"))
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    p.setClipPath(clip)
    if previous is not None and fade < 1.0:
        _paint_pill_content(p, t, rect, previous, 1.0 - fade)
        _paint_pill_content(p, t, rect, content, fade)
    else:
        _paint_pill_content(p, t, rect, content, 1.0)
    p.restore()


# --- The ring and the overlay's paint -------------------------------------------------------------------------

def ring_width(t) -> int:
    """How far the ring reaches outside the box: 1 halo, 2 edge, 1 halo (DIP x k)."""
    return 2 * t.px(1) + t.px(2)


def paint_ring(p: QPainter, t, rect: QRectF) -> None:
    """The selection ring, outside `rect` with square corners: 1 DIP SelectionHalo, 2 DIP SelectionEdge and
    1 DIP SelectionHalo (Window, Highlight and Window in high contrast). The inside is left untouched."""
    a, b = t.px(1), t.px(2)
    halo, edge = t.color("SelectionHalo"), t.color("SelectionEdge")
    r1 = rect.adjusted(-a, -a, a, a)
    r2 = r1.adjusted(-b, -b, b, b)
    r3 = r2.adjusted(-a, -a, a, a)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    for inner, outer, color in ((rect, r1, halo), (r1, r2, edge), (r2, r3, halo)):
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.addRect(outer)
        path.addRect(inner)
        p.fillPath(path, color)
    p.restore()


def paint_overlay(p: QPainter, t, size, selection: QRect | None, pill: tuple | None = None) -> None:
    """One screen's overlay: the scrim with the selection cut out (or, in high contrast, the frame along the
    screen's edges), the ring, and the pill when `pill` is (rect, content, previous, fade)."""
    full = QRect(0, 0, size.width(), size.height())
    scrim = t.color("Scrim")
    p.fillRect(full, scrim if scrim.alpha() > 0 else CATCH)
    if t.high_contrast:
        frame = t.color("ModeFrame")
        w = t.px(HC_FRAME)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.addRect(QRectF(full))
        path.addRect(QRectF(full).adjusted(w, w, -w, -w))
        p.fillPath(path, frame)
    if selection is not None and not selection.isEmpty():
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(selection, CATCH)
        p.restore()
        paint_ring(p, t, QRectF(selection))
    if pill is not None:
        paint_pill(p, t, *pill)


# --- Windows ------------------------------------------------------------------------------------------------------

class RegionOverlay(QWidget):
    """Covers one screen; reports the pointer to its OverlayManager, which owns the state."""

    def __init__(self, manager: OverlayManager, screen: QScreen):
        super().__init__(None, OVERLAY_FLAGS)
        self.manager = manager
        self.theme = manager.theme
        self.screen_ = screen
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setAccessibleName(ACCESSIBLE_NAME)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.origin: QPoint | None = None
        self.selection = QRect()
        self.pill_x: float | None = None   # the pill's centre when this screen shows it
        self.pill_low = False
        self.content: PillContent | None = None
        self.previous: PillContent | None = None
        self.fade = Tween(self, self.theme, 1.0, TEXT_FADE_MS)
        self.excluded = win32_overlay.exclude_from_capture(int(self.winId()))

    # --- state

    def work_area(self) -> QRect:
        """The screen's work area in this window's coordinates."""
        return self.screen_.availableGeometry().translated(-self.screen_.geometry().topLeft())

    def pill_geometry(self) -> QRectF | None:
        if self.pill_x is None or self.content is None:
            return None
        return pill_rect(self.theme, self.work_area(), self.pill_x, pill_width(self.theme, self.content), self.pill_low)

    def set_pill(self, centre_x: float | None, content: PillContent | None) -> None:
        old = self.pill_geometry()
        self.pill_x = centre_x
        self.content = content
        self.previous = None
        self.fade.snap(1.0)
        self._update_pill(old)

    def set_content(self, content: PillContent) -> None:
        if content == self.content:
            return
        old = self.pill_geometry()
        self.previous, self.content = self.content, content
        if self.pill_x is not None:
            self.fade.snap(0.0)
            self.fade.to(1.0, TEXT_FADE_MS)
        self._update_pill(old)

    def move_pill_away(self) -> None:
        if self.pill_low or self.pill_x is None:
            return
        old = self.pill_geometry()
        self.pill_low = True
        self._update_pill(old)

    def _update_pill(self, old: QRectF | None) -> None:
        for r in (old, self.pill_geometry()):
            if r is not None:
                self.update(r.toAlignedRect().adjusted(-2, -2, 2, 2))

    def set_selection(self, rect: QRect) -> None:
        grow = ring_width(self.theme) + 2
        for r in (self.selection, rect):
            if not r.isEmpty():
                self.update(r.adjusted(-grow, -grow, grow, grow))
        self.selection = QRect(rect)

    # --- events

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        geometry = self.pill_geometry()
        pill = None
        if geometry is not None:
            pill = (geometry, self.content, self.previous, self.fade.value)
        paint_overlay(p, self.theme, self.size(), self.selection, pill)
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.manager.cancel_from(self, "right_click")
        elif event.button() == Qt.MouseButton.LeftButton:
            self.manager.pressed(self, event.position().toPoint())
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        self.manager.moved(self, event.position())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.manager.released(self, event.position().toPoint())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        event.accept()  # swallowed: scrolling the page under the overlay would move the text being chosen

    def enterEvent(self, event) -> None:
        self.manager.entered(self, QCursor.pos() - self.screen_.geometry().topLeft())
        super().enterEvent(event)

    def keyPressEvent(self, event) -> None:
        # Only reached when the overlay was activated because another app holds Esc.
        if event.key() == Qt.Key.Key_Escape:
            self.manager.cancel_from(self, "esc")
        event.accept()


class OverlayManager(QObject):
    selected = Signal(object, QRect)   # QScreen, the area in global logical px
    cancelled = Signal(str)            # "esc" | "right_click" | "timeout" | "screens_changed" | "hotkey" | "app"

    def __init__(self, theme, parent: QObject | None = None, *, clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.theme = theme
        self._clock = clock
        self._windows: list[RegionOverlay] = []
        self._press: RegionOverlay | None = None
        self._detail = "esc"
        self._countdown = 0
        self._last_move = 0.0
        self._hint_until = 0.0
        self.activated: RegionOverlay | None = None
        self.activated_hwnd = 0   # kept after closing, so the caller can hand the foreground back
        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._watched: list[QScreen] = []
        self._watching = False

    # --- public

    def is_open(self) -> bool:
        return bool(self._windows)

    def capture_excluded(self) -> bool:
        """True if every overlay is left out of screen captures; otherwise hide them before grabbing."""
        return bool(self._windows) and all(w.excluded for w in self._windows)

    def windows(self) -> list[RegionOverlay]:
        return list(self._windows)

    def open(self, *, pill_detail: str = "esc") -> None:
        """Cover every screen. `pill_detail` is how the pill says to cancel: "esc" or "right_click"."""
        if self._windows:
            return
        self._detail = pill_detail if pill_detail in ("esc", "right_click") else "esc"
        self._countdown = 0
        self._hint_until = 0.0
        self._press = None
        self.activated = None
        self.activated_hwnd = 0
        screens = QGuiApplication.screens()
        self._windows = [RegionOverlay(self, s) for s in screens]
        pointer = QCursor.pos()
        here = QGuiApplication.screenAt(pointer) or QGuiApplication.primaryScreen()
        for w in self._windows:
            if w.screen_ is here:
                w.set_pill(pointer.x() - here.geometry().x(), self._content())
        for w in self._windows:
            w.show()
        self._watch_screens(True)
        self._last_move = self._clock()
        self._tick.start()

    def close(self) -> None:
        """Close every overlay at once (no animation). Nothing is emitted."""
        self._tick.stop()
        self._watch_screens(False)
        windows, self._windows = self._windows, []
        self._press = None
        for w in windows:
            if w is self.activated:
                w.releaseKeyboard()
            w.hide()
            w.close()
        self.activated = None

    def cancel(self, reason: str = "app") -> None:
        """Close and report `cancelled(reason)` (Esc from the input thread, another shortcut, or the app)."""
        if not self._windows:
            return
        self.close()
        self.cancelled.emit(reason)

    def set_pill_detail(self, detail: str) -> None:
        """Switch how the pill says to cancel ("right_click" once Esc turned out to be unavailable)."""
        self._detail = detail
        self._refresh_content()

    def activate(self) -> bool:
        """The Esc fallback when another app holds Esc: bring the overlay under the pointer to the front so Esc
        reaches it as a key press. True if it worked; give the foreground back when closing."""
        target = next((w for w in self._windows if w.pill_x is not None), None) or \
            (self._windows[0] if self._windows else None)
        if target is None:
            return False
        if not win32_overlay.activate(int(target.winId())):
            log.info("Overlay couldn't be activated")
            return False
        target.grabKeyboard()
        self.activated = target
        self.activated_hwnd = int(target.winId())
        return True

    # --- from the windows

    def pressed(self, w: RegionOverlay, pos: QPoint) -> None:
        self._moved_now()
        if self._press is not None and self._press is not w:
            return
        self._press = w
        w.origin = QPoint(pos)
        w.set_selection(QRect())

    def moved(self, w: RegionOverlay, pos: QPointF) -> None:
        self._moved_now()
        if w.pill_x is None and self._press is None:
            self.entered(w, pos.toPoint())
        if w.origin is not None and self._press is w:
            w.set_selection(selection_rect(w.origin, pos.toPoint(), QRect(QPoint(0, 0), w.size())))
        pill = w.pill_geometry()
        if pill is not None and not w.pill_low and pill_should_move(self.theme, pill, pos, w.selection):
            w.move_pill_away()

    def released(self, w: RegionOverlay, pos: QPoint) -> None:
        if self._press is not w or w.origin is None:
            return
        rect = selection_rect(w.origin, pos, QRect(QPoint(0, 0), w.size()))
        self._press = None
        w.origin = None
        if too_small(rect, w.screen_.devicePixelRatio()):
            w.set_selection(QRect())
            self._hint_until = self._clock() + HINT_S
            self._refresh_content()
            return
        w.set_selection(rect)
        self.selected.emit(w.screen_, rect.translated(w.screen_.geometry().topLeft()))

    def entered(self, w: RegionOverlay, local: QPoint) -> None:
        """The pointer reached another screen: the pill moves there, centred on the pointer (at the bottom if it
        already moved away on that screen; it never moves back up)."""
        if w.pill_x is not None or self._press is not None:
            return
        for other in self._windows:
            if other is not w and other.pill_x is not None:
                other.set_pill(None, None)
        w.set_pill(local.x(), self._content())

    def cancel_from(self, _w: RegionOverlay, reason: str) -> None:
        self.cancel(reason)

    # --- idle timeout, hint and countdown

    def _moved_now(self) -> None:
        self._last_move = self._clock()
        if self._countdown:
            self._countdown = 0
            self._refresh_content()

    def _on_tick(self) -> None:
        now = self._clock()
        idle = now - self._last_move
        if idle >= IDLE_S:
            self.cancel("timeout")
            return
        left = math.ceil(IDLE_S - idle) if idle >= IDLE_S - COUNTDOWN_S else 0
        hint_over = self._hint_until and now >= self._hint_until
        if hint_over:
            self._hint_until = 0.0
        if left != self._countdown or hint_over:
            self._countdown = left
            self._refresh_content()

    def _content(self) -> PillContent:
        return pill_content(self._detail, hint=self._hint_until > 0.0, seconds_left=self._countdown)

    def _refresh_content(self) -> None:
        content = self._content()
        for w in self._windows:
            if w.pill_x is not None:
                w.set_content(content)

    # --- screens

    def _watch_screens(self, on: bool) -> None:
        app = QGuiApplication.instance()
        if on == self._watching:
            return
        self._watching = on
        if on:
            app.screenAdded.connect(self._screens_changed)
            app.screenRemoved.connect(self._screens_changed)
            self._watched = list(QGuiApplication.screens())
            for s in self._watched:
                s.geometryChanged.connect(self._screens_changed)
            return
        for signal in (app.screenAdded, app.screenRemoved):
            try:
                signal.disconnect(self._screens_changed)
            except (RuntimeError, TypeError):
                pass
        for s in self._watched:
            try:
                s.geometryChanged.disconnect(self._screens_changed)
            except (RuntimeError, TypeError):
                pass
        self._watched = []

    def _screens_changed(self, *_args) -> None:
        self.cancel("screens_changed")
