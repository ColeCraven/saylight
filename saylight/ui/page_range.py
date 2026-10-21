"""The Choose pages window for a long scanned PDF: which pages to find the text in.

A small focusable window beside the panel, following the Type or paste box's rules through the sheet recipe:
unowned, fixed flags, in the foreground only while open, light dismiss, DWM corners and dark mode. It is as wide
as the panel and 168 DIP tall, placed at the panel's anchored edge, and the panel counts as covered while it is
open. It opens with a 167 ms fade and closes with a 120 ms one; nothing moves.

    From page  [ -   1   + ]   to  [ -  50   + ]   of 160
    50 pages · about 40 s to find the text
    ( > Read these pages )                       [ x Cancel ]

Esc, Back, Cancel and light dismiss close it and keep the choice card; Enter or the pill reads.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from PySide6.QtCore import QRect, QRectF, QRegularExpression, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QKeyEvent, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import QWidget

from ..documents.pdf_ocr import estimate_s
from .compose import compose_rect
from .controls import (FOCUS_PAD, G_CANCEL, G_MINUS, G_PAGES, G_PLAY, G_PLUS, TRANSPARENT, FadeLabel,
                       PillButton, TextButton, draw_glyph, hc_outline, paint_focus_ring)
from .motion import FAST, Tween, mix
from .sheet import LineField, Sheet

NATURAL = QSize(476, 168)    # the compact panel's width; the window always takes the panel's own width
NAME = "Choose pages"
DEFAULT_PAGES = 50           # From 1 to this (or the last page) when it opens
MAX_DIGITS = 5
REPEAT_MS = (400, 120)       # holding - or +: the first repeat, then every
BIG_STEP_AFTER_S = 2.0       # holding longer steps by BIG_STEP
BIG_STEP = 10
READ_LABEL = "Read these pages"


# --- Pure helpers ------------------------------------------------------------------------------

def clamp_range(a: int, b: int, n: int) -> tuple[int, int]:
    """(first, last) inside 1..n, swapped if given the wrong way round."""
    n = max(1, int(n))
    a, b = (max(1, min(n, int(v))) for v in (a, b))
    return (a, b) if a <= b else (b, a)


def eta_text(seconds: float) -> str:
    """"40 s" (rounded up to 10 s) under a minute, then "2 min", then "1 h 5 min"."""
    s = max(0, math.ceil(seconds))
    tens = max(10, math.ceil(s / 10) * 10)
    if tens < 60:
        return f"{tens} s"
    minutes = max(1, round(s / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def range_note(n_pages: int, n_scanned: int) -> str:
    pages = f"{n_pages:,} page{'s' if n_pages != 1 else ''}"
    if n_scanned <= 0:
        return f"{pages} · no scanned pages"
    return f"{pages} · about {eta_text(estimate_s(n_scanned))} to find the text"


# --- The page stepper --------------------------------------------------------------------------

class _PageField(LineField):
    """The stepper's middle: the page number, typed. Digits only; arrows and Page Up/Down step it."""

    def __init__(self, theme, stepper: "PageStepper", name: str):
        super().__init__(theme, stepper, style="control", name=name)
        self.stepper = stepper
        self.ring = False  # focus came from the keyboard
        self.setMaxLength(MAX_DIGITS)
        self.setValidator(QRegularExpressionValidator(QRegularExpression(rf"\d{{0,{MAX_DIGITS}}}"), self))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setDragEnabled(False)

    def field_font(self):
        return self.theme.font("value")

    def apply_theme(self) -> None:
        super().apply_theme()
        self.setTextMargins(0, 0, 0, 0)
        self.setFixedHeight(self.theme.px(32))

    def paintEvent(self, event) -> None:
        # The stepper draws the face around it; only the number and the caret are drawn here.
        super(LineField, self).paintEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        steps = {Qt.Key.Key_Up: 1, Qt.Key.Key_Down: -1, Qt.Key.Key_PageUp: BIG_STEP, Qt.Key.Key_PageDown: -BIG_STEP}
        if event.key() in steps and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            self.stepper.step(steps[event.key()])
            self.selectAll()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        self.ring = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        super().focusInEvent(event)
        self.stepper.update()

    def focusOutEvent(self, event) -> None:
        self.ring = False
        super().focusOutEvent(event)
        self.stepper.commit()
        self.stepper.update()

    def mousePressEvent(self, event) -> None:
        self.ring = False  # the ring is for keyboard use only
        super().mousePressEvent(event)
        self.stepper.update()


