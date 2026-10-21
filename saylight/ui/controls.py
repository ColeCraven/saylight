"""Custom-painted controls for the panel. No QSS; every colour comes from the theme."""

from __future__ import annotations

import math
from typing import Callable, Iterable, NamedTuple

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontInfo, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..theme import READING_PRESETS, composite
from .motion import FAST, MEDIUM, SLOW, Clock, Tween, decelerate, mix, with_alpha

# Segoe Fluent Icons code points (all present in the font, and all but G_SCREEN in Segoe MDL2 Assets too: draw
# that one through icon()).
G_PLAY = ""
G_PAUSE = ""
G_RETRY = ""
G_PREV = ""
G_NEXT = ""
G_HIDE = ""
G_MINUS = ""
G_PLUS = ""
G_VOL = ("", "", "", "")
G_VOL_LOW = ""
G_OFFLINE = ""
G_ERROR = ""
G_NOTHING = ""
G_BUSY = ""
G_SETTINGS = ""
G_EXPAND = ""     # FullScreen
G_COLLAPSE = ""   # BackToWindow
G_BACK = ""
G_CHECK = ""
G_TYPE = ""
G_CLEAR = ""
G_UNDO = ""
G_PASTE = ""
G_DOWNLOAD = ""
G_OPEN = ""      # OpenInNewWindow
G_CHEVRON = ""   # ChevronRight
G_MORE = ""
G_OPEN_FILE = ""   # FolderOpen (G_OPEN is OpenInNewWindow, for links)
G_DOCUMENT = ""
G_SAVE = ""
G_DONE = ""
G_FOLDER = ""
G_SPEECH = ""
G_HISTORY = ""
G_LOCK = ""
G_EYE = ""         # show password
G_EYE_OFF = ""     # hide password
G_TEST = ""        # a speaker, not Play
G_DELETE = ""
G_FIND = ""
G_IMPORT = ""
G_EXPORT = ""
G_CANCEL = ""
G_ARROW = ""       # word -> said as
G_SCREEN = ""      # Read from screen, everywhere it appears
G_SCREEN_MDL2 = ""  # G_SCREEN's stand-in where only Segoe MDL2 Assets is installed
G_LANGUAGE = ""    # language settings
G_SCAN = ""        # a scanned PDF
G_PAGES = ""       # choosing pages
_MDL2_STAND_INS = {G_SCREEN: G_SCREEN_MDL2}

TRANSPARENT = QColor(0, 0, 0, 0)


def icon(theme, glyph: str) -> str:
    """The glyph to draw with the theme's icon font: `glyph`, or its Segoe MDL2 Assets stand-in when the icon
    font resolved to anything but Segoe Fluent Icons (Windows 10 without it)."""
    stand_in = _MDL2_STAND_INS.get(glyph)
    if stand_in is None or QFontInfo(theme.font("icon16")).family() == "Segoe Fluent Icons":
        return glyph
    return stand_in


def volume_glyph(v: int) -> str:
    if v <= 0:
        return G_VOL[0]
    return G_VOL[1] if v <= 33 else G_VOL[2] if v <= 66 else G_VOL[3]


def draw_glyph(p: QPainter, rect: QRectF, glyph: str, font, color: QColor, dx: float = 0.0) -> None:
    p.setFont(font)
    p.setPen(color)
    p.drawText(rect.translated(dx, 0), int(Qt.AlignmentFlag.AlignCenter), glyph)


# --- Keycap hints ------------------------------------------------------------------------------

Part = tuple[str, str]  # ("text", "..."), ("key", "Ctrl"), ("glyph", G_PLAY): a round chip with an icon,
#                         or ("action", "type"): a clickable chip from ACTION_PARTS
CAP_H = {"small": 16, "body": 18, "message": 20}  # keycap and glyph-chip height in DIP for each text role
# key: (glyph, label). "continue" and "scan_all" are always given their labels when painted ("Continue Smith 2024
# from page 14", "All 160 pages").
ACTION_PARTS = {"type": (G_TYPE, "Type or paste text"), "undo": (G_UNDO, "Undo"),
                "open": (G_OPEN_FILE, "Open a file"), "continue": (G_HISTORY, "Continue"),
                "password": (G_LOCK, "Enter password"), "retry": (G_RETRY, "Try again"),
                "screen": (G_SCREEN, "Read from screen"), "screen_retry": (G_SCREEN, "Try again"),
                "scan_retry": (G_RETRY, "Try again"), "lang_settings": (G_LANGUAGE, "Open language settings"),
                "scan_first": (G_PLAY, "First 50 pages"), "scan_all": (G_PLAY, "All pages"),
                "scan_choose": (G_PAGES, "Choose pages…")}


class ActionState(NamedTuple):
    """How an action chip is drawn: hover and press amounts (0-1) and whether it can be clicked."""
    hover: float = 0.0
    pressed: float = 0.0
    enabled: bool = True


def spoken_parts(parts: list[Part]) -> str:
    """The parts as one accessible name, with " + " between keycaps: "Ctrl + ` reads the highlighted text"."""
    out = ""
    for i, (kind, s) in enumerate(parts):
        if kind == "key" and i and parts[i - 1][0] == "key":
            out += " + "
        out += s
    return out


def _keycap_width(theme, label: str) -> float:
    w = QFontMetricsF(theme.font("keycap")).horizontalAdvance(label) + 2 * theme.px(6)
    return max(float(theme.px(20)), w)


def action_label(key: str, labels: dict[str, str] | None = None) -> str:
    """An action chip's label: the override in `labels`, else its ACTION_PARTS label."""
    return (labels or {}).get(key) or ACTION_PARTS[key][1]


def _part_width(theme, kind: str, s: str, role: str, labels: dict[str, str] | None = None) -> float:
    if kind == "key":
        return _keycap_width(theme, s)
    if kind == "glyph":
        return float(theme.px(CAP_H.get(role, 18)))
    if kind == "action":  # glyph at +10, label at +32, 10 of right padding
        return theme.px(32) + QFontMetricsF(theme.font(role)).horizontalAdvance(action_label(s, labels)) + theme.px(10)
    return QFontMetricsF(theme.font(role)).horizontalAdvance(s)


def measure_parts(theme, parts: list[Part], *, role: str = "body", labels: dict[str, str] | None = None) -> float:
    """The parts' width on one line; `labels` overrides action chip labels by key, as in paint_parts."""
    w = 0.0
    for i, (kind, s) in enumerate(parts):
        w += _part_width(theme, kind, s, role, labels)
        if kind != "text" and i + 1 < len(parts) and parts[i + 1][0] != "text":
            w += theme.px(4)
    return w


def paint_parts(p: QPainter, theme, x: float, line_top: float, line_h: float, parts: list[Part],
                color: QColor, *, role: str = "body", caps: tuple[QColor, QColor, QColor] | None = None,
                actions: dict[str, ActionState] | None = None, disabled: QColor | None = None,
                hits: dict[str, QRectF] | None = None, labels: dict[str, str] | None = None) -> float:
    """Paint text with inline keycaps and chips on one line; returns the x after the last part.

    `caps` is (fill, stroke, ink) for the keycaps and chips; the app-theme keycap tokens by default.
    `actions` gives each action chip's state, `disabled` the ink of a disabled one (TextDisabled by default),
    `hits`, when given, receives each action chip's rect by key, and `labels` overrides action chip labels by key
    (the Continue chip names its file)."""
    font = theme.font(role)
    fm = QFontMetricsF(font)
    baseline = line_top + (line_h - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
    mid = baseline - fm.ascent() + (fm.ascent() + fm.descent()) / 2
    cap_h = theme.px(CAP_H.get(role, 18))
    fill, stroke, ink = caps or (theme.color("KeycapFill"), theme.color("KeycapStroke"), theme.color("TextPrimary"))
    for i, (kind, s) in enumerate(parts):
        w = _part_width(theme, kind, s, role, labels)
        if kind == "text":
            p.setFont(font)
            p.setPen(color)
            p.drawText(QPointF(x, baseline), s)
        elif kind == "action":
            state = (actions or {}).get(s, ActionState())
            r = QRectF(x, mid - (cap_h + theme.px(4)) / 2, w, cap_h + theme.px(4))
            _paint_action(p, theme, r, baseline, s, state, (fill, stroke, ink), font,
                          disabled or theme.color("TextDisabled"), action_label(s, labels))
            if hits is not None:
                hits[s] = r
        else:
            r = QRectF(x, mid - cap_h / 2, w, cap_h)
            p.setPen(QPen(stroke, 0))
            p.setBrush(fill)
            if kind == "glyph":
                p.drawEllipse(r.adjusted(0.5, 0.5, -0.5, -0.5))
                draw_glyph(p, r, s, theme.font("icon12"), ink, theme.k if s == G_PLAY else 0.0)
            else:
                p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), theme.px(4), theme.px(4))
                if s == "`":  # the grave accent is a speck in any font: draw it as a clear diagonal stroke
                    u, c = cap_h / 18, r.center()
                    pen = QPen(ink, 1.75 * u)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    p.setPen(pen)
                    p.drawLine(QPointF(c.x() - 2.0 * u, c.y() - 2.5 * u), QPointF(c.x() + 2.0 * u, c.y() + 1.5 * u))
                else:
                    p.setFont(theme.font("keycap"))
                    p.setPen(ink)
                    p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), s)
        x += w
        if kind != "text" and i + 1 < len(parts) and parts[i + 1][0] != "text":
            x += theme.px(4)
    return x


