"""The screen-wide reading ruler: a clear band at the pointer's height, the rest of the screen dimmed.

Off by default. Only the pointer's screen is dimmed, never Saylight's own windows or the taskbar; the windows
are click-through and left out of screen captures. High contrast shows two lines and no dimming.

One window covers the work area of the pointer's screen. When the band moves, only the strips it left and entered
are repainted: at 7680 x 2160 that costs about 0.3 ms of CPU a frame, where resizing two dim windows every frame
cost 18-34 ms. The window of a screen the pointer has left is destroyed, so only one full-screen surface exists.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QScreen
from PySide6.QtWidgets import QWidget

from .. import win32_overlay
from ..theme import RULER_BAND
from .motion import MEDIUM, Tween, decelerate, exit_curve

log = logging.getLogger("saylight.reading_ruler")

RULER_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
               | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
FOLLOW_MS = 33        # the pointer is read 30 times a second, only while the ruler shows
CHECK_MS = 1000       # the lock screen / full-screen check, while the ruler is on
REBUILD_MS = 250      # after a screen change, the windows come back this much later
FADE_OUT_MS = 120
LINE = 2              # DIP: the two lines drawn instead of dimming (high contrast, band only)
# Dimming stays on: partial repaints measured 0.29 ms of CPU a frame at 7680 x 2160 (30 Hz, 0.6% of a core).
# Band-only mode, the fallback where that is too costly, draws just the two lines.
BAND_ONLY = False
SUPPRESS_REASONS = ("overlay", "fullscreen", "screens")


# --- Geometry and paint (pure) ------------------------------------------------------------------------------------

def band_height(t, size: str) -> int:
    return t.px(RULER_BAND.get(size, RULER_BAND["medium"]))


def ruler_rects(work_area: QRect, y: int, band: int) -> tuple[QRect, QRect, QRect]:
    """(dim above, band, dim below) for a pointer at height `y`: the band centred on y and kept inside the work
    area (so the taskbar is never dimmed). All in the work area's coordinates (global logical px)."""
    band = max(1, min(band, work_area.height()))
    top = y - band // 2
    top = max(work_area.top(), min(top, work_area.top() + work_area.height() - band))
    bottom = top + band
    above = QRect(work_area.left(), work_area.top(), work_area.width(), top - work_area.top())
    middle = QRect(work_area.left(), top, work_area.width(), band)
    below = QRect(work_area.left(), bottom, work_area.width(), work_area.top() + work_area.height() - bottom)
    return above, middle, below


def ruler_area(screen: QScreen) -> QRect:
    """Where a screen's ruler window goes: its work area, one px short where that is the whole screen (an
    auto-hidden taskbar, or none), so Windows doesn't take the ruler for a full-screen app."""
    return win32_overlay.short_of_full_screen(screen.availableGeometry(), screen.geometry())


def paint_ruler(p: QPainter, t, size: QSize, top: int, bottom: int, colors: dict[str, QColor], *, lines: bool,
                dpr: float = 1.0) -> None:
    """The ruler over a work area of `size` with the band from `top` to `bottom` (logical px, the area's own
    coordinates). Dimming: the `dim` colour above and below, with a 1 px `edge` line on each dim area's band side.
    `lines`: no dimming, two 2 DIP `line` lines just outside the band. Drawn in device px so lines stay crisp."""
    p.save()
    p.scale(1 / dpr, 1 / dpr)
    w = round(size.width() * dpr)
    h = round(size.height() * dpr)
    t_dev, b_dev = round(top * dpr), round(bottom * dpr)
    if lines:
        thick = max(1, round(t.px(LINE) * dpr))
        p.fillRect(QRect(0, t_dev - thick, w, thick), colors["line"])
        p.fillRect(QRect(0, b_dev, w, thick), colors["line"])
    else:
        p.fillRect(QRect(0, 0, w, t_dev), colors["dim"])
        p.fillRect(QRect(0, b_dev, w, h - b_dev), colors["dim"])
        if t_dev > 0:
            p.fillRect(QRect(0, t_dev - 1, w, 1), colors["edge"])
        if b_dev < h:
            p.fillRect(QRect(0, b_dev, w, 1), colors["edge"])
    p.restore()


# --- The window ---------------------------------------------------------------------------------------------------