class PageStepper(QWidget):
    """[ −  12  + ]: the speed stepper's look (112 x 32: − 32, the number 48, + 32) around a typed page number.
    −/+ step by 1, and holding repeats (400 then 120 ms), by 10 after 2 s; the wheel steps by 1."""

    valueChanged = Signal(int)

    def __init__(self, theme, name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.n = 1
        self._value = 1
        self.field = _PageField(theme, self, name)
        self.field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFocusProxy(self.field)  # the stepper itself never takes focus: its number does
        self.setMouseTracking(True)
        self.setAccessibleName(name)
        self._zone: str | None = None
        self._press_zone: str | None = None
        self._held_since = 0.0
        self._hover = {z: Tween(self, theme, 0.0, FAST) for z in ("minus", "plus")}
        self.press = Tween(self, theme, 0.0, FAST)
        self._repeat = QTimer(self)
        self._repeat.setSingleShot(True)
        self._repeat.timeout.connect(self._on_repeat)
        self._wheel = 0
        self.field.textEdited.connect(self._on_edited)
        self.field.editingFinished.connect(self.commit)
        theme.changed.connect(self.relayout)
        self.relayout()
        self._show()

    # --- Value --------------------------------------------------------------------------------

    def value(self) -> int:
        return self._value

    def set_range(self, n: int) -> None:
        self.n = max(1, int(n))
        self.set_value(self._value)

    def set_value(self, v: int) -> None:
        v = max(1, min(self.n, int(v)))
        changed = v != self._value
        self._value = v
        self._show()
        if changed:
            self.valueChanged.emit(v)

    def step(self, delta: int) -> None:
        self.set_value(self._value + delta)

    def commit(self) -> None:
        """Show the value the field holds (an empty or out-of-range entry goes back to it)."""
        self._show()

    def _show(self) -> None:
        text = str(self._value)
        if self.field.text() != text:
            self.field.setText(text)
        self.update()

    def _on_edited(self, text: str) -> None:
        if not text:
            return  # still typing: keep the last value until the field is left
        v = int(text)
        if v > self.n or v < 1:
            self.set_value(v)
            self.field.selectAll()
        elif v != self._value:
            self._value = v
            self.update()
            self.valueChanged.emit(v)

    # --- Geometry and painting ----------------------------------------------------------------

    def pad(self) -> int:
        return self.theme.px(FOCUS_PAD)

    def face(self) -> QRectF:
        m = self.pad()
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def place(self, rect: QRect) -> None:
        """Put the stepper's face at `rect` (parent coordinates)."""
        m = self.pad()
        self.setGeometry(rect.adjusted(-m, -m, m, m))

    def relayout(self) -> None:
        t = self.theme
        m = self.pad()
        self.setFixedSize(t.px(112) + 2 * m, t.px(32) + 2 * m)
        self.field.setGeometry(m + t.px(32), m, t.px(48), t.px(32))

    def _zones(self) -> dict[str, QRectF]:
        f, w32 = self.face(), self.theme.px(32)
        return {"minus": QRectF(f.left(), f.top(), w32, f.height()),
                "plus": QRectF(f.right() - w32, f.top(), w32, f.height())}

    def _zone_at(self, pos) -> str | None:
        return next((z for z, r in self._zones().items() if r.contains(pos)), None)

    def _enabled(self, zone: str | None) -> bool:
        return (zone == "minus" and self._value > 1) or (zone == "plus" and self._value < self.n)

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        focused = self.field.hasFocus()
        width = (2.0 if t.high_contrast else float(t.k)) if focused else 0.0  # 0: a cosmetic hairline
        inset = max(width, 1.0) / 2
        face = self.face()
        r = face.adjusted(inset, inset, -inset, -inset)
        p.setPen(QPen(t.color("TextPrimary") if focused else t.color("ControlStroke"), width))
        p.setBrush(t.color("ControlFill"))
        p.drawRoundedRect(r, t.px(4), t.px(4))
        inner = t.px(2)
        for zone, glyph in (("minus", G_MINUS), ("plus", G_PLUS)):
            zr = self._zones()[zone]
            plate = zr.adjusted(inner, inner, -inner, -inner)
            enabled = self._enabled(zone)
            hv = self._hover[zone].value if enabled else 0.0
            pressed = self.press.value if self._press_zone == zone and enabled else 0.0
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
        if focused and self.field.ring:
            paint_focus_ring(p, t, face, t.px(4))

    # --- Mouse --------------------------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        zone = self._zone_at(event.position())
        if zone != self._zone:
            self._zone = zone
            for z, tw in self._hover.items():
                tw.to(1.0 if z == zone and self._enabled(z) else 0.0)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._zone = None
        for tw in self._hover.values():
            tw.to(0.0)
        self._repeat.stop()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        event.accept()
        zone = self._zone_at(event.position())
        if not self._enabled(zone):
            return
        self.field.ring = False
        self._zone = self._press_zone = zone
        self._held_since = time.monotonic()
        self.press.to(1.0)
        self.step(-1 if zone == "minus" else 1)
        self._repeat.start(REPEAT_MS[0])

    def _on_repeat(self) -> None:
        zone = self._press_zone
        if zone is None or self._zone != zone or not self._enabled(zone):
            return
        size = BIG_STEP if time.monotonic() - self._held_since >= BIG_STEP_AFTER_S else 1
        self.step(-size if zone == "minus" else size)
        self._repeat.start(REPEAT_MS[1])

    def mouseReleaseEvent(self, event) -> None:
        self._repeat.stop()
        self._press_zone = None
        self.press.to(0.0)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)  # a fast second click is another step

    def wheelEvent(self, event) -> None:
        self._wheel += event.angleDelta().y()
        while abs(self._wheel) >= 120:
            step = 1 if self._wheel > 0 else -1
            self._wheel -= 120 * step
            self.step(step)
        event.accept()