def _paint_action(p: QPainter, t, r: QRectF, baseline: float, key: str, state: ActionState,
                  caps: tuple[QColor, QColor, QColor], font, disabled_ink: QColor, label: str) -> None:
    """An action chip: keycap colours, a 16 DIP glyph and the label on the line's baseline."""
    fill, stroke, ink = caps
    glyph = icon(t, ACTION_PARTS[key][0])
    hover, pressed = (state.hover, state.pressed) if state.enabled else (0.0, 0.0)
    face = fill
    if not t.high_contrast:
        face = mix(mix(fill, composite(ink, 0.06, fill), hover), composite(ink, 0.10, fill), pressed)
    p.setPen(QPen(stroke, 0))
    p.setBrush(face)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), t.px(4), t.px(4))
    hc_outline(p, t, r, t.px(4), max(hover, pressed))
    color = ink if state.enabled else disabled_ink
    draw_glyph(p, QRectF(r.left() + t.px(10), r.center().y() - t.px(8), t.px(16), t.px(16)), glyph,
               t.font("icon16"), color)
    p.setFont(font)
    p.setPen(color)
    p.drawText(QPointF(r.left() + t.px(32), baseline), label)


def paint_focus_ring(p: QPainter, theme, rect: QRectF, radius: float) -> None:
    """The keyboard focus ring around a control's `rect` (corner `radius`): a 1 DIP Surface gap, then 2 DIP of
    TextPrimary, so the outer edge sits 3 DIP out with radius + 3."""
    k = theme.k
    p.save()
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(theme.color("Surface"), k))
    p.drawRoundedRect(rect.adjusted(-k / 2, -k / 2, k / 2, k / 2), radius + k / 2, radius + k / 2)
    p.setPen(QPen(theme.color("TextPrimary"), 2 * k))
    p.drawRoundedRect(rect.adjusted(-2 * k, -2 * k, 2 * k, 2 * k), radius + 2 * k, radius + 2 * k)
    p.restore()


def hc_outline(p: QPainter, t, rect: QRectF, radius: float, amount: float, token: str = "HoverOutline") -> None:
    """In high contrast, hover (or `token`) is a 1 px outline instead of a fill."""
    if t.high_contrast and amount > 0.5:
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(t.color(token), 0))
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.restore()


# --- Base control ------------------------------------------------------------------------------

FOCUS_PAD = 3  # DIP around a focusable control's face: the 1 DIP gap and the 2 DIP focus ring


class Control(QWidget):
    """Hover/press tracking, tooltips and accessibility shared by every control.

    Never takes focus, unless `focusable` (only in a window that may take focus): then Tab reaches it, Space or
    Enter clicks it, and keyboard focus draws a ring in the FOCUS_PAD kept around the control's face."""

    clicked = Signal()

    def __init__(self, theme, tips, parent: QWidget | None = None, tooltip: str | Callable[[], str] = "", *,
                 focusable: bool = False):
        super().__init__(parent)
        self.theme = theme
        self.tips = tips
        self.focusable = focusable
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus if focusable else Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.hover = Tween(self, theme, 0.0, FAST)
        self.press = Tween(self, theme, 0.0, FAST)
        self._pressed = False
        self._ring = False
        self._tooltip = tooltip
        theme.changed.connect(self.update)

    # A focusable control's widget is FOCUS_PAD larger on every side than the face it paints.
    def pad(self) -> int:
        return self.theme.px(FOCUS_PAD) if self.focusable else 0

    def face(self) -> QRectF:
        m = self.pad()
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def place(self, rect: QRect) -> None:
        """Put the control's face at `rect` (parent coordinates)."""
        m = self.pad()
        self.setGeometry(rect.adjusted(-m, -m, m, m))

    def focus_ring_shown(self) -> bool:
        return self._ring and self.hasFocus()

    def focusInEvent(self, event) -> None:
        self._ring = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._ring = False
        self.update()
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:
        plain = event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
        keys = (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if self.focusable and plain and event.key() in keys and self.isEnabled():
            if not event.isAutoRepeat():
                self.press.snap(1.0)
                self.press.to(0.0)
                self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # Tooltip text is re-evaluated each time so it always reflects the current state.
    def set_tooltip(self, tooltip: str | Callable[[], str]) -> None:
        self._tooltip = tooltip
        self.tips.refresh(self)
        self.setAccessibleDescription(self.tooltip_text())

    def tooltip_text(self) -> str:
        return self._tooltip() if callable(self._tooltip) else self._tooltip

    def changeEvent(self, event) -> None:
        if event.type() == event.Type.EnabledChange and not self.isEnabled():
            self.hover.snap(0.0)
            self.press.snap(0.0)
            self._pressed = False
        super().changeEvent(event)

    def enterEvent(self, event) -> None:
        if self.isEnabled():
            self.hover.to(1.0)
        if self.tooltip_text():
            self.tips.hover(self, self.tooltip_text)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover.to(0.0)
        self.tips.leave(self)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._pressed = True
            self._ring = False  # the ring is for keyboard use only
            self.press.to(1.0)
            self.tips.press(self)
            event.accept()
            return
        event.ignore()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.press.to(0.0)
            if self.face().contains(event.position()) and self.isEnabled():
                self.clicked.emit()
            event.accept()
            return
        event.ignore()

    def mouseDoubleClickEvent(self, event) -> None:
        # Treat a fast second click as another click instead of swallowing it.
        self.mousePressEvent(event)


class IconButton(Control):
    """Subtle 32/36 DIP button: radius-4 plate on hover, glyph in TextPrimary."""

    def __init__(self, theme, tips, glyph: str, *, size: int = 36, small_glyph: bool = False,
                 parent: QWidget | None = None, tooltip: str | Callable[[], str] = "", name: str = "",
                 focusable: bool = False):
        super().__init__(theme, tips, parent, tooltip, focusable=focusable)
        self.glyph = glyph
        self.size_dip = size
        self.small_glyph = small_glyph
        self.selected = False
        self.painter_glyph: Callable[[QPainter, QRectF, QColor], None] | None = None
        if name:
            self.setAccessibleName(name)
        self.relayout()

    def relayout(self) -> None:
        side = self.theme.px(self.size_dip) + 2 * self.pad()
        self.setFixedSize(side, side)

    def set_glyph(self, glyph: str) -> None:
        if glyph != self.glyph:
            self.glyph = glyph
            self.update()

    def set_selected(self, on: bool) -> None:
        if on != self.selected:
            self.selected = on
            self.update()

    def glyph_color(self) -> QColor:
        t = self.theme
        if not self.isEnabled():
            return t.color("TextDisabled")
        if self.selected:
            return t.color("Accent")
        return mix(t.color("TextPrimary"), t.color("TextSecondary"), self.press.value)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.face()
        base = t.color("SubtleSelected") if self.selected else TRANSPARENT
        fill = mix(mix(base, t.color("SubtleHover"), self.hover.value), t.color("SubtlePressed"), self.press.value)
        if self.selected and self.hover.value == 0 and self.press.value == 0:
            fill = base
        if fill.alpha() > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            radius = t.px(4)
            p.drawRoundedRect(r, radius, radius)
            if t.high_contrast and self.hover.value > 0.5:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(t.color("Accent"), 0))
                p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        color = self.glyph_color()
        if self.painter_glyph:
            self.painter_glyph(p, r, color)
        else:
            draw_glyph(p, r, self.glyph, t.font("icon12" if self.small_glyph else "icon16"), color)
        if self.focus_ring_shown():
            paint_focus_ring(p, t, r, t.px(4))


def paint_stop_square(theme):
    def paint(p: QPainter, r: QRectF, color: QColor) -> None:
        side = theme.k * 10
        sq = QRectF(r.center().x() - side / 2, r.center().y() - side / 2, side, side)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(sq, theme.k * 2, theme.k * 2)
    return paint