class RulerWindow(QWidget):
    """Covers one screen's work area; the band is a clear gap in it."""

    def __init__(self, ruler: ReadingRuler, screen: QScreen):
        super().__init__(None, RULER_FLAGS)
        self.ruler = ruler
        self.screen_ = screen
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setScreen(screen)
        self.setGeometry(ruler_area(screen))
        self.top = self.bottom = 0
        self.leaving = False   # fading out after the ruler was turned off
        self.fade = Tween(self, ruler.theme, 0.0, MEDIUM)
        self.fade.on_change = self._on_fade
        self.excluded = win32_overlay.exclude_from_capture(int(self.winId()))

    def set_band(self, top: int, bottom: int) -> None:
        """Move the band (local logical px), repainting only the strips that change."""
        if (top, bottom) == (self.top, self.bottom):
            return
        pad = self.ruler.theme.px(LINE) + 2
        for a, b in ((self.top, top), (self.bottom, bottom)):
            lo, hi = min(a, b) - pad, max(a, b) + pad
            self.update(QRect(0, lo, self.width(), hi - lo))
        self.top, self.bottom = top, bottom

    def _on_fade(self, v: float) -> None:
        if v <= 0.0 and self.leaving:
            self.ruler._faded_out(self)
            return
        self.setWindowOpacity(1.0 if v >= 0.999 else max(0.0, v))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        r = self.ruler
        paint_ruler(p, r.theme, self.size(), self.top, self.bottom, r.colors(), lines=r.lines_only(),
                    dpr=self.devicePixelRatioF())
        p.end()


