"""Sheets: the focusable windows laid over the panel (Pronunciations, Save as audio, the PDF password).

The panel never takes focus, so anything you type into needs a window of its own. A sheet is the Type or paste
box's window recipe made reusable: unowned (an owned window would hand activation to the panel when it hides),
Tool | Frameless | StaysOnTop with flags that never change, shown with show() and never exec(), and in the
foreground only while it is open. Its geometry is final from the first frame; only its opacity moves.

Only one focusable Saylight window is open at a time: the App closes the others with the reason "switch".
The App also wires light dismiss (ForegroundTracker.appActivated -> is_dismissed_by), the tray and the HUD.

DialogHost gives a native Open or Save dialog an owner that may take focus, so it opens in front of the panel.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication, QKeyEvent, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QDialog, QFileDialog, QGraphicsOpacityEffect, QLineEdit, QProxyStyle, QStyle, QWidget

from .. import win32
from .compose import AWAY_SETTLE_MS, WM_ACTIVATEAPP, WM_NCACTIVATE, compose_rect, is_light_dismiss
from .controls import (G_COLLAPSE, G_NOTHING, TRANSPARENT, FadeLabel, IconButton, Part, draw_glyph, paint_parts,
                       spoken_parts)
from .motion import FAST, MEDIUM, SLOW, Tween, decelerate, exit_curve
from .panel import MA_NOACTIVATE, WM_MOUSEACTIVATE

log = logging.getLogger("saylight.sheet")

SHEET_FLAGS = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
SHEET_MODES = ("cover", "panel")  # at least the panel's size and grown to the natural size, or exactly the panel
CLOSE_MS = 120
FIELD_H = 34  # DIP
ACTIVATION_NOTE = "Click in the box to start typing."
BACK_TIP = "Back to the panel (Esc)"


# --- Pure helpers ------------------------------------------------------------------------------

def sheet_rect(rest: QRect, area: QRect, natural: QSize, panel_size: QSize, *, mode: str = "cover",
               snap_x: str | None = None, margin: int = 0, bottom_margin: int = 0) -> QRect:
    """Where a sheet goes. "panel": exactly the panel's resting rect. "cover": the larger of the panel and the
    sheet's natural size, placed like the Type or paste box (sharing the panel's anchored edges, kept inside the
    work area's margins), so the sheet always covers the whole panel."""
    if mode == "panel":
        return QRect(rest)
    if mode != "cover":
        raise ValueError(f"unknown sheet mode: {mode}")
    size = QSize(max(panel_size.width(), natural.width(), rest.width()),
                 max(panel_size.height(), natural.height(), rest.height()))
    return compose_rect(rest, area, size, snap_x=snap_x, margin=margin, bottom_margin=bottom_margin)


def sheet_chrome(theme, W: int, H: int, info_w: float = 0.0) -> dict[str, QRect]:
    """The chrome every sheet shares, in window coordinates: header glyph, title, info and Back, the hint row,
    and the footer row whose controls are centred on H - 32."""
    px = theme.px
    iw = math.ceil(info_w)
    info = QRect(W - px(52) - iw, px(14), iw, px(20))
    title_right = info.left() - px(12) if iw else W - px(52)
    return {"glyph": QRect(px(22), px(16), px(16), px(16)),
            "title": QRect(px(46), px(14), max(0, title_right - px(46)), px(20)),
            "info": info,
            "back": QRect(W - px(44), px(8), px(32), px(32)),
            "hint": QRect(px(22), H - px(72), W - 2 * px(22), px(16)),
            "footer": QRect(0, H - px(52), W, px(40))}


@dataclass
class SheetSession:
    """One opening of a sheet."""
    phase: str = "closed"       # closed | opening | open | closing
    return_to: int = 0          # the window focus goes back to
    was_active: bool = False    # the sheet has had the foreground (light dismiss waits for it)
    source: str = ""            # what opened it: button | card | flyout | tray | word | settings


# --- Parts -------------------------------------------------------------------------------------

class _CaretStyle(QProxyStyle):
    """QLineEdit takes its caret width from the style; this one answers with the width it is given."""

    def __init__(self, width: int):
        super().__init__()
        self.caret = width

    def pixelMetric(self, metric, option=None, widget=None) -> int:
        if metric == QStyle.PixelMetric.PM_TextCursorWidth:
            return self.caret
        return super().pixelMetric(metric, option, widget)