class PlayButton(Control):
    """40 DIP accent circle with a crossfading glyph and an optional buffering ring (48 DIP widget)."""

    def __init__(self, theme, tips, clock: Clock, parent: QWidget | None = None):
        super().__init__(theme, tips, parent)
        self.clock = clock
        self.glyph = G_PLAY
        self._prev_glyph = ""
        self.swap = Tween(self, theme, 1.0, 120)
        self.ring = Tween(self, theme, 0.0, MEDIUM)
        self.ring.on_change = self._ring_changed
        self._ring_wanted = False
        self._t = 0.0
        clock.tick.connect(self._on_tick)
        self.setAccessibleName("Play")
        self.relayout()

    def relayout(self) -> None:
        self.setFixedSize(self.theme.px(48), self.theme.px(48))

    def set_glyph(self, glyph: str, name: str) -> None:
        self.setAccessibleName(name)
        if glyph == self.glyph:
            return
        self._prev_glyph = self.glyph
        self.glyph = glyph
        self.swap.snap(0.0)
        self.swap.to(1.0, curve=decelerate())

    def set_buffering(self, on: bool) -> None:
        on = on and not self.theme.reduced_motion
        if on == self._ring_wanted:
            return
        self._ring_wanted = on
        self.ring.to(1.0 if on else 0.0)
        if on:
            self.clock.subscribe(self)

    def _ring_changed(self, v: float) -> None:
        if v <= 0 and not self._ring_wanted:
            self.clock.unsubscribe(self)

    def _on_tick(self, now: float) -> None:
        if self.ring.value > 0 and self.isVisible():
            self._t = now
            self.update()

    def hideEvent(self, event) -> None:
        self.clock.unsubscribe(self)
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if self._ring_wanted:
            self.clock.subscribe(self)
        super().showEvent(event)

    def contains_circle(self, pos) -> bool:
        c = QPointF(self.width() / 2, self.height() / 2)
        return math.hypot(pos.x() - c.x(), pos.y() - c.y()) <= self.theme.px(20) + 1

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        radius = t.k * 20
        if self.isEnabled():
            fill = mix(mix(t.color("Accent"), t.color("AccentHover"), self.hover.value),
                       t.color("AccentPressed"), self.press.value)
            glyph_color = mix(t.color("OnAccent"), t.color("OnAccentPressed"), self.press.value)
        else:
            fill = t.color("AccentDisabled")
            glyph_color = t.color("TextDisabled")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawEllipse(c, radius, radius)
        if self.isEnabled():
            ring = radius + t.k
            hc_outline(p, t, QRectF(c.x() - ring, c.y() - ring, 2 * ring, 2 * ring), ring, self.hover.value)
        elif t.high_contrast:  # disabled is a GrayText outline, not Window on Window
            p.setPen(QPen(t.color("DisabledOutline"), 0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, radius - 0.5, radius - 0.5)

        font = t.font("icon16")
        box = QRectF(c.x() - radius, c.y() - radius, 2 * radius, 2 * radius)
        s = self.swap.value
        if s < 1.0 and self._prev_glyph:
            p.setOpacity(1.0 - s)
            draw_glyph(p, box, self._prev_glyph, font, glyph_color, t.k if self._prev_glyph == G_PLAY else 0)
        p.setOpacity(s)
        scale = 0.85 + 0.15 * s
        p.save()
        p.translate(c)
        p.scale(scale, scale)
        p.translate(-c)
        draw_glyph(p, box, self.glyph, font, glyph_color, t.k if self.glyph == G_PLAY else 0)
        p.restore()
        p.setOpacity(1.0)

        if self.ring.value > 0:
            ring_r = t.k * 23 - t.k
            pen = QPen(with_alpha(t.color("Accent"), self.ring.value), t.k * 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            rot = (self._t / 1.2) * 360.0
            sweep = 30 + 240 * (0.5 - 0.5 * math.cos(2 * math.pi * self._t / 1.6))
            rect = QRectF(c.x() - ring_r, c.y() - ring_r, 2 * ring_r, 2 * ring_r)
            p.drawArc(rect, int(-rot * 16), int(-sweep * 16))

    def mousePressEvent(self, event) -> None:
        if self.contains_circle(event.position()):
            super().mousePressEvent(event)
        else:
            event.ignore()


# --- Buttons for the Type or paste box (focusable) ---------------------------------------------

class PillButton(Control):
    """Read aloud: Play's accent fill grown into a 40 DIP pill with a glyph and a label."""

    def __init__(self, theme, tips, glyph: str, label: str, parent: QWidget | None = None,
                 tooltip: str | Callable[[], str] = "", name: str = ""):
        super().__init__(theme, tips, parent, tooltip, focusable=True)
        self.glyph = glyph
        self.label = label
        self.label_opacity = 1.0
        self.on = Tween(self, theme, 1.0, FAST)  # enabled amount
        self.setAccessibleName(name or label)

    def natural_size(self) -> QSize:
        """The face: glyph at +10, label at +34 and 16 of right padding, 40 tall."""
        t = self.theme
        label_w = QFontMetricsF(t.font("status")).horizontalAdvance(self.label)
        return QSize(math.ceil(t.px(34) + label_w + t.px(16)), t.px(40))

    def set_label_opacity(self, v: float) -> None:
        """Fade the label alone, while the pill grows out of the Play circle."""
        self.label_opacity = max(0.0, min(1.0, v))
        self.update()

    def changeEvent(self, event) -> None:
        if event.type() == event.Type.EnabledChange:
            self.on.to(1.0 if self.isEnabled() else 0.0)
        super().changeEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.face()
        radius = r.height() / 2
        live = mix(mix(t.color("Accent"), t.color("AccentHover"), self.hover.value), t.color("AccentPressed"),
                   self.press.value)
        live_ink = mix(t.color("OnAccent"), t.color("OnAccentPressed"), self.press.value)
        fill = mix(t.color("AccentDisabled"), live, self.on.value)
        ink = mix(t.color("TextDisabled"), live_ink, self.on.value)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(r, radius, radius)
        if t.high_contrast and self.isEnabled():  # hover rings the Highlight fill 1 DIP outside it
            hc_outline(p, t, r.adjusted(-t.k, -t.k, t.k, t.k), radius + t.k, self.hover.value)
        elif t.high_contrast:  # disabled is a GrayText outline, not Window on Window
            p.setPen(QPen(t.color("DisabledOutline"), 0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)

        shape = QPainterPath()
        shape.addRoundedRect(r, radius, radius)
        p.save()
        p.setClipPath(shape)
        # The glyph moves from the circle's centre to its +10 column as the pill grows to its full width.
        g = t.px(16)
        full = self.natural_size().width()
        grown = max(0.0, min(1.0, (r.width() - r.height()) / max(1.0, full - r.height())))
        gx = (r.width() - g) / 2 * (1.0 - grown) + t.px(10) * grown
        draw_glyph(p, QRectF(r.left() + gx, r.center().y() - g / 2, g, g), self.glyph, t.font("icon16"), ink,
                   t.k if self.glyph == G_PLAY else 0.0)
        if self.label_opacity > 0:
            font = t.font("status")
            fm = QFontMetricsF(font)
            p.setOpacity(self.label_opacity)
            p.setFont(font)
            p.setPen(ink)
            baseline = r.top() + (r.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
            p.drawText(QPointF(r.left() + t.px(34), baseline), self.label)
        p.restore()
        if self.focus_ring_shown():
            paint_focus_ring(p, t, r, radius)


def text_button_size(theme, labels: Iterable[str]) -> QSize:
    """A TextButton face wide enough for the longest label: glyph at +12, label at +36, 14 of right padding."""
    fm = QFontMetricsF(theme.font("body"))
    widest = max((fm.horizontalAdvance(s) for s in labels), default=0.0)
    return QSize(math.ceil(theme.px(36) + widest + theme.px(14)), theme.px(32))


class TextButton(Control):
    """A 32 DIP button with a glyph and a label; a change of content crossfades in 120 ms.

    "standard" uses the control fill and stroke (Clear / Undo in the box's footer); "chip" uses the reading
    tint's keycap colours (Paste, inside the well)."""

    def __init__(self, theme, tips, glyph: str, label: str, *, style: str = "standard",
                 parent: QWidget | None = None, tooltip: str | Callable[[], str] = "", name: str = ""):
        super().__init__(theme, tips, parent, tooltip, focusable=True)
        self.style = style
        self.glyph = glyph
        self.label = label
        self._prev: tuple[str, str] | None = None
        self.swap = Tween(self, theme, 1.0, 120)
        self.setAccessibleName(name or label)

    def set_content(self, glyph: str, label: str, name: str = "") -> None:
        if (glyph, label) != (self.glyph, self.label):
            self._prev = (self.glyph, self.label)
            self.glyph, self.label = glyph, label
            self.swap.snap(0.0)
            self.swap.to(1.0)
        self.setAccessibleName(name or label)

    def natural_size(self) -> QSize:
        return text_button_size(self.theme, (self.label,))

    def _colors(self) -> tuple[QColor, QColor, QColor, QColor]:
        """(fill, stroke, ink, disabled ink) at the current hover and press."""
        t = self.theme
        hv, pr = self.hover.value, self.press.value
        if self.style == "chip":
            c = t.reading_colors()
            base, ink = c["keycap_fill"], c["keycap_text"]
            fill = base if t.high_contrast else mix(mix(base, composite(ink, 0.06, base), hv),
                                                    composite(ink, 0.10, base), pr)
            return fill, c["keycap_stroke"], ink, t.color("TextDisabled") if t.high_contrast else c["secondary"]
        fill = mix(mix(t.color("ControlFill"), t.color("StepperHover"), hv), t.color("StepperPressed"), pr)
        ink = mix(t.color("TextPrimary"), t.color("TextSecondary"), pr)
        return fill, t.color("ControlStroke"), ink, t.color("TextDisabled")

    def _paint_content(self, p: QPainter, r: QRectF, glyph: str, label: str, color: QColor) -> None:
        t = self.theme
        draw_glyph(p, QRectF(r.left() + t.px(12), r.center().y() - t.px(8), t.px(16), t.px(16)), glyph,
                   t.font("icon16"), color)
        font = t.font("body")
        fm = QFontMetricsF(font)
        p.setFont(font)
        p.setPen(color)
        baseline = r.top() + (r.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
        p.drawText(QPointF(r.left() + t.px(36), baseline), label)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.face()
        radius = t.px(4)
        fill, stroke, ink, disabled_ink = self._colors()
        if t.high_contrast and not self.isEnabled():
            stroke = t.color("DisabledOutline")
        p.setPen(QPen(stroke, 0))
        p.setBrush(fill)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        hc_outline(p, t, r, radius, max(self.hover.value, self.press.value))
        color = ink if self.isEnabled() else disabled_ink
        s = self.swap.value
        if s < 1.0 and self._prev:
            p.setOpacity(1.0 - s)
            self._paint_content(p, r, *self._prev, color)
        p.setOpacity(s)
        self._paint_content(p, r, self.glyph, self.label, color)
        p.setOpacity(1.0)
        if self.focus_ring_shown():
            paint_focus_ring(p, t, r, radius)


# --- Speed stepper -----------------------------------------------------------------------------

class SpeedStepper(Control):
    """[ −  0.95×  + ] with hold-to-repeat, wheel and double-click-to-reset."""

    stepped = Signal(int)          # -1 or +1
    resetRequested = Signal()

    ZONES = ("minus", "value", "plus")

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(theme, tips, parent, tooltip=self._zone_tip)
        self.value_text = "0.95×"
        self.pending = Tween(self, theme, 0.0, MEDIUM)
        self.can_down = True
        self.can_up = True
        self.slower_tip = ""
        self.faster_tip = ""
        self.value_tip = "Reading speed. Scroll to change, double-click for 1.0×. Applies from the next sentence."
        self._zone: str | None = None
        self._press_zone: str | None = None
        self._zone_hover = {z: Tween(self, theme, 0.0, FAST) for z in ("minus", "plus")}
        self._repeat = QTimer(self)
        self._repeat.timeout.connect(self._on_repeat)
        self._wheel_acc = 0
        self.setAccessibleName("Reading speed")
        self.relayout()

    def relayout(self) -> None:
        self.setFixedSize(self.theme.px(112), self.theme.px(32))

    def set_value(self, text: str, *, pending: bool, can_down: bool, can_up: bool) -> None:
        self.value_text = text
        self.can_down, self.can_up = can_down, can_up
        self.pending.to(1.0 if pending else 0.0)
        self.setAccessibleName(f"Reading speed {text}")
        self.update()
        self.tips.refresh(self)

    def _zone_rects(self) -> dict[str, QRectF]:
        t = self.theme
        w32 = t.px(32)
        return {"minus": QRectF(0, 0, w32, self.height()),
                "value": QRectF(w32, 0, self.width() - 2 * w32, self.height()),
                "plus": QRectF(self.width() - w32, 0, w32, self.height())}

    def _zone_at(self, pos) -> str | None:
        for name, r in self._zone_rects().items():
            if r.contains(pos):
                return name
        return None

    def _zone_tip(self) -> str:
        if self._zone == "minus":
            return self.slower_tip
        if self._zone == "plus":
            return self.faster_tip
        return self.value_tip

    def _zone_enabled(self, zone: str | None) -> bool:
        return (zone == "minus" and self.can_down) or (zone == "plus" and self.can_up)

    def mouseMoveEvent(self, event) -> None:
        zone = self._zone_at(event.position())
        if zone != self._zone:
            self._zone = zone
            for z, tw in self._zone_hover.items():
                tw.to(1.0 if z == zone and self._zone_enabled(z) else 0.0)
            self.tips.refresh(self)
            if self.tooltip_text():
                self.tips.hover(self, self.tooltip_text)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._zone = None
        for tw in self._zone_hover.values():
            tw.to(0.0)
        self._repeat.stop()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        zone = self._zone_at(event.position())
        self.tips.press(self)
        if zone in ("minus", "plus") and self._zone_enabled(zone):
            self._press_zone = zone
            self.press.to(1.0)
            self.stepped.emit(-1 if zone == "minus" else 1)
            self._repeat.start(400)
        event.accept()

    def _on_repeat(self) -> None:
        zone = self._press_zone
        if zone and self._zone == zone and self._zone_enabled(zone):
            self.stepped.emit(-1 if zone == "minus" else 1)
            self._repeat.start(120)
        else:
            self._repeat.stop()

    def mouseReleaseEvent(self, event) -> None:
        self._repeat.stop()
        self._press_zone = None
        self.press.to(0.0)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if self._zone_at(event.position()) == "value":
            self.resetRequested.emit()
            event.accept()
        else:
            self.mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        self._wheel_acc += event.angleDelta().y()
        while abs(self._wheel_acc) >= 120:
            step = 1 if self._wheel_acc > 0 else -1
            self._wheel_acc -= 120 * step
            if (step < 0 and self.can_down) or (step > 0 and self.can_up):
                self.stepped.emit(step)
        event.accept()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(t.color("ControlStroke"), 0))
        p.setBrush(t.color("ControlFill"))
        p.drawRoundedRect(r, t.px(4), t.px(4))
        rects = self._zone_rects()
        inset = t.px(2)
        for zone, glyph, enabled in (("minus", G_MINUS, self.can_down), ("plus", G_PLUS, self.can_up)):
            zr = rects[zone]
            plate = zr.adjusted(inset, inset, -inset, -inset)
            hv = self._zone_hover[zone].value if enabled else 0.0
            pressed = self.press.value if self._press_zone == zone else 0.0
            fill = mix(mix(TRANSPARENT, t.color("StepperHover"), hv), t.color("StepperPressed"), pressed)
            if fill.alpha() > 0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(plate, t.px(3), t.px(3))
            hc_outline(p, t, plate, t.px(3), hv)
            color = t.color("TextPrimary") if enabled else t.color("TextDisabled")
            if pressed:
                color = mix(color, t.color("TextSecondary"), pressed)
            draw_glyph(p, zr, glyph, t.font("icon12"), color)
        value_color = mix(t.color("TextPrimary"), t.color("Accent"), self.pending.value)
        p.setFont(t.font("value"))
        p.setPen(value_color)
        p.drawText(rects["value"], int(Qt.AlignmentFlag.AlignCenter), self.value_text)


# --- Volume slider -----------------------------------------------------------------------------

class VolumeSlider(Control):
    """Inline 0–100 slider: accent fill, Fluent thumb, live percentage tip while dragging."""

    valueChanged = Signal(int)
    interacted = Signal()

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(theme, tips, parent, tooltip="")
        self.value = 100
        self._dragging = False
        self.thumb = Tween(self, theme, 0.0, FAST)  # 0 rest, 1 hover, -1 pressed
        self.setAccessibleName("Volume")
        self.relayout()

    def relayout(self) -> None:
        self.setFixedSize(self.theme.px(112), self.theme.px(32))

    def _rail(self) -> tuple[float, float, float]:
        t = self.theme
        x0 = t.px(10)
        x1 = self.width() - t.px(10)
        return x0, x1, self.height() / 2

    def _value_at(self, x: float) -> int:
        x0, x1, _ = self._rail()
        return round(max(0.0, min(1.0, (x - x0) / (x1 - x0))) * 100)

    def set_value(self, v: int) -> None:
        v = max(0, min(100, int(v)))
        if v != self.value:
            self.value = v
            self.setAccessibleName(f"Volume {v}%")
            self.update()

    def _commit(self, v: int) -> None:
        if v != self.value:
            self.set_value(v)
            self.valueChanged.emit(v)
        self.interacted.emit()
        x0, x1, cy = self._rail()
        tx = x0 + (x1 - x0) * self.value / 100
        self.tips.show_at(self, f"{self.value}%", QRect(int(tx) - 1, 0, 2, 2))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.press.to(1.0)
            self._commit(self._value_at(event.position().x()))
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._commit(self._value_at(event.position().x()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self.press.to(0.0)
            self.tips.press(self)
        event.accept()

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120
        if steps:
            self._commit(max(0, min(100, self.value + round(5 * steps))))
        event.accept()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x0, x1, cy = self._rail()
        h = t.k * 4
        tx = x0 + (x1 - x0) * self.value / 100
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.color("Track"))
        p.drawRoundedRect(QRectF(x0, cy - h / 2, x1 - x0, h), h / 2, h / 2)
        p.setBrush(t.color("Accent"))
        p.drawRoundedRect(QRectF(x0, cy - h / 2, max(h, tx - x0), h), h / 2, h / 2)
        outer = t.k * 10
        p.setPen(QPen(t.color("ThumbStroke"), 0))
        p.setBrush(t.color("ThumbOuter"))
        p.drawEllipse(QPointF(tx, cy), outer, outer)
        dot = t.k * (6 + 1 * self.hover.value - 2 * self.press.value)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.color("Accent"))
        p.drawEllipse(QPointF(tx, cy), dot, dot)


# --- Progress bar ------------------------------------------------------------------------------

class ProgressBar(Control):
    """Played / buffered bar with sentence ticks, a playhead and click-to-jump on hover."""

    jumpRequested = Signal(int)

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(theme, tips, parent, tooltip=self._tip)
        self.played = Tween(self, theme, 0.0, SLOW, decelerate())
        self.buffered = Tween(self, theme, 0.0, SLOW, decelerate())
        self.thickness = Tween(self, theme, 0.0, FAST)
        self.dim = Tween(self, theme, 0.0, MEDIUM)  # 1 → played drawn in ProgressInactive, no buffer (stopped)
        self.starts: list[float] = []     # sentence start fractions
        self.marks: list[float] | None = None  # the ticks to draw instead of the sentence starts (page starts)
        self.label_fn: Callable[[int], str] | None = None
        self.interactive = False
        self._hover_x: float | None = None
        self.setAccessibleName("Reading progress")
        self.relayout()

    def relayout(self) -> None:
        self.setFixedHeight(self.theme.px(16))

    def set_progress(self, played: float, buffered: float) -> None:
        for tw, v in ((self.played, played), (self.buffered, buffered)):
            v = max(0.0, min(1.0, v))
            if abs(v - tw.target) > 0.02:
                tw.to(v)
            elif not tw.running:
                tw.snap(v)
        self.setAccessibleName(f"Reading progress {round(self.played.target * 100)}%")

    def set_interactive(self, on: bool) -> None:
        self.interactive = on
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if not on:
            self.thickness.to(0.0)
            self._hover_x = None
        self.update()

    def _sentence_at(self, x: float) -> int:
        if not self.starts:
            return 0
        frac = max(0.0, min(1.0, x / max(1, self.width())))
        idx = 0
        for i, s in enumerate(self.starts):
            if s <= frac:
                idx = i
            else:
                break
        return idx

    def _tip(self) -> str:
        if not self.interactive or self._hover_x is None or not self.label_fn:
            return ""
        return self.label_fn(self._sentence_at(self._hover_x))

    def enterEvent(self, event) -> None:
        if self.interactive:
            self.thickness.to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.thickness.to(0.0)
        self._hover_x = None
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.interactive:
            old = self._sentence_at(self._hover_x) if self._hover_x is not None else -1
            self._hover_x = event.position().x()
            if self._sentence_at(self._hover_x) != old:
                self.tips.hover(self, self._tip)
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if self.interactive and event.button() == Qt.MouseButton.LeftButton:
            self.tips.press(self)
            self.jumpRequested.emit(self._sentence_at(event.position().x()))
            event.accept()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event) -> None:
        event.accept() if self.interactive else event.ignore()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        cy = self.height() / 2
        h = t.k * (4 + 2 * self.thickness.value)
        track = QRectF(0, cy - h / 2, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.color("Track"))
        p.drawRoundedRect(track, h / 2, h / 2)
        if t.high_contrast:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(t.color("TextPrimary"), 0))
            p.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), h / 2, h / 2)
            p.setPen(Qt.PenStyle.NoPen)
        bw = w * self.buffered.value
        if bw > 0 and self.dim.value < 1.0:
            p.setBrush(with_alpha(t.color("Buffered"), 1.0 - self.dim.value))
            p.drawRoundedRect(QRectF(0, cy - h / 2, max(h, bw), h), h / 2, h / 2)
        pw = w * self.played.value
        played_color = mix(t.color("Accent"), t.color("ProgressInactive"), self.dim.value)
        if pw > 0:
            p.setBrush(played_color)
            p.drawRoundedRect(QRectF(0, cy - h / 2, max(h, pw), h), h / 2, h / 2)
        hv = self.thickness.value
        if hv > 0 and self.starts:
            gap = t.k * 2
            surface = with_alpha(t.color("Surface"), hv)
            last_x = -1e9
            for s in (self.marks if self.marks is not None else self.starts[1:]):
                x = s * w
                if x <= 0 or x >= w:
                    continue
                if x - last_x < t.k * 4:
                    continue
                last_x = x
                p.fillRect(QRectF(x - gap / 2, cy - h / 2 - 1, gap, h + 2), surface)
        if hv > 0 and self.interactive:
            r_outer = t.k * 6
            px = max(r_outer, min(w - r_outer, pw))
            p.setBrush(with_alpha(t.color("Surface"), hv))
            p.drawEllipse(QPointF(px, cy), r_outer, r_outer)
            p.setBrush(with_alpha(played_color, hv))
            p.drawEllipse(QPointF(px, cy), r_outer - t.k * 2, r_outer - t.k * 2)