class ReadingRuler(QObject):
    toggled = Signal(bool)
    shown = Signal()   # a ruler window just appeared: Saylight's own windows (and the marks) go back on top

    def __init__(self, theme, settings, own_rects: Callable[[], list[QRect]], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.settings = settings
        self._own_rects = own_rects
        self.band_only = BAND_ONLY
        self._enabled = False
        self._reasons: set[str] = set()
        self._settle = False   # skip the next system check: the selection overlay has just been up
        self._window: RulerWindow | None = None
        self._y: int | None = None
        self._follow = QTimer(self)
        self._follow.setTimerType(Qt.TimerType.PreciseTimer)
        self._follow.setInterval(FOLLOW_MS)
        self._follow.timeout.connect(self._on_follow)
        self._check = QTimer(self)
        self._check.setInterval(CHECK_MS)
        self._check.timeout.connect(self._on_check)
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(REBUILD_MS)
        self._rebuild_timer.timeout.connect(lambda: self._set_reason("rebuild", False))
        self._watched: list[QScreen] = []
        settings.changed.connect(self._on_setting)
        theme.changed.connect(self._on_theme)
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(self._screens_changed)
            app.screenRemoved.connect(self._screens_changed)
        self._watch_screens()
        self.apply_settings()

    # --- public

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        """Turn the ruler on or off (ruler.enabled; the height stays as it was)."""
        self.settings.set("ruler.enabled", bool(on))
        self.apply_settings()

    def apply_settings(self) -> None:
        """Read ruler.enabled, ruler.height and ruler.dim. Turning on fades in; height and dim apply at once."""
        on = bool(self.settings.get("ruler.enabled"))
        if on != self._enabled:
            self._enabled = on
            if on:
                log.info("Ruler: on (%s, %s)", self.settings.get("ruler.height"), self.settings.get("ruler.dim"))
                self._check.start()
                self._on_check()
            else:
                log.info("Ruler: off")
                self._check.stop()
                self._reasons.discard("system")
            self._update(fade=True)
            self.toggled.emit(on)
        elif self._window is not None:
            self._place(force=True)
            self._window.update()

    def suppress(self, reason: str, on: bool) -> None:
        """Hide while any reason holds: "overlay" | "fullscreen" | "screens". Hiding is instant; the ruler
        comes back with a fade once nothing holds it."""
        if reason == "overlay":
            self._settle = True
        self._set_reason(reason, on)

    def is_showing(self) -> bool:
        return self._window is not None and self._window.isVisible() and self._window.fade.target > 0.0

    def window_id(self) -> int:
        return int(self._window.winId()) if self._window is not None else 0

    def colors(self) -> dict[str, QColor]:
        return self.theme.ruler_colors(self.settings.get("ruler.dim"))

    def lines_only(self) -> bool:
        """High contrast and band-only mode draw two lines instead of dimming."""
        return self.theme.high_contrast or self.band_only

    def band(self) -> int:
        return band_height(self.theme, self.settings.get("ruler.height"))

    # --- showing and hiding

    def _should_show(self) -> bool:
        return self._enabled and not self._reasons

    def _set_reason(self, reason: str, on: bool) -> None:
        had = reason in self._reasons
        if on == had:
            return
        if on:
            self._reasons.add(reason)
        else:
            self._reasons.discard(reason)
        self._update(fade=not on)

    def _update(self, *, fade: bool) -> None:
        """Show or hide to match the state. Turning off fades out; anything holding it hides it at once."""
        if self._should_show():
            if not self.is_showing():
                self._show(fade=fade)
            return
        self._follow.stop()
        w = self._window
        if w is None:
            return
        if fade and not self._reasons and w.isVisible():
            w.leaving = True
            w.fade.to(0.0, FADE_OUT_MS, exit_curve())
        else:
            self._drop_window()

    def _show(self, *, fade: bool) -> None:
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or (self._window.screen_ if self._window else None) \
            or QGuiApplication.primaryScreen()
        w = self._window_for(screen)
        w.leaving = False
        if self._y is None or QGuiApplication.screenAt(pos) is screen:
            self._y = pos.y()
        self._place(force=True)
        if not w.isVisible():
            w.fade.snap(0.0)
            w.setWindowOpacity(0.0)
            w.show()
            self.shown.emit()
        if fade:
            w.fade.to(1.0, MEDIUM, decelerate())
        else:
            w.fade.snap(1.0)
        self._follow.start()

    def _window_for(self, screen: QScreen) -> RulerWindow:
        if self._window is not None and self._window.screen_ is screen:
            return self._window
        self._drop_window()
        self._window = RulerWindow(self, screen)
        return self._window

    def _faded_out(self, w: RulerWindow) -> None:
        if w is self._window:
            self._drop_window()
        else:
            w.hide()

    def _drop_window(self) -> None:
        w, self._window = self._window, None
        if w is not None:
            w.hide()
            w.deleteLater()

    def _place(self, *, force: bool = False) -> None:
        w = self._window
        if w is None or self._y is None:
            return
        area = ruler_area(w.screen_)
        if force and w.geometry() != area:
            w.setGeometry(area)
        _above, band, _below = ruler_rects(area, self._y, self.band())
        w.set_band(band.top() - area.top(), band.top() - area.top() + band.height())

    # --- following the pointer

    def _on_follow(self) -> None:
        w = self._window
        if w is None:
            return
        pos = QCursor.pos()
        if any(r.contains(pos) for r in self._own_rects()):
            return  # over Saylight's own windows the band holds still
        screen = QGuiApplication.screenAt(pos)
        if screen is None:
            return  # between screens: keep the last position
        if screen is not w.screen_:
            self._y = pos.y()
            w = self._window_for(screen)
            self._place(force=True)
            w.fade.snap(1.0)
            w.setWindowOpacity(1.0)
            w.show()
            self.shown.emit()
            return
        if pos.y() != self._y:
            self._y = pos.y()
            self._place()

    # --- the system: lock screen, full-screen apps, screen changes, theme

    def _on_check(self) -> None:
        # Windows counts the selection overlay (topmost, the size of the screen) as a full-screen app, until a
        # moment after it closes
        if "overlay" in self._reasons:
            return
        if self._settle:
            self._settle = False
            return
        hidden = win32_overlay.screen_hidden_by_system()
        if hidden != ("system" in self._reasons):
            log.info("Ruler: hidden (full screen)" if hidden else "Ruler: shown again")
            self._set_reason("system", hidden)

    def _screens_changed(self, *_args) -> None:
        self._set_reason("rebuild", True)
        self._drop_window()
        self._watch_screens()
        self._rebuild_timer.start()

    def _watch_screens(self) -> None:
        for s in self._watched:
            try:
                s.geometryChanged.disconnect(self._screens_changed)
                s.availableGeometryChanged.disconnect(self._on_work_area)
            except (RuntimeError, TypeError):
                pass
        self._watched = list(QGuiApplication.screens())
        for s in self._watched:
            s.geometryChanged.connect(self._screens_changed)
            s.availableGeometryChanged.connect(self._on_work_area)

    def _on_work_area(self, *_args) -> None:
        if self._window is not None:
            self._place(force=True)
            self._window.update()

    def _on_setting(self, key: str) -> None:
        if key.startswith("ruler.") or key == "reading_tint":
            self.apply_settings()

    def _on_theme(self) -> None:
        if self._window is not None:
            self._place(force=True)
            self._window.update()

    def shutdown(self) -> None:
        """Quit: everything goes at once."""
        self._follow.stop()
        self._check.stop()
        self._drop_window()
