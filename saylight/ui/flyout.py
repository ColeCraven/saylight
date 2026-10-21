"""The More button and its flyout: Open a file, Save as audio, Pronunciations and Settings.

The flyout is a menu that never takes focus, so the app you are reading from keeps the keyboard. It is a tool
window with the panel's own flags (never a QMenu, and its flags never change). It opens below More, or at the
pointer when the reading page is right-clicked, where it starts with "Change how “word” is said…" over a word.

It closes when an item is chosen, when More is clicked again, on a mouse press anywhere outside it and the panel,
after the pointer has been away from both for 4 s, and when the panel hides or is covered (the App and the panel
see to those). While a save runs, More carries a progress ring, and a failed save leaves a badge on it.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .. import win32
from .controls import (G_CANCEL, G_ERROR, G_FOLDER, G_MORE, G_OPEN_FILE, G_RETRY, G_SAVE, G_SETTINGS, G_SPEECH,
                       TRANSPARENT, Control, IconButton, draw_glyph, hc_outline)
from .motion import FAST, MEDIUM, SLOW, Tween, decelerate, exit_curve, mix

FLYOUT_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus)
WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0021, 3
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02

ROW_H, DETAIL_ROW_H, DIVIDER_H, PAD = 36, 52, 9, 4   # DIP
MIN_W, MAX_W = 220, 320
WORD_CHARS = 16          # a long word is cut to this many characters in "Change how “…” is said…"
AWAY_MS = 4000           # the pointer away from the flyout and the panel this long closes it
POLL_MS = 50             # how often a shown flyout looks for a mouse press elsewhere
MORE_TIP = "More: open a file, save as audio, pronunciations, settings"
SAVE_DISABLED_TIP = "Open a file or read some text first."
CANCEL_SAVE_TIP = "Stop saving. The unfinished file is deleted."


@dataclass(frozen=True)
class MenuItem:
    """One row of the flyout, or a divider (key "-")."""
    key: str
    glyph: str = ""
    label: str = ""
    enabled: bool = True
    detail: str = ""                       # a second line (the reason a save failed)
    tooltip: str = ""
    glyph_token: str = "TextPrimary"
    trailing: tuple[str, str, str] | None = None   # (glyph, tooltip, accessible name) of a button at the end
    name: str = ""                         # accessible name, when it isn't the label


DIVIDER = MenuItem("-")


def word_label(word: str) -> str:
    """ "Change how “fMRI” is said…", with a long word cut short."""
    word = " ".join(word.split())
    if len(word) > WORD_CHARS:
        word = word[:WORD_CHARS].rstrip() + "…"
    return f"Change how “{word}” is said…"


def menu_items(word: str | None = None, *, can_open: bool = True, can_save: bool = False,
               saving: float | None = None, failed: str = "", saved: bool = False) -> list[MenuItem]:
    """The flyout's rows. `word` adds "Change how “word” is said…" on top (right-click on a word); `saving` is
    the fraction of a save under way; `failed` is the reason the last save failed (it shows until Try again or
    a new save); `saved` offers the folder of the last save."""
    items: list[MenuItem] = []
    if word:
        items += [MenuItem("word", G_SPEECH, word_label(word)), DIVIDER]
    items.append(MenuItem("open", G_OPEN_FILE, "Open a file…", enabled=can_open))
    if failed and saving is None:
        items.append(MenuItem("save_failed", G_ERROR, "Couldn't save the audio", detail=failed,
                              glyph_token="Critical", trailing=(G_RETRY, "Try again", "Try saving the audio again")))
    if saving is not None:
        percent = max(0, min(100, int(saving * 100)))
        items.append(MenuItem("saving", G_SAVE, f"Saving audio · {percent}%", name=f"Saving audio, {percent} percent",
                              trailing=(G_CANCEL, CANCEL_SAVE_TIP, "Cancel saving audio")))
    else:
        items.append(MenuItem("save", G_SAVE, "Save as audio…", enabled=can_save,
                              tooltip="" if can_save else SAVE_DISABLED_TIP))
    if saved:
        items.append(MenuItem("saved", G_FOLDER, "Show saved audio in folder"))
    items += [DIVIDER, MenuItem("pron", G_SPEECH, "Pronunciations…"), MenuItem("settings", G_SETTINGS, "Settings")]
    return items


def item_height(item: MenuItem) -> int:
    """In DIP."""
    if item.key == "-":
        return DIVIDER_H
    return DETAIL_ROW_H if item.detail else ROW_H


def flyout_size(theme, items: list[MenuItem]) -> QSize:
    """clamp(44 + widest label + 16 (+ 36 with a trailing button), 220, 320) DIP wide; rows, dividers and 4 DIP
    of padding top and bottom tall."""
    px = theme.px
    body = QFontMetricsF(theme.font("body"))
    small = QFontMetricsF(theme.font("small"))
    widest = 0.0
    for item in items:
        if item.key == "-":
            continue
        w = max(body.horizontalAdvance(item.label), small.horizontalAdvance(item.detail) if item.detail else 0.0)
        widest = max(widest, w + (px(36) if item.trailing else 0))
    width = max(px(MIN_W), min(px(MAX_W), math.ceil(px(44) + widest + px(16))))
    return QSize(width, px(2 * PAD) + sum(px(item_height(i)) for i in items))


def flyout_rect(anchor: QRect, size: QSize, area: QRect, mode: str, *, gap: int = 4, margin: int = 8) -> QRect:
    """Where the flyout goes, in global coordinates.

    "more": `anchor` is the More plate. The flyout's right edge is the plate's, its top `gap` below the plate;
    when that would pass the work area's bottom (less `margin`), it opens upward, ending `gap` above the plate.
    "pointer": `anchor` is the pointer; the top-left corner sits at pointer + (2, 2), flipped left or up to fit.
    Either way it is kept `margin` inside the work area."""
    w, h = size.width(), size.height()
    left, top = area.left() + margin, area.top() + margin
    right, bottom = area.right() + 1 - margin, area.bottom() + 1 - margin
    if mode == "more":
        x = anchor.right() + 1 - w
        y = anchor.bottom() + 1 + gap
        if y + h > bottom:
            y = anchor.top() - gap - h
    elif mode == "pointer":
        x, y = anchor.x() + 2, anchor.y() + 2
        if x + w > right:
            x = anchor.x() - 2 - w
        if y + h > bottom:
            y = anchor.y() - 2 - h
    else:
        raise ValueError(f"unknown flyout mode: {mode}")
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return QRect(x, y, w, h)


# --- The More button -----------------------------------------------------------------------------

class MoreButton(IconButton):
    """More (U+E712): drawn selected while the flyout is open, with a progress ring while a save runs and a
    Critical badge after a save failed."""

    def __init__(self, theme, tips, parent: QWidget | None = None):
        super().__init__(theme, tips, G_MORE, size=32, small_glyph=True, parent=parent, tooltip=self._tip,
                         name="More")
        self.fraction: float | None = None
        self.eta = ""
        self.badge = False
        self.arc = Tween(self, theme, 0.0, SLOW, decelerate())
        self.ring = Tween(self, theme, 0.0, MEDIUM)   # the ring's opacity: it fades once a save is done

    def _tip(self) -> str:
        if self.fraction is not None:
            left = f", {self.eta}" if self.eta else ""
            return f"More · Saving audio {int(self.fraction * 100)}%{left}"
        return MORE_TIP

    def set_progress(self, fraction: float, eta: str = "") -> None:
        """A save under way: the arc grows to `fraction` (0-1) over 250 ms. `eta` is "about 3 min left"."""
        if self.fraction is None:
            self.arc.snap(0.0)
        self.fraction, self.eta = max(0.0, min(1.0, fraction)), eta
        self.ring.snap(1.0)
        self.arc.to(self.fraction)
        self.setAccessibleDescription(f"Saving audio, {int(self.fraction * 100)} percent")
        self.tips.refresh(self)
        self.update()

    def finish_progress(self, done: bool) -> None:
        """The save ended: a finished one fills the ring, which then fades; a cancelled or failed one just goes."""
        if self.fraction is None:
            return
        self.fraction, self.eta = None, ""
        if done:
            self.arc.to(1.0)
            QTimer.singleShot(self.theme.ms(SLOW), lambda: self.ring.to(0.0))
        else:
            self.arc.snap(0.0)
            self.ring.snap(0.0)
        self.setAccessibleDescription(MORE_TIP)
        self.tips.refresh(self)
        self.update()

    def set_badge(self, on: bool) -> None:
        if on != self.badge:
            self.badge = on
            self.setAccessibleName("More, saving the audio failed" if on else "More")
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        t = self.theme
        r = self.face()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        opacity = self.ring.value if self.fraction is None else 1.0
        if opacity > 0 and (self.fraction is not None or self.arc.value > 0):
            stroke = 2.0 * t.k
            radius = (t.px(24) - stroke) / 2
            c = r.center()
            box = QRectF(c.x() - radius, c.y() - radius, 2 * radius, 2 * radius)
            p.setOpacity(opacity)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if t.high_contrast:
                p.setPen(QPen(t.color("TextPrimary"), 0))
            else:
                p.setPen(QPen(t.color("Track"), stroke))
            p.drawEllipse(box)
            span = max(0.0, min(1.0, self.arc.value))
            if span > 0:
                pen = QPen(t.color("TextPrimary" if t.high_contrast else "TextSecondary"), stroke)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawArc(box, 90 * 16, -round(span * 360 * 16))  # clockwise from 12 o'clock
            p.setOpacity(1.0)
        if self.badge:
            centre = QPointF(r.left() + t.px(24), r.top() + t.px(8))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.color("Surface"))
            p.drawEllipse(centre, t.px(4), t.px(4))
            p.setBrush(t.color("TextPrimary" if t.high_contrast else "Critical"))
            p.drawEllipse(centre, t.px(3), t.px(3))


# --- The flyout window ---------------------------------------------------------------------------

class _Row(Control):
    """One item: a plate on hover, the glyph at 16, the label at 44 and an optional button at the end."""

    def __init__(self, theme, tips, item: MenuItem, parent: QWidget):
        super().__init__(theme, tips, parent, tooltip=item.tooltip)
        self.item = item
        self.button: IconButton | None = None
        self.setAccessibleName(item.name or item.label + (f", {item.detail}" if item.detail else ""))
        if not item.enabled:
            self.setAccessibleDescription(item.tooltip or "Not available now")
        if item.trailing:
            glyph, tip, name = item.trailing
            self.button = IconButton(theme, tips, glyph, size=28, small_glyph=True, parent=self, tooltip=tip,
                                     name=name)

    def enterEvent(self, event) -> None:
        if not self.item.enabled:  # no plate, but the tooltip says why
            if self.tooltip_text():
                self.tips.hover(self, self.tooltip_text)
            return
        super().enterEvent(event)

    def mousePressEvent(self, event) -> None:
        if self.item.enabled:
            super().mousePressEvent(event)
        else:
            event.accept()

    def resizeEvent(self, event) -> None:
        if self.button is not None:
            self.button.move(self.width() - self.theme.px(36), (self.height() - self.button.height()) // 2)
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.theme
        px = t.px
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        item = self.item
        plate = QRectF(px(PAD), 0, self.width() - 2 * px(PAD), self.height())
        if item.enabled and (item.key != "saving"):
            fill = mix(mix(TRANSPARENT, t.color("SubtleHover"), self.hover.value), t.color("SubtlePressed"),
                       self.press.value)
            if fill.alpha() and not t.high_contrast:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(plate, px(4), px(4))
            hc_outline(p, t, plate, px(4), self.hover.value)
        # The label's band: the whole row, or its first line (+8, 20 tall) when a detail line follows at +30.
        top, line_h = (px(8), px(20)) if item.detail else (0, self.height())
        if not item.enabled:
            ink = t.color("TextDisabled")
            glyph_ink = ink
        else:
            ink = mix(t.color("TextPrimary"), t.color("TextSecondary"), self.press.value)
            glyph_ink = t.color(item.glyph_token) if item.glyph_token != "TextPrimary" else ink
        g = px(16)
        draw_glyph(p, QRectF(px(16), top + (line_h - g) / 2, g, g), item.glyph, t.font("icon16"), glyph_ink)
        right = self.width() - px(16) - (px(36) if self.button is not None else 0)
        font = QFont(t.font("body"))
        if item.key == "saving":  # the percentage counts up without the label jittering
            font.setFeature(QFont.Tag("tnum"), 1)
        fm = QFontMetricsF(font)
        p.setFont(font)
        p.setPen(ink)
        baseline = top + (line_h - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
        p.drawText(QPointF(px(44), baseline), fm.elidedText(item.label, Qt.TextElideMode.ElideRight,
                                                             max(0.0, right - px(44))))
        if item.detail:
            small = t.font("small")
            sfm = QFontMetricsF(small)
            p.setFont(small)
            p.setPen(t.color("TextSecondary") if item.enabled else ink)
            sb = px(30) + (px(16) - (sfm.ascent() + sfm.descent())) / 2 + sfm.ascent()
            p.drawText(QPointF(px(44), sb), sfm.elidedText(item.detail, Qt.TextElideMode.ElideRight,
                                                            max(0.0, right - px(44))))


class Flyout(QWidget):
    """The flyout window. Created hidden, never activated; the panel opens and closes it."""

    chosen = Signal(str)           # an item's key
    trailingClicked = Signal(str)  # the key of the row whose end button was clicked
    closed = Signal()

    def __init__(self, theme, tips, *, inside: Callable[[QPoint], bool] | None = None):
        super().__init__(None, FLYOUT_FLAGS)
        self.theme = theme
        self.tips = tips
        self.inside = inside or (lambda _pos: False)   # a global point over the panel, which handles its own clicks
        self.items: list[MenuItem] = []
        self.rows: list[_Row] = []
        self.mode = "more"
        self._anchor = QRect()
        self._rest = QPoint()
        self._dir = 1
        self._closing = False
        self._down = False
        self._away_since: float | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Saylight menu")
        self.setAccessibleName("More")
        self.hwnd = int(self.winId())
        win32.dwm_set_corner(self.hwnd)
        win32.dwm_disable_transitions(self.hwnd)
        self.anim = Tween(self, theme, 0.0, MEDIUM, decelerate())
        self.anim.on_change = self._apply_motion
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._check_pointer)
        theme.changed.connect(self._on_theme)
        self._on_theme()

    @property
    def is_open(self) -> bool:
        return self.isVisible() and not self._closing

    def _on_theme(self) -> None:
        t = self.theme
        win32.dwm_set_dark(self.hwnd, t.dark)
        ink = t.color("TextPrimary")  # high contrast: a WindowText border, as the sheets have
        win32.dwm_set_border_color(self.hwnd, (ink.red(), ink.green(), ink.blue()) if t.high_contrast else None)
        if self.is_open:
            self.set_items(self.items)

    # --- Content and placement -------------------------------------------------------------------

    def open_at(self, items: list[MenuItem], anchor: QRect, mode: str) -> None:
        """Show `items` below the More plate (`mode` "more", `anchor` its global rect) or at the pointer
        ("pointer", `anchor` a 1 x 1 rect at the pointer). Already open: moves there with the new items."""
        self.mode, self._anchor = mode, QRect(anchor)
        self._closing = False
        self._build(items)
        rect = self._place()
        self._dir = -1 if mode == "more" and rect.top() < anchor.top() else 1
        if mode == "pointer" and rect.top() < anchor.top():
            self._dir = -1
        self._down = self._buttons()[0]  # also forgets presses from before it opened
        self._away_since = None
        if not self.isVisible():
            self.anim.snap(0.0)
            self._apply_motion(0.0)
            self.show()
            win32.set_topmost(self.hwnd)
        self.anim.to(1.0, MEDIUM, decelerate())
        self._poll.start()

    def set_items(self, items: list[MenuItem]) -> None:
        """New rows in the open flyout (a save's progress, a failure), keeping its place."""
        if not self.isVisible():
            self.items = list(items)
            return
        self._build(items)
        self._place()
        self._apply_motion(self.anim.value)

    def _build(self, items: list[MenuItem]) -> None:
        t = self.theme
        px = t.px
        size = flyout_size(t, items)
        same = [i.key for i in items] == [i.key for i in self.items] and len(self.rows) == len(
            [i for i in items if i.key != "-"])
        if not same:
            for row in self.rows:
                row.deleteLater()
            self.rows = []
        self.items = list(items)
        y = px(PAD)
        rows = iter(self.rows) if same else None
        for item in items:
            h = px(item_height(item))
            if item.key != "-":
                if rows is not None:
                    row = next(rows)
                    row.item = item
                    row.set_tooltip(item.tooltip)
                    row.setAccessibleName(item.name or item.label)
                    row.update()
                else:
                    row = _Row(t, self.tips, item, self)
                    row.clicked.connect(lambda key=item.key: self._chosen(key))
                    if row.button is not None:
                        row.button.clicked.connect(lambda key=item.key: self._trailing(key))
                    row.show()
                    self.rows.append(row)
                row.setGeometry(0, y, size.width(), h)
            y += h
        self.setFixedSize(size)
        self.update()

    def _place(self) -> QRect:
        screen = QGuiApplication.screenAt(self._anchor.center()) or QGuiApplication.primaryScreen()
        px = self.theme.px
        rect = flyout_rect(self._anchor, self.size(), screen.availableGeometry(), self.mode, gap=px(4), margin=px(8))
        self._rest = rect.topLeft()
        return rect

    # --- Closing ---------------------------------------------------------------------------------

    def close_animated(self) -> None:
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        self._poll.stop()
        self.tips.press(self)
        self.anim.to(0.0, FAST, exit_curve())

    def close_now(self) -> None:
        if not self.isVisible():
            return
        self._closing = True
        self._poll.stop()
        self.anim.snap(0.0)

    def _apply_motion(self, v: float) -> None:
        if self._closing and v <= 0.0:
            self._closing = False
            self.hide()
            self._poll.stop()
            self.closed.emit()
            return
        dy = 0 if self._closing else round(self.theme.px(4) * (1.0 - v)) * -self._dir
        self.move(self._rest.x(), self._rest.y() + dy)
        self.setWindowOpacity(1.0 if v >= 0.999 else max(0.0, v))

    def _chosen(self, key: str) -> None:
        self.close_animated()
        self.chosen.emit(key)

    def _trailing(self, key: str) -> None:
        self.close_animated()
        self.trailingClicked.emit(key)

    # --- Pointer away and presses elsewhere ------------------------------------------------------

    @staticmethod
    def _buttons() -> tuple[bool, bool]:
        """(a mouse button is down, one was pressed since the last look): a quick tap can start and end between
        two polls."""
        left, right = win32.key_state(VK_LBUTTON), win32.key_state(VK_RBUTTON)
        return left[0] or right[0], left[1] or right[1]

    def _check_pointer(self) -> None:
        if not self.is_open:
            return
        pos = QCursor.pos()
        over = self.frameGeometry().contains(pos) or self.inside(pos)
        down, tapped = self._buttons()
        pressed, self._down = tapped or (down and not self._down), down
        if pressed and not over:
            self.close_animated()
            return
        if over:
            self._away_since = None
            return
        # Measured, not counted in ticks: a 50 ms timer fires about every 62 ms on Windows.
        now = time.monotonic()
        if self._away_since is None:
            self._away_since = now
        elif (now - self._away_since) * 1000 >= AWAY_MS:
            self.close_animated()

    # --- Native and painting ---------------------------------------------------------------------

    def nativeEvent(self, event_type, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_MOUSEACTIVATE:
            return True, MA_NOACTIVATE
        return super().nativeEvent(event_type, message)

    def paintEvent(self, _event) -> None:
        t = self.theme
        px = t.px
        p = QPainter(self)
        p.fillRect(self.rect(), t.color("Surface"))
        p.setPen(QPen(t.color("TextPrimary" if t.high_contrast else "ControlStroke"), 0))
        y = px(PAD)
        for item in self.items:
            h = px(item_height(item))
            if item.key == "-":
                line = y + px(4) + 0.5
                p.drawLine(QPointF(px(PAD), line), QPointF(self.width() - px(PAD), line))
            y += h