# --- Activity indicator ------------------------------------------------------------------------

IDLE_BARS = (6.0, 10.0, 8.0, 4.0)
WAVE_FRAMES = ((10, 4, 4, 4), (6, 10, 4, 4), (4, 6, 10, 4), (4, 4, 6, 10), (4, 4, 4, 6), (4, 4, 4, 4))
LIVE_PERIODS = (0.92, 1.18, 0.74, 1.36)
LIVE_PHASES = (0.0, 1.7, 3.1, 4.4)


class Indicator(QWidget):
    """16 DIP status mark: idle bars, live voice bars, preparing wave, pause mark or a state glyph."""

    def __init__(self, theme, clock: Clock, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.clock = clock
        self.mode = "idle"
        self.glyph = ""
        self.color_token = "TextSecondary"
        self._color = theme.color("TextSecondary")
        self._heights = list(IDLE_BARS)
        self._env = 0.25
        self._in_word = False
        self._last = 0.0
        self.fade = Tween(self, theme, 1.0, SLOW, decelerate())
        self._prev: tuple[str, str, QColor] | None = None
        self._subscribed = False
        clock.tick.connect(self._on_tick)
        theme.changed.connect(self._on_theme)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.relayout()

    def relayout(self) -> None:
        self.setFixedSize(self.theme.px(16), self.theme.px(16))

    def _on_theme(self) -> None:
        self._color = self.theme.color(self.color_token)
        self.update()

    def set_state(self, mode: str, color_token: str, glyph: str = "") -> None:
        if self.theme.reduced_motion and mode == "wave":
            mode, glyph = "glyph", G_BUSY
        if (mode, glyph, color_token) == (self.mode, self.glyph, self.color_token):
            return
        bar_modes = {"idle", "live", "wave"}
        if not (self.mode in bar_modes and mode in bar_modes):
            self._prev = (self.mode, self.glyph, QColor(self._color))
            self.fade.snap(0.0)
            self.fade.to(1.0)
        self.mode, self.glyph, self.color_token = mode, glyph, color_token
        self._color = self.theme.color(color_token)
        self._sync_clock()
        self.update()

    def set_in_word(self, in_word: bool) -> None:
        self._in_word = in_word

    def _animating(self) -> bool:
        if not self.isVisible() or self.theme.reduced_motion:
            return False
        if self.mode in ("live", "wave"):
            return True
        target = IDLE_BARS
        return any(abs(a - b) > 0.05 for a, b in zip(self._heights, target))

    def _sync_clock(self) -> None:
        want = self._animating()
        if want and not self._subscribed:
            self.clock.subscribe(self)
            self._subscribed = True
            self._last = 0.0
        elif not want and self._subscribed:
            self.clock.unsubscribe(self)
            self._subscribed = False
        if self.theme.reduced_motion:
            self._heights = list(IDLE_BARS)

    def showEvent(self, event) -> None:
        self._sync_clock()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        if self._subscribed:
            self.clock.unsubscribe(self)
            self._subscribed = False
        super().hideEvent(event)

    def _targets(self, now: float) -> tuple[float, ...]:
        if self.mode == "live":
            return tuple(3 + 11 * self._env * (0.5 + 0.5 * math.sin(2 * math.pi * now / per + ph))
                         for per, ph in zip(LIVE_PERIODS, LIVE_PHASES))
        if self.mode == "wave":
            pos = (now % 1.0) * len(WAVE_FRAMES)
            i = int(pos)
            f = pos - i
            a, b = WAVE_FRAMES[i], WAVE_FRAMES[(i + 1) % len(WAVE_FRAMES)]
            return tuple(x + (y - x) * f for x, y in zip(a, b))
        return IDLE_BARS

    def _on_tick(self, now: float) -> None:
        if not self._subscribed:
            return
        dt = 0.016 if not self._last else min(0.1, now - self._last)
        self._last = now
        env_target = 1.0 if self._in_word else 0.25
        self._env += (env_target - self._env) * (1 - math.exp(-dt / 0.06))
        tc = 0.05 if self.mode in ("live", "wave") else 0.09
        k = 1 - math.exp(-dt / tc)
        for i, target in enumerate(self._targets(now)):
            self._heights[i] += (target - self._heights[i]) * k
        self.update()
        if not self._animating():
            self._sync_clock()

    def _paint_mode(self, p: QPainter, mode: str, glyph: str, color: QColor) -> None:
        t = self.theme
        s = t.k
        if mode in ("idle", "live", "wave"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            heights = self._heights if not t.reduced_motion else IDLE_BARS
            for i, h in enumerate(heights):
                x = (1 + 4 * i) * s
                hh = max(2.0, h) * s
                p.drawRoundedRect(QRectF(x, (16 * s - hh) / 2, 2 * s, hh), s, s)
        elif mode == "paused":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            for x in (4, 9):
                p.drawRoundedRect(QRectF(x * s, 3 * s, 3 * s, 10 * s), s, s)
        elif mode == "glyph" and glyph:
            draw_glyph(p, QRectF(0, 0, 16 * s, 16 * s), glyph, t.font("icon16"), color)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = self.fade.value
        if self._prev and f < 1.0:
            p.setOpacity(1.0 - f)
            self._paint_mode(p, *self._prev)
        p.setOpacity(f if self._prev else 1.0)
        self._paint_mode(p, self.mode, self.glyph, self._color)
        if f >= 1.0:
            self._prev = None


class FadeLabel(QWidget):
    """A single-line label that crossfades when its text changes (status label and right info)."""

    def __init__(self, theme, role: str, color_token: str, parent: QWidget | None = None,
                 align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft):
        super().__init__(parent)
        self.theme = theme
        self.role = role
        self.color_token = color_token
        self.align = align
        self.parts: list[Part] = []
        self._prev_parts: list[Part] = []
        self.fade = Tween(self, theme, 1.0, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme.changed.connect(self.update)

    def set_parts(self, parts: list[Part], color_token: str | None = None, *, animate: bool = True) -> None:
        color_token = color_token or self.color_token
        if parts == self.parts and color_token == self.color_token:
            return
        self._prev_parts = self.parts
        self.parts = parts
        self.color_token = color_token
        if animate and self._prev_parts:
            self.fade.snap(0.0)
            self.fade.to(1.0)
        else:
            self.fade.snap(1.0)
        self.setAccessibleName(spoken_parts(parts))
        self.update()

    def set_text(self, text: str, color_token: str | None = None, *, animate: bool = True) -> None:
        self.set_parts([("text", text)] if text else [], color_token, animate=animate)

    def content_width(self, parts: list[Part] | None = None) -> float:
        parts = self.parts if parts is None else parts
        if any(k != "text" for k, _ in parts):
            return measure_parts(self.theme, parts, role=self.role)
        fm = QFontMetricsF(self.theme.font(self.role))
        return fm.horizontalAdvance("".join(s for _, s in parts))

    def _paint(self, p: QPainter, parts: list[Part], opacity: float) -> None:
        if not parts or opacity <= 0:
            return
        t = self.theme
        p.setOpacity(opacity)
        color = t.color(self.color_token)
        w = self.content_width(parts)
        x = 0.0 if self.align == Qt.AlignmentFlag.AlignLeft else max(0.0, self.width() - w)
        if any(k != "text" for k, _ in parts):
            paint_parts(p, t, x, 0, self.height(), parts, color, role=self.role)
            return
        font = t.font(self.role)
        fm = QFontMetricsF(font)
        text = "".join(s for _, s in parts)
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, self.width())
        baseline = (self.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
        p.setFont(font)
        p.setPen(color)
        w = fm.horizontalAdvance(text)
        x = 0.0 if self.align == Qt.AlignmentFlag.AlignLeft else max(0.0, self.width() - w)
        p.drawText(QPointF(x, baseline), text)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        f = self.fade.value
        if f < 1.0:
            self._paint(p, self._prev_parts, 1.0 - f)
        self._paint(p, self.parts, f)


# --- Settings page controls --------------------------------------------------------------------

TOGGLE_H = 40     # DIP: a settings row
DESCRIBED_H = 52  # DIP: a row with a detail line under its label


def _baseline(fm: QFontMetricsF, top: float, height: float) -> float:
    """The baseline that centres one line of `fm` text in the band from `top`, `height` tall."""
    return top + (height - (fm.ascent() + fm.descent())) / 2 + fm.ascent()


class ToggleRow(Control):
    """A full-width settings row: label on the left, Fluent toggle switch on the right.

    A described row (made with a `detail`) has a line in the small role under the label and is DESCRIBED_H tall;
    a new detail crossfades in 120 ms. The detail is also the row's accessible description."""

    toggled = Signal(bool)

    def __init__(self, theme, tips, label: str, parent: QWidget | None = None, tooltip: str = "", *,
                 detail: str = "", focusable: bool = False):
        super().__init__(theme, tips, parent, tooltip, focusable=focusable)
        self.label = label
        self.described = bool(detail)  # fixed when made, so the row's height never changes with its detail
        self.detail = detail
        self._prev_detail = ""
        self.checked = False
        self.knob = Tween(self, theme, 0.0, MEDIUM, decelerate())
        self.detail_fade = Tween(self, theme, 1.0, 120)
        self.clicked.connect(lambda: self.set_checked(not self.checked, emit=True))
        self.setAccessibleName(f"{label}: off")
        if detail:
            self.setAccessibleDescription(detail)

    def row_height(self) -> int:
        """The face's height: DESCRIBED_H with a detail line, TOGGLE_H without (place() adds the ring's room)."""
        return self.theme.px(DESCRIBED_H if self.described else TOGGLE_H)

    def set_detail(self, text: str) -> None:
        if text == self.detail:
            return
        self._prev_detail, self.detail = self.detail, text
        self.detail_fade.snap(0.0)
        self.detail_fade.to(1.0)
        self.setAccessibleDescription(text)
        self.update()

    def set_checked(self, on: bool, *, emit: bool = False) -> None:
        if on != self.checked:
            self.checked = on
            self.knob.to(1.0 if on else 0.0)
            self.setAccessibleName(f"{self.label}: {'on' if on else 'off'}")
            if emit:
                self.toggled.emit(on)
        elif self.knob.target != (1.0 if on else 0.0):
            self.knob.snap(1.0 if on else 0.0)

    def _paint_detail(self, p: QPainter, text: str, top: float, right: float, opacity: float) -> None:
        if not text or opacity <= 0:
            return
        t = self.theme
        font = t.font("small")
        fm = QFontMetricsF(font)
        x = self.face().left() + t.px(10)
        p.setOpacity(opacity)
        p.setFont(font)
        p.setPen(t.color("TextSecondary" if self.isEnabled() else "TextDisabled"))
        p.drawText(QPointF(x, _baseline(fm, top, t.px(16))),
                   fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0.0, right - x)))
        p.setOpacity(1.0)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.face()
        fill = mix(mix(TRANSPARENT, t.color("SubtleHover"), self.hover.value), t.color("SubtlePressed"),
                   self.press.value)
        if fill.alpha():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r, t.px(4), t.px(4))
        hc_outline(p, t, r, t.px(4), self.hover.value)
        if self.focus_ring_shown():
            paint_focus_ring(p, t, r, t.px(4))
        # Switch: 40 x 20 track, 12 DIP knob (14 on hover), slides 167 ms; centred on the whole row.
        k = self.knob.value
        tw, th = t.k * 40, t.k * 20
        track = QRectF(r.right() - t.px(10) - tw, r.top() + (r.height() - th) / 2, tw, th)
        enabled = self.isEnabled()  # disabled: label, track and knob in TextDisabled, and no hover
        disabled = t.color("TextDisabled")
        font = t.font("body")
        fm = QFontMetricsF(font)
        p.setFont(font)
        p.setPen(t.color("TextPrimary") if enabled else disabled)
        if self.described:  # label 20 + 2 + detail 16, centred as one block
            top = r.top() + (r.height() - t.px(38)) / 2
            p.drawText(QPointF(r.left() + t.px(10), _baseline(fm, top, t.px(20))), self.label)
            f = self.detail_fade.value
            right = track.left() - t.px(12)
            if f < 1.0:
                self._paint_detail(p, self._prev_detail, top + t.px(22), right, 1.0 - f)
            self._paint_detail(p, self.detail, top + t.px(22), right, f)
        else:
            p.drawText(QPointF(r.left() + t.px(10), _baseline(fm, r.top(), r.height())), self.label)
        on_fill = mix(t.color("Accent"), t.color("AccentHover"), self.hover.value) if enabled else disabled
        if k < 1.0:
            p.setOpacity(1.0 - k)
            p.setPen(QPen(t.color("TextSecondary") if enabled else disabled, t.k))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(t.k / 2, t.k / 2, -t.k / 2, -t.k / 2), th / 2, th / 2)
        if k > 0.0:
            p.setOpacity(k)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(on_fill)
            p.drawRoundedRect(track, th / 2, th / 2)
        p.setOpacity(1.0)
        d = t.k * (12 + 2 * self.hover.value - 1 * self.press.value)
        x0 = track.left() + th / 2
        x1 = track.right() - th / 2
        cx = x0 + (x1 - x0) * k
        knob_color = (mix(t.color("TextSecondary"), t.color("OnAccent"), k) if enabled
                      else mix(disabled, t.color("Surface"), k))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, track.center().y()), d / 2, d / 2)