# --- The window --------------------------------------------------------------------------------

class PageRangeWindow(Sheet):
    """Call open_at() in the chip's click; `rangeChosen` carries the pages to read, and `closeRequested` asks
    the App to close it ("read" after rangeChosen; "esc", "back" (Back, Cancel, Alt+F4) or "away" otherwise),
    which keeps the choice card."""

    rangeChosen = Signal(int, int)   # first, last: 1-based document pages, inclusive

    def __init__(self, ctl):
        super().__init__(ctl, name=NAME, glyph=G_PAGES, title=NAME, natural=NATURAL, mode="panel")
        t, host = self.theme, self.body
        self.n = 1
        self._scanned_in: Callable[[int, int], int] = lambda a, b: b - a + 1
        self.lbl_from = FadeLabel(t, "body", "TextPrimary", host)
        self.lbl_to = FadeLabel(t, "body", "TextPrimary", host)
        self.lbl_of = FadeLabel(t, "body", "TextPrimary", host)
        self.lbl_from.set_text("From page", animate=False)
        self.lbl_to.set_text("to", animate=False)
        self.first = PageStepper(t, "First page", host)
        self.last = PageStepper(t, "Last page", host)
        self.note = FadeLabel(t, "small", "TextSecondary", host)
        self.pill = PillButton(t, self.tips, G_PLAY, READ_LABEL, host)
        self.btn_cancel = TextButton(t, self.tips, G_CANCEL, "Cancel", parent=host)
        self.pill.clicked.connect(self.primary)
        self.btn_cancel.clicked.connect(lambda: self.closeRequested.emit("back"))
        self.first.valueChanged.connect(lambda _v: self._sync())
        self.last.valueChanged.connect(lambda _v: self._sync())
        QWidget.setTabOrder(self.first.field, self.last.field)
        QWidget.setTabOrder(self.last.field, self.pill)
        QWidget.setTabOrder(self.pill, self.btn_cancel)
        QWidget.setTabOrder(self.btn_cancel, self.btn_back)
        self.prepare(1, None)

    # --- Content -------------------------------------------------------------------------------

    def prepare(self, pages: int, scanned_in: Callable[[int, int], int] | None) -> None:
        """The PDF's page count and how to count its scanned pages in a range; the range starts at 1 to 50."""
        self.n = max(1, int(pages))
        self._scanned_in = scanned_in or (lambda a, b: b - a + 1)
        self.lbl_of.set_text(f"of {self.n:,}", animate=False)
        self.set_info(f"{self.n:,} page{'s' if self.n != 1 else ''}")
        for stepper in (self.first, self.last):
            stepper.set_range(self.n)
        self.first.set_value(1)
        self.last.set_value(min(DEFAULT_PAGES, self.n))
        self._sync()
        self.layout_content(self.width(), self.height())

    def chosen(self) -> tuple[int, int]:
        return clamp_range(self.first.value(), self.last.value(), self.n)

    def _sync(self) -> None:
        a, b = self.chosen()
        self.note.set_text(range_note(b - a + 1, self._scanned_in(a, b)), animate=False)

    # --- Opening -------------------------------------------------------------------------------

    def open_at(self, rest: QRect, *, pages: int = 0, scanned_in: Callable[[int, int], int] | None = None,
                return_to: int = 0, panel_visible: bool = True, source: str = "") -> bool:
        """Open beside the panel's rest rect. `scanned_in(a, b)` counts the scanned pages in a range. With no
        `pages`, what prepare() was given last. Focus starts in From, its number selected."""
        if pages and not self.is_open:
            self.prepare(pages, scanned_in)
        self.first.field.selectAll()
        return super().open_at(rest, return_to=return_to, panel_visible=panel_visible, source=source)

    def _geometry(self, rest: QRect) -> QRect:
        t, panel = self.theme, self.ctl.panel
        screen = QGuiApplication.screenAt(rest.center()) or QGuiApplication.primaryScreen()
        snap_x = (self.ctl.settings.get("panel.position") or {}).get("snap_x")
        size = QSize(rest.width(), t.px(NATURAL.height()))
        return compose_rect(rest, screen.availableGeometry(), size, snap_x=snap_x, margin=t.px(12),
                            bottom_margin=panel._bottom_margin(screen))

    # --- Sheet hooks ---------------------------------------------------------------------------

    def focus_target(self) -> QWidget | None:
        return self.first.field

    def primary(self) -> None:
        if self.session.phase == "closing":
            return
        for stepper in (self.first, self.last):
            stepper.commit()
        first, last = self.chosen()
        self.rangeChosen.emit(first, last)
        self.closeRequested.emit("read")

    def show_note(self, text: str) -> None:
        self.note.setVisible(False)  # the activation note takes the range note's row
        super().show_note(text)

    def end_note(self) -> None:
        super().end_note()
        self.note.setVisible(True)

    def _place_chrome(self, W: int, H: int) -> None:
        super()._place_chrome(W, H)
        px = self.theme.px
        self.hint.setGeometry(QRect(px(22), px(92), W - 2 * px(22), px(16)))

    def layout_content(self, W: int, H: int) -> None:
        t = self.theme
        px = t.px
        fm = QFontMetricsF(t.font("body"))
        row, label_h = px(52), px(20)
        label_y = row + (px(32) - label_h) // 2
        x_from = px(22)
        w_from = math.ceil(fm.horizontalAdvance("From page"))
        self.lbl_from.setGeometry(x_from, label_y, w_from + px(2), label_h)
        s1 = max(px(104), x_from + w_from + px(8))  # the steppers keep their columns while the labels fit
        self.first.place(QRect(s1, row, px(112), px(32)))
        x_to = s1 + px(112) + px(12)
        w_to = math.ceil(fm.horizontalAdvance("to"))
        self.lbl_to.setGeometry(x_to, label_y, w_to + px(2), label_h)
        s2 = max(px(252), x_to + w_to + px(8))
        self.last.place(QRect(s2, row, px(112), px(32)))
        x_of = s2 + px(112) + px(12)
        self.lbl_of.setGeometry(x_of, label_y, max(0, W - px(12) - x_of), label_h)
        self.note.setGeometry(px(22), px(92), W - 2 * px(22), px(16))
        size = self.pill.natural_size()
        self.pill.place(QRect(px(12), H - px(52), size.width(), size.height()))
        cancel = self.btn_cancel.natural_size()
        self.btn_cancel.place(QRect(W - px(12) - cancel.width(), H - px(52) + (px(40) - cancel.height()) // 2,
                                    cancel.width(), cancel.height()))