class LineField(QLineEdit):
    """A one-line field for sheets: 34 DIP tall, radius 4, 10 DIP of text padding, drawn from the theme.

    "well" fields hold reading text (a word, how to say it): the reading background, font (at most 18 DIP) and
    colours, with the card's hairline. "control" fields hold UI text (Find, a password): ControlFill and
    ControlStroke, or the reading keycap pair when `in_well` (the field sits on the reading card). Focus draws
    the border in the text colour, 1 DIP (2 px in high contrast). The caret follows the Windows cursor thickness.
    Everything is configured here, before the first show."""

    def __init__(self, theme, parent: QWidget | None = None, *, style: str = "control", in_well: bool = False,
                 name: str = "", placeholder: str = ""):
        super().__init__(parent)
        if style not in ("well", "control"):
            raise ValueError(f"unknown field style: {style}")
        self.theme = theme
        self.field_style = style
        self.in_well = in_well
        self.setFrame(False)
        self._caret_style = _CaretStyle(1)
        self._caret_style.setParent(self)
        self.setAccessibleName(name)
        self.setPlaceholderText(placeholder)
        self.apply_theme()
        theme.changed.connect(self.apply_theme)

    def colors(self) -> tuple:
        """(fill, border, focused border, text, placeholder)."""
        t = self.theme
        c = t.reading_colors()
        if self.field_style == "well":
            return c["background"], c["edge"], c["text"], c["text"], c["secondary"]
        if self.in_well and not t.high_contrast:
            return c["keycap_fill"], c["keycap_stroke"], c["keycap_text"], c["keycap_text"], c["secondary"]
        ink = t.color("TextPrimary")
        return t.color("ControlFill"), t.color("ControlStroke"), ink, ink, t.color("TextSecondary")

    def field_font(self) -> QFont:
        t = self.theme
        if self.field_style == "control":
            font = QFont(t.font("body"))
        else:
            font = QFont(t.font("reading"))
            size, cap = font.pixelSize(), t.px(18)
            if size > cap:
                font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, font.letterSpacing() * cap / size)
                font.setPixelSize(cap)
        font.setBold(False)
        font.setItalic(False)
        font.setUnderline(False)
        return font

    def apply_theme(self) -> None:
        t = self.theme
        c = t.reading_colors()
        _fill, _border, _focused, text, placeholder = self.colors()
        self.setFont(self.field_font())
        pal = QPalette(self.palette())
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            pal.setColor(group, QPalette.ColorRole.Base, TRANSPARENT)  # painted here instead
            pal.setColor(group, QPalette.ColorRole.Text, text)
            pal.setColor(group, QPalette.ColorRole.PlaceholderText,
                         t.color("TextDisabled") if t.high_contrast else placeholder)
            pal.setColor(group, QPalette.ColorRole.Highlight, c["highlight"])
            pal.setColor(group, QPalette.ColorRole.HighlightedText, c["highlight_text"])
        self.setPalette(pal)
        self.setTextMargins(t.px(10), 0, t.px(10), 0)
        width = max(t.px(2), win32.caret_width_px())
        if self.style() is not self._caret_style or self._caret_style.caret != width:
            self._caret_style.caret = width
            self.setStyle(self._caret_style)  # the field reads the caret width again
        self.setFixedHeight(t.px(FIELD_H))
        self.update()

    def paintEvent(self, event) -> None:
        t = self.theme
        fill, border, focused, _text, _ph = self.colors()
        on = self.hasFocus()
        width = (2.0 if t.high_contrast else float(t.k)) if on else 0.0  # 0: a cosmetic 1 px hairline
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        inset = max(width, 1.0) / 2
        r = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        radius = t.px(4)
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.fillPath(path, fill)
        p.setPen(QPen(focused if on else border, width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, radius, radius)
        p.end()
        super().paintEvent(event)


class _SheetGlyph(QWidget):
    """The header's 16 DIP glyph on the 22 DIP column."""

    def __init__(self, theme, glyph: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.glyph = glyph
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme.changed.connect(self.update)

    def set_glyph(self, glyph: str) -> None:
        self.glyph = glyph
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_glyph(p, QRectF(self.rect()), self.glyph, self.theme.font("icon16"), self.theme.color("TextSecondary"))


class SheetHint(QWidget):
    """The hint row: keycap hints ("[Enter] saves · [Esc] goes back"), or a note in their place (an Info glyph
    in Caution, then the text). Changes crossfade in 120 ms."""

    def __init__(self, theme, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.content: tuple[str, object] = ("hint", [])
        self._prev: tuple[str, object] | None = None
        self.fade = Tween(self, theme, 1.0, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme.changed.connect(self.update)

    @property
    def note(self) -> bool:
        return self.content[0] == "note"

    def set_content(self, kind: str, value: object) -> None:
        if (kind, value) == self.content:
            return
        self._prev, self.content = self.content, (kind, value)
        self.fade.snap(0.0)
        self.fade.to(1.0)
        self.setAccessibleName(value if kind == "note" else spoken_parts(value))
        self.update()

    def _paint(self, p: QPainter, content: tuple[str, object], opacity: float) -> None:
        kind, value = content
        if opacity <= 0 or not value:
            return
        t = self.theme
        p.setOpacity(opacity)
        if kind == "hint":
            paint_parts(p, t, 0, 0, self.height(), value, t.color("TextSecondary"), role="small")
            return
        g = t.px(16)
        draw_glyph(p, QRectF(0, (self.height() - g) / 2, g, g), G_NOTHING, t.font("icon16"), t.color("Caution"))
        font = t.font("small")
        fm = QFontMetricsF(font)
        x = t.px(24)  # the text on the 46 DIP column
        p.setFont(font)
        p.setPen(t.color("TextPrimary"))
        p.drawText(QRectF(x, 0, self.width() - x, self.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   fm.elidedText(value, Qt.TextElideMode.ElideRight, self.width() - x))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        f = self.fade.value
        if f < 1.0 and self._prev:
            self._paint(p, self._prev, 1.0 - f)
        self._paint(p, self.content, f)


# --- The window --------------------------------------------------------------------------------

class Sheet(QDialog):
    """A focusable window over the panel. Created hidden; the App opens and closes it.

    Subclasses put their content in `body` and place it in layout_content(), name the widget that takes the caret
    in focus_target(), and run their main action in primary() (Enter). Esc, Back and Alt+F4 ask the App to close
    the sheet through closeRequested; the App then calls close_animated() with the reason, and `closed` follows.

    `natural` is the sheet's own size in DIP at k = 1. Opening: shown at opacity 0 and activated at once, then
    the opacity rises over 167 ms while the content fades in from 83 to 250 ms, and the panel counts as covered
    from 167 ms. A "panel" sheet only fades: 167 ms in. Closing: a 120 ms fade, then hide."""

    closeRequested = Signal(str)   # esc | back | away | screen
    closed = Signal(str)           # the reason close_animated() was given

    def __init__(self, ctl, *, name: str, glyph: str, title: str, natural: QSize, mode: str = "cover"):
        super().__init__(None, SHEET_FLAGS)  # always unowned, and the flags never change
        if mode not in SHEET_MODES:
            raise ValueError(f"unknown sheet mode: {mode}")
        self.ctl = ctl
        t = self.theme = ctl.theme
        self.tips = ctl.tips
        self.mode = mode
        self.natural = QSize(natural)
        self.session = SheetSession()
        self._panel_visible = True
        self._covered = False
        self._reason = ""
        self._motion = ""
        self._hint: list[Part] = []
        self.setWindowTitle(f"{name} – Saylight")
        self.setModal(False)
        self.hwnd = int(self.winId())

        # The body sits under the chrome and holds the subclass's content; it fades in as the sheet opens.
        self.body = QWidget(self)
        self._body_fade = QGraphicsOpacityEffect(self.body)
        self._body_fade.setEnabled(False)
        self.body.setGraphicsEffect(self._body_fade)
        self.glyph = _SheetGlyph(t, glyph, self)
        self.title = FadeLabel(t, "status", "TextPrimary", self)
        self.title.set_text(title, animate=False)
        self.info = FadeLabel(t, "small", "TextSecondary", self, align=Qt.AlignmentFlag.AlignRight)
        self.btn_back = IconButton(t, self.tips, G_COLLAPSE, size=32, small_glyph=True, parent=self,
                                   tooltip=BACK_TIP, name="Back to the panel", focusable=True)
        self.hint = SheetHint(t, self)
        self.btn_back.clicked.connect(lambda: self.closeRequested.emit("back"))

        self._decel, self._exit = decelerate(), exit_curve()
        self._t = Tween(self, t, 1.0, SLOW)
        self._t.on_change = self._on_motion
        self._away_timer = QTimer(self)
        self._away_timer.setSingleShot(True)
        self._away_timer.timeout.connect(self._check_away)
        t.changed.connect(self._on_theme)

        self._apply_window_theme()
        size = self.natural_size()  # the content is laid out when the sheet opens: subclasses aren't built yet
        self.resize(size)
        self.body.setGeometry(0, 0, size.width(), size.height())
        self._place_chrome(size.width(), size.height())

    # --- For subclasses ------------------------------------------------------------------------

    def layout_content(self, W: int, H: int) -> None:
        """Place the content in `body` (which covers the whole window) for a W x H sheet."""

    def focus_target(self) -> QWidget | None:
        """The widget that takes the caret when the sheet comes to the front."""
        return None

    def primary(self) -> None:
        """Enter: the sheet's main action."""

    # --- Chrome --------------------------------------------------------------------------------

    def natural_size(self) -> QSize:
        return QSize(self.theme.px(self.natural.width()), self.theme.px(self.natural.height()))

    def set_title(self, text: str) -> None:
        self.title.set_text(text)

    def set_info(self, text: str) -> None:
        self.info.set_text(text)
        self._place_chrome(self.width(), self.height())

    def set_hint(self, parts: list[Part]) -> None:
        """The keycap hint the row shows when no note is up."""
        self._hint = list(parts)
        if not self.hint.note:
            self.hint.set_content("hint", self._hint)

    def show_note(self, text: str) -> None:
        self.hint.set_content("note", text)

    def end_note(self) -> None:
        self.hint.set_content("hint", self._hint)

    def footer_rect(self) -> QRect:
        """The footer row: 40 DIP tall, centred on H - 32."""
        return sheet_chrome(self.theme, self.width(), self.height())["footer"]

    def _place_chrome(self, W: int, H: int) -> None:
        lay = sheet_chrome(self.theme, W, H, self.info.content_width() if self.info.parts else 0.0)
        self.glyph.setGeometry(lay["glyph"])
        self.title.setGeometry(lay["title"])
        self.info.setGeometry(lay["info"])
        self.btn_back.place(lay["back"])
        self.hint.setGeometry(lay["hint"])

    def _layout_at(self, W: int, H: int) -> None:
        self.body.setGeometry(0, 0, W, H)
        self._place_chrome(W, H)
        self.layout_content(W, H)
        self.update()

    # --- Opening and closing -------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.session.phase in ("opening", "open")

    def open_at(self, rest: QRect, *, return_to: int, panel_visible: bool = True, source: str = "") -> bool:
        """Show the sheet over the panel's resting rect and take the foreground; True if Windows let it. Call it
        synchronously in the click, hotkey or tray handler: Windows lets a window come to the front only then.
        If it is refused, the hint row asks for a click. On a sheet that is already open it just activates."""
        s = self.session
        if s.phase in ("opening", "open"):
            return self.activate()
        if s.phase == "closing":
            self._finish_close()
            s = self.session
        s.phase, s.return_to, s.source = "opening", return_to, source
        self._panel_visible = panel_visible
        self._place_at(rest)
        self._motion = "open"
        self.setWindowOpacity(0.0)
        self._t.snap(0.0)
        self.show()
        s.was_active = self.activate()
        if not s.was_active:
            self.show_note(ACTIVATION_NOTE)
        self._t.to(1.0, SLOW if self.mode == "cover" else MEDIUM)
        return s.was_active

    def close_animated(self, reason: str) -> None:
        """Fade out, then hide and emit `closed(reason)`. "quit" and "switch" hide at once."""
        if self.session.phase in ("closed", "closing"):
            return
        if self._motion == "open":
            self._t.snap(1.0)  # finish the opening first; the close starts from the open sheet
        self.session.phase = "closing"
        self._reason = reason
        self._cover(False)
        log.info("%s closed (%s)", type(self).__name__, reason)
        if reason in ("quit", "switch"):
            self._finish_close()
            return
        self._motion = "close"
        self._t.snap(0.0)
        self._t.to(1.0, CLOSE_MS)

    def _finish_close(self) -> None:
        self._motion = ""
        self.hide()
        self._set_content(1.0)
        self.end_note()
        reason, self.session = self._reason, SheetSession()
        self.closed.emit(reason)

    def activate(self) -> bool:
        """Bring the sheet to the front with the caret in its field; True if Windows let it (trust only that)."""
        self.activateWindow()
        target = self.focus_target()
        if target is not None:
            target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        win32.set_topmost(self.hwnd)
        if not self.is_foreground():
            win32.set_foreground_window(self.hwnd)
        return self.is_foreground()

    def is_foreground(self) -> bool:
        return win32.get_foreground_window() == self.hwnd

    def is_dismissed_by(self, hwnd: int) -> bool:
        """Whether `hwnd` coming to the front should close the sheet (light dismiss, as for the Type or paste box)."""
        return is_light_dismiss(self.session.phase, self.session.was_active, hwnd, win32.get_window_pid(hwnd),
                                win32.get_class_name(hwnd), own_pid=os.getpid())

    def reanchor(self, rest: QRect) -> None:
        """Fit the open sheet to the panel's resting rect at once, with no motion (after a screen, DPI, text size
        or theme change)."""
        if not self.is_open:
            return
        if self._motion:
            self._t.snap(1.0)
        self._place_at(rest)

    def _geometry(self, rest: QRect) -> QRect:
        t, panel = self.theme, self.ctl.panel
        screen = QGuiApplication.screenAt(rest.center()) or QGuiApplication.primaryScreen()
        snap_x = (self.ctl.settings.get("panel.position") or {}).get("snap_x")
        return sheet_rect(rest, screen.availableGeometry(), self.natural_size(), panel.size(), mode=self.mode,
                          snap_x=snap_x, margin=t.px(12), bottom_margin=panel._bottom_margin(screen))

    def _place_at(self, rest: QRect) -> None:
        rect = self._geometry(rest)
        self._layout_at(rect.width(), rect.height())
        self.setGeometry(rect)

    # --- Motion --------------------------------------------------------------------------------

    def _on_motion(self, v: float) -> None:
        if self._motion == "open":
            if self.mode == "panel":  # opacity only
                ms, opacity = v * MEDIUM, self._decel.valueForProgress(v)
            else:
                ms = v * SLOW
                opacity = self._decel.valueForProgress(min(1.0, ms / MEDIUM))
                self._set_content(min(1.0, max(0.0, (ms - FAST) / (SLOW - FAST))))
            if ms >= MEDIUM:
                self._cover(True)
            if v >= 1.0:
                self._motion = ""
                if self.session.phase == "opening":
                    self.session.phase = "open"
                    if not self.is_foreground():  # a click away while it faded in had to wait until now
                        self._away_timer.start(AWAY_SETTLE_MS)
        elif self._motion == "close":
            opacity = 1.0 - self._exit.valueForProgress(v)
            if v >= 1.0:
                self._finish_close()
                return
        else:
            return
        self.setWindowOpacity(1.0 if opacity >= 0.999 else max(0.0, opacity))

    def _set_content(self, q: float) -> None:
        if q >= 1.0:
            self._body_fade.setEnabled(False)
        else:
            self._body_fade.setOpacity(q)
            self._body_fade.setEnabled(True)

    def _cover(self, on: bool) -> None:
        if on and not self._panel_visible:
            return
        if on != self._covered:
            self._covered = on
            self.ctl.panel.set_covered(on)

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
        for w in self.findChildren(QWidget):
            relayout = getattr(w, "relayout", None)
            if relayout:
                relayout()
        if self.is_open:
            self.reanchor(self.ctl.panel.rest_rect())
        else:
            size = self.natural_size()
            self._layout_at(size.width(), size.height())

    # --- Events --------------------------------------------------------------------------------

    def reject(self) -> None:
        # QDialog's Esc; the sheet stays until the App closes it.
        self.closeRequested.emit("esc")

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
            return True, MA_NOACTIVATE  # a click on the fading sheet never brings it back to the front
        if msg.message in (WM_NCACTIVATE, WM_ACTIVATEAPP) and msg.wParam:
            # After a refused activation Qt already counts the sheet as active; ask Windows once it settles.
            QTimer.singleShot(0, self._on_front)
        return super().nativeEvent(event_type, message)

    def _on_front(self) -> None:
        """The sheet may have come to the front: from then on light dismiss applies and the activation note goes."""
        s = self.session
        if s.phase in ("opening", "open") and self.is_foreground():
            s.was_active = True
            if self.hint.note and self.hint.content[1] == ACTIVATION_NOTE:
                self.end_note()

    def _check_away(self) -> None:
        if self.is_dismissed_by(win32.get_foreground_window()):
            self.closeRequested.emit("away")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Keys a focused field or button passes on.
        alt = event.modifiers() & Qt.KeyboardModifier.AltModifier
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not alt:
            self.primary()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event) -> None:
        if self.is_open:
            event.ignore()  # Alt+F4 is Back
            self.closeRequested.emit("back")
        else:  # already on its way out: never hold up the close Qt sends every window when the app quits
            event.accept()

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), self.theme.color("Surface"))


# --- Native file dialogs -----------------------------------------------------------------------

class DialogHost(QWidget):
    """The owner of a native Open or Save dialog.

    The panel never takes focus, and a dialog without an owner can open behind it. The host is a 1 x 1 unowned
    focusable Tool window, topmost and at opacity 0, over the panel's centre. Activated inside the click, it owns
    a window-modal QFileDialog opened with open(), so the panel stays usable. When the dialog closes the host
    hands the foreground back to `return_to` and hides, then the chosen path (if any) goes to `on_chosen`.
    Paths are never logged."""

    closed = Signal()

    def __init__(self) -> None:
        super().__init__(None, SHEET_FLAGS)
        self.setWindowTitle("Saylight")
        self.setWindowOpacity(0.0)
        self.hwnd = int(self.winId())
        self.dialog: QFileDialog | None = None
        self._return_to = 0
        self._on_chosen: Callable[[str], None] | None = None

    @property
    def busy(self) -> bool:
        return self.dialog is not None

    def open_file(self, at: QPoint, return_to: int, on_chosen: Callable[[str], None], *, caption: str,
                  filters: list[str], directory: str = "") -> bool:
        """Show an Open dialog for one existing file. False if a dialog is already up (it comes to the front)."""
        return self._run(at, return_to, on_chosen, caption, filters, directory, save=False)

    def save_file(self, at: QPoint, return_to: int, on_chosen: Callable[[str], None], *, caption: str,
                  filters: list[str], directory: str = "", default_suffix: str = "") -> bool:
        """Show a Save dialog. False if a dialog is already up (it comes to the front)."""
        return self._run(at, return_to, on_chosen, caption, filters, directory, save=True,
                         default_suffix=default_suffix)

    def _run(self, at: QPoint, return_to: int, on_chosen: Callable[[str], None], caption: str,
             filters: list[str], directory: str, *, save: bool, default_suffix: str = "") -> bool:
        if self.dialog is not None:
            self._activate()
            self.dialog.raise_()
            return False
        self._return_to, self._on_chosen = return_to, on_chosen
        self.setGeometry(at.x(), at.y(), 1, 1)
        self.show()
        self._activate()
        d = QFileDialog(self, caption, directory, ";;".join(filters))
        if save:
            d.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            d.setFileMode(QFileDialog.FileMode.AnyFile)
            if default_suffix:
                d.setDefaultSuffix(default_suffix)
        else:
            d.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
            d.setFileMode(QFileDialog.FileMode.ExistingFile)
        d.setWindowModality(Qt.WindowModality.WindowModal)
        d.finished.connect(self._finished)
        self.dialog = d
        d.open()
        return True

    def _activate(self) -> None:
        self.activateWindow()
        win32.set_topmost(self.hwnd)
        if win32.get_foreground_window() != self.hwnd:
            win32.set_foreground_window(self.hwnd)

    def _finished(self, result: int) -> None:
        d, self.dialog = self.dialog, None
        if d is None:
            return
        files = d.selectedFiles() if result == QDialog.DialogCode.Accepted.value else []
        if self._return_to:
            win32.set_foreground_window(self._return_to)
        self.hide()
        d.deleteLater()
        on_chosen, self._on_chosen = self._on_chosen, None
        if files and files[0] and on_chosen:
            on_chosen(os.path.normpath(files[0]))
        self.closed.emit()