class Segmented(Control):
    """A row of equal segments; the selected one is raised with a short accent bar (Fluent style)."""

    selected = Signal(int)

    def __init__(self, theme, tips, items: list[tuple], parent: QWidget | None = None, name: str = "", *,
                 focusable: bool = False):
        """`items`: (label, font px at k=1, tooltip[, font family]) per segment. A focusable one moves its
        selection with the arrow keys, Home and End, like a radio group."""
        super().__init__(theme, tips, parent, tooltip=self._tip, focusable=focusable)
        self.items = items
        self.index = 0
        self._hover_i: int | None = None
        self.bar = Tween(self, theme, 0.0, MEDIUM, decelerate())
        self.setAccessibleName(name)

    def set_index(self, i: int) -> None:
        if i != self.index:
            self.index = i
            self.bar.to(float(i))
            self.setAccessibleName(f"{self.accessibleName().split(':')[0]}: {self.items[i][2]}")
            self.update()

    def keyPressEvent(self, event) -> None:
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1}
        last = len(self.items) - 1
        if self.focusable and event.key() in steps:
            i = max(0, min(last, self.index + steps[event.key()]))
        elif self.focusable and event.key() in (Qt.Key.Key_Home, Qt.Key.Key_End):
            i = 0 if event.key() == Qt.Key.Key_Home else last
        else:
            super().keyPressEvent(event)
            return
        event.accept()
        if i != self.index and self.isEnabled():
            self.set_index(i)
            self.selected.emit(i)

    def _seg_w(self) -> float:
        return self.face().width() / max(1, len(self.items))

    def _index_at(self, x: float) -> int:
        return max(0, min(len(self.items) - 1, int((x - self.face().left()) / self._seg_w())))

    def _tip(self) -> str:
        return self.items[self._hover_i][2] if self._hover_i is not None else ""

    def mouseMoveEvent(self, event) -> None:
        i = self._index_at(event.position().x())
        if i != self._hover_i:
            self._hover_i = i
            self.tips.hover(self, self._tip)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_i = None
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            i = self._index_at(event.position().x())
            if i != self.index:
                self.set_index(i)
                self.selected.emit(i)
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        face = self.face()
        r = face.adjusted(0.5, 0.5, -0.5, -0.5)
        enabled = self.isEnabled()  # disabled: labels and the bar in TextDisabled, no hover
        stroke = t.color("ControlStroke" if enabled or not t.high_contrast else "DisabledOutline")
        p.setPen(QPen(stroke, 0))
        p.setBrush(t.color("SegmentTrack"))
        p.drawRoundedRect(r, t.px(4), t.px(4))
        if self.focus_ring_shown():
            paint_focus_ring(p, t, face, t.px(4))
        sw = self._seg_w()
        x0, top, h = face.left(), face.top(), face.height()
        inset = t.px(2)
        sel = QRectF(x0 + self.bar.value * sw, top, sw, h).adjusted(inset, inset, -inset, -inset)
        p.setPen(QPen(stroke, 0))
        p.setBrush(t.color("SegmentSelected"))
        p.drawRoundedRect(sel, t.px(3), t.px(3))
        if not t.dark and not t.high_contrast:  # a hairline under the white chip makes it read as raised
            p.setPen(QPen(t.color("KeycapStroke"), 0))
            y = sel.bottom() - 0.5
            p.drawLine(QPointF(sel.left() + t.px(3), y), QPointF(sel.right() - t.px(3), y))
        bar_w, bar_h = t.k * 16, t.k * 3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.color("Accent" if enabled else "TextDisabled"))
        p.drawRoundedRect(QRectF(sel.center().x() - bar_w / 2, sel.bottom() - bar_h - t.k, bar_w, bar_h),
                          bar_h / 2, bar_h / 2)
        # Every label sits on the body font's baseline, so mixed faces and sizes line up.
        fmb = QFontMetricsF(t.font("body"))
        baseline = top + (h - 3 * t.k - (fmb.ascent() + fmb.descent())) / 2 + fmb.ascent()
        for i, item in enumerate(self.items):
            label, size = item[0], item[1]
            if self._hover_i == i and i != self.index and self.hover.value > 0 and enabled:
                plate = QRectF(x0 + i * sw, top, sw, h).adjusted(inset, inset, -inset, -inset)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(t.color("SegmentSelected"), 0.6 * self.hover.value))
                p.drawRoundedRect(plate, t.px(3), t.px(3))
                hc_outline(p, t, plate, t.px(3), self.hover.value)
            font = t.font("body")
            if len(item) > 3 and item[3]:
                font.setFamily(item[3])
            font.setPixelSize(max(1, round(size * t.k)))
            fm = QFontMetricsF(font)
            p.setFont(font)
            if not enabled:
                p.setPen(t.color("TextDisabled"))
            else:
                p.setPen(t.color("TextPrimary") if i == self.index else t.color("TextSecondary"))
            p.drawText(QPointF(x0 + i * sw + (sw - fm.horizontalAdvance(label)) / 2, baseline), label)


class SwatchRow(Control):
    """Reading-background swatches: a filled circle with "Aa" in the tint's text colour."""

    selected = Signal(str)

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(theme, tips, parent, tooltip=self._tip)
        self.items: list[tuple[str, str, QColor, QColor]] = []  # key, name, background, text
        self.current = ""
        self._hover_i: int | None = None
        self.setAccessibleName("Reading background")

    def set_items(self, items: list[tuple[str, str, QColor, QColor]], current: str) -> None:
        self.items, self.current = items, current
        self.update()

    def _slot(self) -> float:
        return self.theme.px(36)

    def _index_at(self, x: float) -> int | None:
        i = int(x // self._slot())
        return i if 0 <= i < len(self.items) and x >= 0 else None

    def _tip(self) -> str:
        if self._hover_i is None:
            return ""
        name = self.items[self._hover_i][1]
        return name + " (used when Windows contrast themes are off)" if self.theme.high_contrast else name

    def mouseMoveEvent(self, event) -> None:
        i = self._index_at(event.position().x())
        if i != self._hover_i:
            self._hover_i = i
            if i is not None:
                self.tips.hover(self, self._tip)
            else:
                self.tips.leave(self)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_i = None
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            i = self._index_at(event.position().x())
            if i is not None and self.items[i][0] != self.current:
                self.current = self.items[i][0]
                self.selected.emit(self.current)
                self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        slot = self._slot()
        x0 = 0.0
        cy = self.height() / 2
        rad = t.k * 13
        font = t.font("small")
        hc = t.high_contrast  # contrast themes ignore the tint, so every swatch shows the system colours
        ring = QPen(t.color("ControlStroke" if hc else "KeycapStroke"), 0)
        for i, (key, _name, bg, fg) in enumerate(self.items):
            c = QPointF(x0 + i * slot + slot / 2, cy)
            if key == self.current:
                p.setPen(QPen(t.color("Accent"), t.k * 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, rad + t.k * 3, rad + t.k * 3)
            elif self._hover_i == i:
                p.setPen(QPen(with_alpha(t.color("TextSecondary"), self.hover.value), t.k))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, rad + t.k * 3, rad + t.k * 3)
            circle = QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad)
            if key == "auto" and not hc:  # half light preset, half dark preset: "follows the Windows theme"
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(READING_PRESETS["auto"]["light"]["background"]))
                p.drawPie(circle, 45 * 16, 180 * 16)
                p.setBrush(QColor(READING_PRESETS["auto"]["dark"]["background"]))
                p.drawPie(circle, 225 * 16, 180 * 16)
                p.setPen(ring)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, rad, rad)
                continue
            p.setPen(ring)
            p.setBrush(t.color("Surface") if hc else bg)
            p.drawEllipse(c, rad, rad)
            p.setFont(font)
            p.setPen(t.color("TextPrimary") if hc else fg)
            p.drawText(circle, int(Qt.AlignmentFlag.AlignCenter), "Aa")


class NavRow(Control):
    """A full-width settings row that opens something: label at the optical inset, an optional detail
    right-aligned before the glyph (a chevron by default), whose right edge lines up with the toggle switches.
    The whole row is the target; the detail is also its accessible description."""

    def __init__(self, theme, tips, label: str, parent: QWidget | None = None, tooltip: str = "", *,
                 glyph: str = G_CHEVRON, detail: str = "", focusable: bool = False):
        super().__init__(theme, tips, parent, tooltip, focusable=focusable)
        self.label = label
        self.glyph = glyph
        self.detail = detail
        self.setAccessibleName(label)
        if detail:
            self.setAccessibleDescription(detail)

    def set_detail(self, text: str) -> None:
        if text != self.detail:
            self.detail = text
            self.setAccessibleDescription(text)
            self.update()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.face()
        fill = mix(mix(TRANSPARENT, t.color("SubtleHover"), self.hover.value), t.color("SubtlePressed"),
                   self.press.value)
        if fill.alpha():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r, t.px(4), t.px(4))
        hc_outline(p, t, r, t.px(4), self.hover.value)
        if self.focus_ring_shown():
            paint_focus_ring(p, t, r, t.px(4))
        enabled = self.isEnabled()
        font = t.font("body")
        fm = QFontMetricsF(font)
        p.setFont(font)
        p.setPen(t.color("TextPrimary" if enabled else "TextDisabled"))
        p.drawText(QPointF(r.left() + t.px(10), _baseline(fm, r.top(), r.height())), self.label)
        g = t.px(12)
        glyph_rect = QRectF(r.right() - t.px(10) - g, r.top() + (r.height() - g) / 2, g, g)
        ink = t.color("TextSecondary" if enabled else "TextDisabled")
        draw_glyph(p, glyph_rect, self.glyph, t.font("icon12"), ink)
        if self.detail:
            small = t.font("small")
            sfm = QFontMetricsF(small)
            p.setFont(small)
            p.setPen(ink)
            x = glyph_rect.left() - t.px(8) - sfm.horizontalAdvance(self.detail)
            p.drawText(QPointF(x, _baseline(sfm, r.top(), r.height())), self.detail)


class LinkText(Control):
    """One line of text with inline links, such as "Saylight 1.3 is ready · What's new · Install".

    Parts are ("text", s) in the line's colour, ("sep", s) in TextSecondary, or (key, label) for a link. A link is
    Accent and underlined on hover (always in high contrast); a click on it emits linkClicked(key). A change of
    parts crossfades in 120 ms. A press away from a link passes through, so the window can still be dragged."""

    linkClicked = Signal(str)

    def __init__(self, theme, tips, parent: QWidget | None = None, *, role: str = "body",
                 color_token: str = "TextSecondary"):
        super().__init__(theme, tips, parent, tooltip=self._tip)
        self.role = role
        self.color_token = color_token
        self.parts: list[Part] = []
        self._prev: list[Part] = []
        self.link_tips: dict[str, str] = {}
        self.text_tip = ""   # the tooltip over the plain text, if any
        self.fade = Tween(self, theme, 1.0, 120)
        self._hits: dict[str, QRectF] = {}
        self._hover_key: str | None = None
        self._press_key: str | None = None

    def set_parts(self, parts: list[Part], color_token: str | None = None, *, animate: bool = True) -> None:
        color_token = color_token or self.color_token
        if parts == self.parts and color_token == self.color_token:
            return
        self._prev, self.parts, self.color_token = self.parts, list(parts), color_token
        if animate and self._prev:
            self.fade.snap(0.0)
            self.fade.to(1.0)
        else:
            self.fade.snap(1.0)
        self._hits = self._layout(self.parts)
        self.setAccessibleName(spoken_parts(self.parts))
        self._set_hover_key(None)
        self.update()

    def link_keys(self) -> list[str]:
        return [k for k, _ in self.parts if k not in ("text", "sep")]

    def content_width(self) -> float:
        return QFontMetricsF(self.theme.font(self.role)).horizontalAdvance("".join(s for _, s in self.parts))

    def link_rect(self, key: str) -> QRectF | None:
        return self._hits.get(key)

    def _layout(self, parts: list[Part]) -> dict[str, QRectF]:
        fm = QFontMetricsF(self.theme.font(self.role))
        hits, x = {}, 0.0
        for kind, s in parts:
            w = fm.horizontalAdvance(s)
            if kind not in ("text", "sep"):
                hits[kind] = QRectF(x, 0, w, self.height())
            x += w
        return hits

    def resizeEvent(self, event) -> None:
        self._hits = self._layout(self.parts)
        super().resizeEvent(event)

    def _key_at(self, pos) -> str | None:
        if not self.isEnabled():
            return None
        pad = self.theme.px(2)
        return next((k for k, r in self._hits.items() if r.adjusted(-pad, 0, pad, 0).contains(pos)), None)

    def _set_hover_key(self, key: str | None) -> None:
        if key == self._hover_key:
            return
        self._hover_key = key
        if key is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()
        if self.tooltip_text():
            self.tips.hover(self, self._tip)
        else:
            self.tips.leave(self)
        self.update()

    def _tip(self) -> str:
        if self._hover_key is not None:
            return self.link_tips.get(self._hover_key, "")
        return self.text_tip

    def mouseMoveEvent(self, event) -> None:
        self._set_hover_key(self._key_at(event.position()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover_key(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        key = self._key_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and key is not None:
            self._press_key = key
            super().mousePressEvent(event)
            return
        event.ignore()

    def mouseReleaseEvent(self, event) -> None:
        key, self._press_key = self._press_key, None
        if event.button() == Qt.MouseButton.LeftButton and key is not None:
            self._pressed = False
            self.press.to(0.0)
            if self._key_at(event.position()) == key:
                self.linkClicked.emit(key)
            event.accept()
            return
        event.ignore()

    def _paint(self, p: QPainter, parts: list[Part], opacity: float, current: bool) -> None:
        if not parts or opacity <= 0:
            return
        t = self.theme
        font = t.font(self.role)
        fm = QFontMetricsF(font)
        baseline = (self.height() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
        p.setOpacity(opacity)
        p.setFont(font)
        x = 0.0
        for kind, s in parts:
            w = fm.horizontalAdvance(s)
            if kind == "text":
                color = t.color(self.color_token)
            elif kind == "sep":
                color = t.color("TextSecondary")
            elif not self.isEnabled():
                color = t.color("TextDisabled")
            else:
                hovered = current and kind == self._hover_key
                pressed = current and kind == self._press_key
                color = mix(mix(t.color("Accent"), t.color("AccentHover"), self.hover.value if hovered else 0.0),
                            t.color("AccentPressed"), self.press.value if pressed else 0.0)
                if hovered or t.high_contrast:
                    p.setPen(QPen(color, max(1.0, t.k)))
                    y = baseline + max(1.0, fm.underlinePos())
                    p.drawLine(QPointF(x, y), QPointF(x + w, y))
            p.setPen(color)
            p.drawText(QPointF(x, baseline), s)
            x += w

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        f = self.fade.value
        if f < 1.0:
            self._paint(p, self._prev, 1.0 - f, False)
        self._paint(p, self.parts, f, True)
